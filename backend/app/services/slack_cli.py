"""Slack 传输层 —— httpx 直连 `https://slack.com/api/*`。

命名与分层对齐 `feishu_cli.py`：这一层只管"怎么把请求发出去"（认证、方法
语义、速率、重试、错误语义），不管"消息长什么样"。渲染在各模块自己的
`slack_*.py` 里（见 `docs/superpowers/specs/2026-09-23-jarvis-slack-notify-design.md`
的「卡片：不做中立 IR」）。

比飞书那层简单的地方：bot token（`xoxb-`）是静态的、不过期，所以**没有**
`_get_tenant_token()` 那套"用 app_id+secret 换 tenant_access_token + 过期缓存"
的机制；也没有飞书 `content: json.dumps({...})` 的双层编码；也**不需要
lark CLI**（`feishu_cli._run_cli` 那条 subprocess 路径），纯 HTTP。

下面四个常量/机制都是 2026-09-22 在 Apollo 那边用真 token 实测踩出来的，
不是照文档抄的（两个仓库刻意不共享代码，见设计文档「架构」一节）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("jarvis.slack_cli")

_BASE = "https://slack.com/api"

# ---------------------------------------------------------------------------
# ① GET 语义的方法 —— 只吃 query 参数，传 JSON body 会失败
#
# 实测：这类方法用 JSON body 调，Slack 返回
# `invalid_arguments: missing required field: channel`——看起来像少传了字段，
# 其实是**传法不对**。这个错误信息会把排查方向完全带偏（去检查参数名、
# 去怀疑权限），所以这里用显式清单而不是"试错回退"。
#
# 判据是 Slack 文档里该方法标的 HTTP method，不是"看起来像读还是像写"。
# ---------------------------------------------------------------------------
_GET_METHODS = frozenset({
    "auth.test",
    "conversations.info",
    "users.info",
    "users.lookupByEmail",
})

# ---------------------------------------------------------------------------
# ② 不算错误的 "error"
#
# Slack 用 `ok:false` + error code 表达一些**幂等成功**的情形。把它们当异常
# 抛出去会让正常路径长满 try/except。
# ---------------------------------------------------------------------------
_BENIGN_ERRORS = frozenset({
    "already_in_channel",
    "name_taken",
})


class SlackAPIError(RuntimeError):
    """Slack 返回 ok:false。`error` 是 Slack 的错误码，调用方可以按码分支。"""

    def __init__(self, method: str, error: str, detail: Any = None):
        self.method = method
        self.error = error
        self.detail = detail
        super().__init__(f"Slack API error ({method}): {error}"
                         + (f" | {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# ③ 按 channel 串行化
#
# `chat.postMessage` 的限制约 1 条/秒/**频道**。jarvis 的爆发点是早晚报：
# 一条主消息 + N 条 thread 回复（折叠段）全部打同一个频道，见设计文档
# 「thread 落地的两个硬约束」。
#
# 锁按 channel 分，不是全局一把：不同频道之间本来就不互相限速，全局锁会把
# crashguard 和 graygate 的发送串成一条队。
#
# 这个 dict 只增不减 —— channel 数量等于模块数（2 个）+ DM 数量（按人），
# 是有界的，不需要淘汰。
# ---------------------------------------------------------------------------
_channel_locks: Dict[str, asyncio.Lock] = {}


def _lock_for(channel: str) -> asyncio.Lock:
    lock = _channel_locks.get(channel)
    if lock is None:
        lock = asyncio.Lock()
        _channel_locks[channel] = lock
    return lock


# ---------------------------------------------------------------------------
# ④ 邮箱 → uid 的进程内缓存
#
# `users.lookupByEmail` 没有批量端点，一个人一次调用。
#
# 只缓存**命中**：查不到的邮箱通常是配置写错了，缓存失败结果会让改对之后要
# 等重启才生效。
# ---------------------------------------------------------------------------
_email_uid_cache: Dict[str, str] = {}

_MAX_RETRIES = 3


def _bot_token() -> str:
    from app.config import get_settings

    token = (get_settings().slack.bot_token or "").strip()
    if not token:
        raise SlackAPIError("(config)", "not_configured",
                            "SLACK_BOT_TOKEN 未配置——把某个模块切到 slack 前必须先配")
    return token


async def slack_api(method: str, **params: Any) -> Dict[str, Any]:
    """调一个 Slack Web API 方法。

    `ok:false` 抛 `SlackAPIError`，但 `_BENIGN_ERRORS` 里的错误码原样返回
    （调用方自己判断），因为它们表达的是"已经是目标状态"。

    429 按 `Retry-After` 退避重试；5xx 也重试（Slack 偶发 502/503）。
    其它 4xx 不重试——那是我们请求写错了，重试只是把同一个错误再发一遍。
    """
    token = _bot_token()
    url = f"{_BASE}/{method}"
    is_get = method in _GET_METHODS
    # None 值不要发出去：Slack 对 `thread_ts=null` 这类会报 invalid_arguments，
    # 而调用方写 `thread_ts=thread_ts or None` 是很自然的写法。
    payload = {k: v for k, v in params.items() if v is not None}

    last_err: Optional[Exception] = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                headers = {"Authorization": f"Bearer {token}"}
                if is_get:
                    resp = await http.get(url, params=payload, headers=headers)
                else:
                    headers["Content-Type"] = "application/json; charset=utf-8"
                    resp = await http.post(url, json=payload, headers=headers)

            if resp.status_code == 429:
                # Retry-After 是秒。Slack 一定会带这个头，但兜底给 1 秒——
                # 缺头时死等或立刻重试都不对。
                wait = float(resp.headers.get("Retry-After") or 1)
                logger.warning("Slack 429 on %s, retry after %.1fs (attempt %d/%d)",
                               method, wait, attempt, _MAX_RETRIES)
                await asyncio.sleep(wait)
                continue
            if resp.status_code >= 500:
                wait = min(2 ** attempt, 8)
                logger.warning("Slack %d on %s, retrying in %ds (attempt %d/%d)",
                               resp.status_code, method, wait, attempt, _MAX_RETRIES)
                await asyncio.sleep(wait)
                continue

            data = resp.json()
            if not data.get("ok"):
                err = data.get("error") or "unknown_error"
                if err in _BENIGN_ERRORS:
                    return data
                raise SlackAPIError(method, err,
                                    data.get("response_metadata") or data.get("errors"))
            return data
        except SlackAPIError:
            raise
        except (httpx.HTTPError, ValueError) as e:
            # 网络抖动 / 响应不是 JSON。这两类重试有意义。
            last_err = e
            wait = min(2 ** attempt, 8)
            logger.warning("Slack transport error on %s: %s — retrying in %ds", method, e, wait)
            await asyncio.sleep(wait)

    raise SlackAPIError(method, "max_retries_exceeded", str(last_err) if last_err else "429/5xx")


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------
async def post_message(
    channel: str,
    text: str = "",
    *,
    blocks: Optional[List[Dict[str, Any]]] = None,
    color: str = "",
    thread_ts: str = "",
) -> str:
    """发消息，返回这条消息的 `ts`（主消息的 ts 就是后续 thread 回复的锚点）。

    `text` 即使用了 blocks 也**必须传**——它是通知栏/邮件摘要/无障碍读屏看到
    的内容。只给 blocks 不给 text，手机推送会显示成一条空白通知。

    `color` 走 legacy attachment 的左侧色条。这是 Block Kit **唯一**能表达
    "整条消息的严重程度"的手段（飞书 card 的 `template: red/yellow/turquoise`
    没有对等物）。attachments 至今仍受支持，且是 Slack 自己的告警集成在用的
    形态；不要为了"只用新 API"把它换成在正文里插一个 🔴 emoji——色条在长列表
    里的扫读效果完全不同。

    `unfurl_links=False` 不是可选项：卡里全是 Datadog / GitHub / 前端深链，
    不关会把频道刷成一堆预览图。
    """
    kwargs: Dict[str, Any] = {
        "channel": channel,
        "text": text or " ",
        "thread_ts": thread_ts or None,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if color:
        # 有色条时 blocks 必须挂在 attachment 里面，否则色条只会出现在
        # attachment 那一段（正文 blocks 在色条**外面**），看起来像两条消息。
        kwargs["attachments"] = [{"color": color, "blocks": blocks or []}]
    else:
        kwargs["blocks"] = blocks

    async with _lock_for(channel):
        data = await slack_api("chat.postMessage", **kwargs)
    return data.get("ts", "")


# ---------------------------------------------------------------------------
# 用户 / DM
# ---------------------------------------------------------------------------
async def uid_for_email(email: str) -> str:
    """邮箱 → Slack uid。查不到返回空串（**不抛**）。

    查不到不是异常情形：配置里的邮箱可能写错、人可能还没加入工作区。调用方
    需要的是"这个人没解析出来"这个事实（该降级到别的目标 / 记一条 warning），
    而不是一个会把整轮告警打断的异常。
    """
    e = (email or "").strip().lower()
    if not e:
        return ""
    if e in _email_uid_cache:
        return _email_uid_cache[e]
    try:
        data = await slack_api("users.lookupByEmail", email=e)
    except SlackAPIError as err:
        if err.error != "users_not_found":
            logger.warning("users.lookupByEmail(%s) failed: %s", e, err.error)
        return ""
    uid = (data.get("user") or {}).get("id", "")
    if uid:
        _email_uid_cache[e] = uid
    return uid


async def open_dm(user_id: str) -> str:
    """开 1:1 会话，返回 `D...` 频道 id。

    纯文本 / blocks DM 其实可以直接 `post_message(channel="U...")`（Apollo
    那边实测可行），但保留这个函数有两个理由：附件上传的 `channel_id` 校验
    `^[CGDZ][A-Z0-9]{8,}$` 会直接拒掉 `U...`；以及 `D...` 是可以进
    `_lock_for()` 做串行化的稳定 key。
    """
    data = await slack_api("conversations.open", users=user_id)
    return (data.get("channel") or {}).get("id", "")


async def healthcheck() -> Dict[str, Any]:
    """**只做本地态检查，不打真实 API。**

    `/api/health` 是被频繁轮询的；而且飞书那边今天也没有健康检查，保持对称。
    """
    from app.config import get_settings

    s = get_settings()
    return {
        "bot_token_configured": bool((s.slack.bot_token or "").strip()),
        "email_cache_size": len(_email_uid_cache),
    }
