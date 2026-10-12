"""
容器内 claude CLI 登录态探测 + 告警。

底层逻辑：crashguard 的 AI 分析 / 自动 PR 全部依赖容器内 `claude` CLI 的订阅 OAuth 登录。
登录失效时 CLI 直接 exit 1、stderr 为空，分析行静默落 failed——任务心跳照常是 success/skipped，
job_health 扫描看不出来（2026-09-19 ~ 10-09 实测：503 次分析全挂、0 个 PR，静默 20 天，
最后是别的任务超期才被顺带发现）。这里直接真调一次 CLI，失败就发独立飞书告警，
卡片里写明恢复命令。

挂在 job_health tick 上（5min 一次），本模块自己按 claude_auth_probe_interval_minutes 节流
真实探测频率，避免每 5 分钟都花一次调用。
- 鉴权类失败（输出里带登录/401 关键字）：首次即告警——确定性故障，不会自愈
- 其他失败（超时 / 网络 / CLI 缺失）：连续 2 次才告警，过滤偶发抖动
- 恢复后只打日志不发卡（遵循"报告只显示异常"约定）
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from app.crashguard.config import get_crashguard_settings

logger = logging.getLogger("crashguard.claude_auth_probe")

_PROBE_TIMEOUT_SEC = 90
# CLI 鉴权失败时的输出特征（大小写不敏感）
_AUTH_MARKERS = (
    "failed to authenticate",
    "oauth",
    "not logged in",
    "please run /login",
    "invalid api key",
    "401",
)
RECOVERY_COMMAND = "docker compose exec -it backend claude login"

_last_probe_at: Optional[datetime] = None
_last_alerted_at: Optional[datetime] = None
_consecutive_failures: int = 0


def _classify(output: str) -> str:
    low = (output or "").lower()
    return "auth" if any(m in low for m in _AUTH_MARKERS) else "other"


async def probe_claude_auth(timeout: float = _PROBE_TIMEOUT_SEC) -> Tuple[bool, str, str]:
    """真调一次 `claude -p`。返回 (ok, kind, detail)；kind ∈ {"", "auth", "other"}。

    prompt 走参数而不是 stdin：登录失效时 CLI 会在读 stdin 前就退出，往已关闭的管道
    写 prompt 会抛 "unable to perform operation on WriteUnixTransport"（2026-10-09 实测），
    把真正的鉴权报错盖掉。
    """
    from app.agents.claude_code import _make_cli_env

    proc: Optional[asyncio.subprocess.Process] = None
    try:
        with tempfile.TemporaryDirectory(prefix="claude_auth_probe_") as cwd:
            proc = await asyncio.create_subprocess_exec(
                "claude", "-p", "Reply with exactly: OK",
                cwd=cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_make_cli_env(),
            )
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except FileNotFoundError:
        return False, "other", "claude CLI not found in PATH"
    except asyncio.TimeoutError:
        if proc is not None and proc.returncode is None:
            proc.kill()
        return False, "other", f"claude CLI probe timed out after {int(timeout)}s"

    output = (out_b or b"").decode("utf-8", "replace") + (err_b or b"").decode("utf-8", "replace")
    output = output.strip()
    detail = output[-300:]
    if proc.returncode == 0 and _classify(output) != "auth":
        return True, "", detail
    return False, _classify(output), detail or f"exit code {proc.returncode} (no output)"


async def check_claude_auth_and_alert() -> Dict[str, Any]:
    global _last_probe_at, _last_alerted_at, _consecutive_failures
    s = get_crashguard_settings()
    if not getattr(s, "claude_auth_probe_enabled", True):
        return {"skipped": "disabled"}

    now = datetime.utcnow()
    interval = timedelta(minutes=int(getattr(s, "claude_auth_probe_interval_minutes", 30) or 30))
    if _last_probe_at is not None and (now - _last_probe_at) < interval:
        return {"skipped": "interval"}
    _last_probe_at = now

    ok, kind, detail = await probe_claude_auth()
    if ok:
        if _consecutive_failures:
            logger.info("claude auth probe recovered after %d failure(s)", _consecutive_failures)
        _consecutive_failures = 0
        _last_alerted_at = None  # 恢复后下次再挂立即告警，不受上一轮节流影响
        return {"ok": True}

    _consecutive_failures += 1
    logger.warning(
        "claude auth probe failed: kind=%s consecutive=%d detail=%s",
        kind, _consecutive_failures, detail,
    )
    result: Dict[str, Any] = {
        "ok": False, "kind": kind, "consecutive": _consecutive_failures, "alerted": False,
    }
    if kind != "auth" and _consecutive_failures < 2:
        return result

    cooldown = timedelta(minutes=int(getattr(s, "claude_auth_alert_cooldown_minutes", 120) or 120))
    if _last_alerted_at is not None and (now - _last_alerted_at) < cooldown:
        result["throttled"] = True
        return result

    from app.crashguard.services import notify
    from app.crashguard.services.feishu_card import build_claude_auth_alert_card
    from app.services.im.feishu_to_slack import compile_card

    card_kw = dict(
        kind=kind, detail=detail, consecutive=_consecutive_failures,
        recovery_command=RECOVERY_COMMAND,
    )
    sent_ok = await notify.send_alert(
        build_claude_auth_alert_card(**card_kw),
        lambda: compile_card(build_claude_auth_alert_card(**card_kw, lang="en")),
        s=s, what="claude_auth_alert",
    )
    _last_alerted_at = now
    logger.info("claude_auth_alert fired: kind=%s sent=%s", kind, sent_ok)
    result.update(alerted=True, sent=sent_ok)
    return result
