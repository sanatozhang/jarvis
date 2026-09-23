"""通知渠道开关 —— DB override + 运行中生效 + 启动回灌。

## 为什么不是改 yaml / env

`notify_provider` 配在三个模块各自的 `config.yaml` / env 里，那是**部署态**
配置：改一次要改文件 + 重启。而切换渠道是个需要来回试的动作（切过去看看
效果、不对再切回来），走 DB override + 热生效，跟
`api/settings.py::AGENT_OVERRIDE_KEY` 是同一个范式。

优先级：**env > DB override > yaml > 默认**。env 仍然最高——一台机器想被钉死
在飞书上（比如还没进 Slack 频道的老部署），配个 env 就不会被界面上的开关
影响。

## 三件套

1. `PUT` 写 DB
2. 同时 **mutate 运行中的 settings 单例**（三个模块的 `get_*_settings()` 都是
   `lru_cache` 单例，改属性即刻生效，不用重启）
3. `main.py` 启动期调 `apply_notify_overrides_from_db()` 回灌

漏掉第 3 步的后果是"界面上切了、重启后偷偷弹回去"——`api/settings.py` 顶部
那段注释记的就是这个 bug（fb_f57ddda7d0）。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

from app.db import database as db
from app.services.im import implemented_providers

logger = logging.getLogger("jarvis.notify_switch")

NOTIFY_OVERRIDE_KEY = "notify_provider_overrides"

# 模块名 → (settings getter 的 import 路径, 群/频道字段名)
#
# 群字段名两边不一样是历史原因（crashguard/coreguard 叫 feishu_target_chat_id，
# graygate 叫 feishu_chat_id），不在这次迁移里统一——改名要动各自的 env 和
# 已部署的机器，收益只是好看。
_MODULES: Dict[str, Dict[str, str]] = {
    "crashguard": {
        "getter": "app.crashguard.config:get_crashguard_settings",
        "feishu_channel_attr": "feishu_target_chat_id",
        "label": "Crashguard（早晚报 / 小时级告警 / 核心指标 / 任务健康 / 符号表）",
    },
    "coreguard": {
        "getter": "app.coreguard.config:get_coreguard_settings",
        "feishu_channel_attr": "feishu_target_chat_id",
        "label": "Coreguard（核心指标异常告警）",
    },
    "graygate": {
        "getter": "app.graygate.config:get_graygate_settings",
        "feishu_channel_attr": "feishu_chat_id",
        "label": "Graygate（灰度期每日指标）",
    },
}


def modules() -> List[str]:
    return list(_MODULES)


def _settings_for(module: str):
    spec = _MODULES[module]
    mod_path, fn_name = spec["getter"].split(":")
    mod = __import__(mod_path, fromlist=[fn_name])
    return getattr(mod, fn_name)()


def _env_pinned(module: str) -> bool:
    """这个模块的 provider 是不是被 env 钉死了。

    被钉死时界面上要禁用开关并说明原因，而不是"点了没反应"——后者会让人
    以为是 bug，然后去翻日志。
    """
    return bool(os.environ.get(f"{module.upper()}_NOTIFY_PROVIDER"))


async def _load_overrides() -> Dict[str, Any]:
    raw = await db.get_oncall_config(NOTIFY_OVERRIDE_KEY, "")
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        logger.warning("notify override 不是合法 JSON，忽略：%r", raw)
        return {}


def status_for(module: str) -> Dict[str, Any]:
    """一个模块的「体检行」。

    ⚠️ **这张表不是装饰。** 切到一个没配频道的渠道**不会报错**：
    发送方 `_send()` 只会记一行 warning 然后返回 False，而上游大多把
    False 吞成"这次没发成功"。表现是"一切正常，只是没人收到告警"，而且
    要等下一次告警触发才发生。所以切换前必须能一眼看到"目标渠道配齐了没"。
    """
    from app.config import get_settings

    spec = _MODULES[module]
    s = _settings_for(module)
    provider = (getattr(s, "notify_provider", "") or "feishu").strip().lower()

    feishu_channel = getattr(s, spec["feishu_channel_attr"], "") or ""
    slack_channel = getattr(s, "slack_channel", "") or ""
    # 告警走点对点的那条链在两个渠道下都用邮箱，所以它不影响 readiness
    alert_email = (getattr(s, "feishu_alert_email", "")
                   or getattr(s, "alert_email", "")
                   or getattr(s, "feishu_target_email", "") or "")
    token_ok = bool((get_settings().slack.bot_token or "").strip())

    return {
        "module": module,
        "label": spec["label"],
        "provider": provider,
        "env_pinned": _env_pinned(module),
        "feishu_channel": feishu_channel,
        "slack_channel": slack_channel,
        "alert_email": alert_email,
        "ready": {
            # 飞书：有群或有兜底邮箱就能发
            "feishu": bool(feishu_channel or alert_email),
            # Slack：**还要有 token**。只配频道不配 token 是最常见的半成品状态
            "slack": bool(token_ok and (slack_channel or alert_email)),
        },
        "slack_token_configured": token_ok,
    }


def status() -> Dict[str, Any]:
    return {
        "implemented": list(implemented_providers()),
        "slack_token_configured": bool(status_for("crashguard")["slack_token_configured"]),
        "modules": [status_for(m) for m in _MODULES],
    }


async def set_provider(module: str, provider: str,
                       slack_channel: str | None = None) -> Dict[str, Any]:
    """切换一个模块的通知渠道。写 DB + 立刻改运行中的单例。"""
    if module not in _MODULES:
        raise ValueError(f"未知模块 {module!r}；可选：{modules()}")
    prov = (provider or "").strip().lower()
    if prov not in implemented_providers():
        raise ValueError(f"未实现的渠道 {provider!r}；可选：{list(implemented_providers())}")

    overrides = await _load_overrides()
    entry = dict(overrides.get(module) or {})
    entry["provider"] = prov
    if slack_channel is not None:
        entry["slack_channel"] = slack_channel.strip()
    overrides[module] = entry
    await db.set_oncall_config(NOTIFY_OVERRIDE_KEY,
                               json.dumps(overrides, ensure_ascii=False))

    _apply_one(module, entry)
    logger.info("notify provider 切换：%s → %s（slack_channel=%r）",
                module, prov, entry.get("slack_channel"))
    return status_for(module)


def _apply_one(module: str, entry: Dict[str, Any]) -> None:
    """把一条 override 灌进运行中的 settings 单例。

    env 优先：被 env 钉死的模块跳过，否则界面上的开关会"看起来生效了"，
    而下次重启 env 又把它顶回去——那种不一致比开关直接禁用难查得多。
    """
    if _env_pinned(module):
        logger.info("notify override：%s 被 env 钉死，跳过 DB override", module)
        return
    s = _settings_for(module)
    if entry.get("provider"):
        s.notify_provider = entry["provider"]
    if entry.get("slack_channel") is not None:
        s.slack_channel = entry["slack_channel"]


async def apply_notify_overrides_from_db() -> None:
    """启动期回灌。`main.py` 的 lifespan 调它。

    漏掉这一步的表现是"界面上切了、重启后偷偷弹回 yaml 的值"。
    """
    overrides = await _load_overrides()
    for module, entry in overrides.items():
        if module not in _MODULES:
            logger.warning("notify override 里有未知模块 %r，跳过", module)
            continue
        try:
            _apply_one(module, entry)
        except Exception:
            logger.exception("notify override 回灌失败：%s", module)
    if overrides:
        logger.info("notify override 已回灌：%s",
                    {m: e.get("provider") for m, e in overrides.items()})
