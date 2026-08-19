"""
Weekly on-call greeting: a bilingual (zh/en) Feishu message @-mentioning the
current week's on-call members, posted to a fixed group chat every Monday
09:00 Asia/Shanghai.

Why this exists: Jarvis has oncall schedule data (see `app/db/database.py`
`get_current_oncall()`) but nothing proactively told the on-call person "you
are on duty this week" — people had to go check the `/oncall` page
themselves, so escalated tickets piled up because the on-call person simply
didn't know. This module closes that gap. It runs one hour after the
(now-disabled) old Feishu-table sync used to run, so historically it always
read the freshest schedule; now it just reads whatever's configured on the
Jarvis `/oncall` page directly.

Idempotency guard's honest limits (do not remove this comment — read it
before touching `_LAST_SENT_KEY` semantics):

    `_LAST_SENT_KEY` in `oncall_config` prevents a *double-send within the
    same process/DB* — e.g. a manual verification call racing the scheduled
    Monday run, or multiple processes sharing the same SQLite file. It does
    **not** prevent a double-send from an independent rogue instance with
    its own separate SQLite file (that class of bug is guarded elsewhere by
    a kill-switch env var that defaults off, handled in a different task).
    The check-then-set here is also **not atomic** — a same-millisecond race
    between two processes could both pass the "already sent?" check before
    either writes the marker. We accept this: the trade-off is a
    theoretical "might miss a week if the process dies between sending and
    writing the marker" in exchange for the much more important "must never
    spam the whole group twice in one week."
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db import database as db
from app.services.feishu_cli import _emails_to_open_id_map, send_message, get_chat_info

logger = logging.getLogger("jarvis.oncall_weekly_greeting")

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
GREETING_HOUR_LOCAL = 9
_LAST_SENT_KEY = "weekly_greeting_last_sent_week"


def _seconds_until_next_monday_9am(now_local: datetime) -> float:
    # 复制自 oncall_feishu_sync._seconds_until_next_monday_8am，
    # 第三次出现时再抽公共工具。
    target = now_local.replace(hour=GREETING_HOUR_LOCAL, minute=0, second=0, microsecond=0)
    days_ahead = (0 - now_local.weekday()) % 7  # Monday == 0
    if days_ahead == 0 and now_local >= target:
        days_ahead = 7
    target = target + timedelta(days=days_ahead)
    return (target - now_local).total_seconds()


def _week_range(today: date) -> Tuple[date, date]:
    """Return (monday, sunday) of the calendar week containing `today`."""
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _render_greeting(
    members: List[str],
    id_map: Dict[str, str],
    week_start: date,
    week_end: date,
    base_url: str,
) -> Tuple[str, List[str]]:
    """Render the bilingual greeting message.

    Critical: iterate `members` (not `id_map.items()`) — otherwise anyone
    whose email failed to resolve to an open_id silently vanishes from the
    message instead of appearing as a plain-text `@name` fallback. That bug
    exists in `escalation_reminder.py`'s `<at>` construction; do not copy it.
    """
    base_url = (base_url or "").rstrip("/")
    if not base_url:
        logger.warning(
            "oncall_weekly_greeting: frontend_base_url resolved empty; omitting "
            "值班看板/Oncall board line from this week's greeting"
        )

    mention_parts: List[str] = []
    unresolved: List[str] = []
    for email in members:
        oid = id_map.get(email)
        localpart = email.split("@")[0]
        if oid:
            mention_parts.append(f'<at user_id="{oid}">{localpart}</at>')
        else:
            mention_parts.append(f"@{localpart}")
            unresolved.append(email)

    lines = [
        f"📅 本周值班提醒 / Weekly Oncall Reminder（{week_start:%Y-%m-%d} ~ {week_end:%Y-%m-%d}）",
        "",
        " ".join(mention_parts),
        "本周由你们值周，请关注本周工单群的转交工单，及时跟进并在 Appllo 标记完成。",
        "You're on oncall duty this week — please follow up on escalated tickets",
        "in this week's ticket group and mark them complete in Appllo.",
    ]

    if unresolved:
        lines.append("")
        lines.append("⚠️ 以下同学的飞书账号未解析出来、@ 不到，请人工同步 / could not be @-mentioned:")
        lines.append(", ".join(unresolved))

    if base_url:
        lines.append("")
        lines.append(f"值班看板 / Oncall board: {base_url}/oncall")

    return "\n".join(lines), unresolved


async def _notify_admin_if_real_run(dry_run: bool, to_email: str, text: str) -> None:
    """Best-effort admin DM for config problems — never fires during a
    dry_run preview or a to_email verification ping (neither is the real
    Monday send, so neither should page anyone about broken config)."""
    if dry_run or to_email:
        return
    try:
        await send_message(email=get_settings().feedback_recipient, text=text)
    except Exception as e:
        logger.warning("oncall_weekly_greeting: failed to notify admin: %s", e)


async def send_weekly_greeting(
    *,
    today: Optional[date] = None,
    dry_run: bool = False,
    force: bool = False,
    to_email: str = "",
    max_attempts: int = 3,
    retry_delay_s: float = 300.0,
) -> Dict[str, Any]:
    """Entry point: resolve this week's on-call members, render the
    bilingual greeting, and send it to the configured Feishu group (or to
    `to_email` for a verification ping). See module docstring for the
    idempotency guard's honest limits.

    `today`/`dry_run` are injectable for tests, matching
    `oncall_feishu_sync.sync_oncall_from_feishu`'s pattern.
    """
    resolved_today = today or datetime.now(SHANGHAI_TZ).date()
    week_start, week_end = _week_range(resolved_today)

    def _skip(reason: str) -> Dict[str, Any]:
        return {
            "sent": False,
            "skipped": True,
            "reason": reason,
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "members": [],
            "unresolved": [],
            "text": "",
            "target": "",
            "dry_run": dry_run,
        }

    # Step 2: explicit start_date check — defense in depth. get_current_oncall()
    # already returns [] when start_date is unset/invalid (Task 1's DB-layer
    # fix), so this module would technically still be correct without this
    # check. But this module must never depend on an implementation detail of
    # a function it merely calls, so it checks the thing it actually cares
    # about directly.
    start_date_str = await db.get_oncall_config("start_date", "")
    if not start_date_str:
        await _notify_admin_if_real_run(
            dry_run, to_email,
            "⚠️ 值班值周提醒已跳过：oncall start_date 未配置，本周未发送值班提醒。\n"
            "Weekly oncall greeting skipped: start_date is not configured.",
        )
        return _skip("no_start_date")

    # Step 3: no members resolved for the current rotation.
    members = await db.get_current_oncall()
    if not members:
        await _notify_admin_if_real_run(
            dry_run, to_email,
            "⚠️ 值班值周提醒已跳过：当前值班组解析为空，本周未发送值班提醒。\n"
            "Weekly oncall greeting skipped: no oncall members resolved for this week.",
        )
        return _skip("no_oncall_members")

    # Step 4: idempotency guard — see module docstring for its honest limits.
    if not force and not dry_run and not to_email:
        last_sent = await db.get_oncall_config(_LAST_SENT_KEY, "")
        if last_sent == week_start.isoformat():
            return _skip("already_sent_this_week")

    # Step 5: chat destination must exist unless we're pinging to_email instead.
    chat_id = get_settings().feishu.oncall_greeting_chat_id
    if not chat_id and not to_email:
        return _skip("no_chat_id")

    # Step 6: resolve emails -> open_ids even during dry_run — read-only call,
    # and the whole point of dry_run is letting someone verify resolution
    # without actually sending anything.
    id_map = await _emails_to_open_id_map(members)

    # Step 7: render.
    text, unresolved = _render_greeting(
        members, id_map, week_start, week_end, get_settings().frontend_base_url,
    )

    # Step 8: dry_run — preview only, never send, never write the marker.
    if dry_run:
        chat_name = ""
        if chat_id:
            try:
                chat_info = await get_chat_info(chat_id)
                chat_name = (chat_info or {}).get("name", "")
            except Exception:
                chat_name = ""
        await db.log_event(
            "oncall_weekly_greeting",
            detail={
                "week_start": week_start.isoformat(),
                "members": members,
                "unresolved": unresolved,
                "sent": False,
                "dry_run": True,
            },
        )
        return {
            "sent": False,
            "skipped": False,
            "reason": "",
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "members": members,
            "unresolved": unresolved,
            "text": text,
            "target": to_email or chat_id,
            "dry_run": True,
            "chat_name": chat_name,
        }

    # Step 9: bounded retry.
    ok = False
    attempts = 0
    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        if to_email:
            ok = await send_message(email=to_email, text=text)
        else:
            ok = await send_message(chat_id=chat_id, text=text)
        if ok:
            break
        if attempt < max_attempts:
            await asyncio.sleep(retry_delay_s)

    # Step 9b: total failure on a *real* send (never for a to_email verification
    # ping — that's the admin's own manual check, and paging them about their
    # own manual check failing would be redundant). Unlike
    # `_notify_admin_if_real_run`, this isn't gated on `dry_run` — dry_run
    # already returned at Step 8, so by this point we're always in a real,
    # non-dry_run attempt; the only thing left to exclude is `to_email`.
    if not ok and not to_email:
        logger.error(
            "oncall_weekly_greeting: failed to send weekly greeting to chat_id=%s after %d attempt(s)",
            chat_id, attempts,
        )
        try:
            await send_message(
                email=get_settings().feedback_recipient,
                text=(
                    f"❌ 值班值周提醒发送失败：已重试 {attempts} 次仍失败，目标群 chat_id={chat_id}，"
                    "本周群里没有人收到值班提醒，请检查飞书机器人/群配置。\n"
                    f"Weekly oncall greeting failed to send after {attempts} attempt(s) to "
                    f"chat_id={chat_id}; no one in the group was notified this week."
                ),
            )
        except Exception as e:
            logger.warning("oncall_weekly_greeting: failed to notify admin about send failure: %s", e)

    # Step 10: write the idempotency marker only on a successful *real* group
    # send. A to_email call is a verification ping to yourself, not the real
    # weekly send — writing the marker there would silently suppress Monday's
    # actual group message.
    if ok and not to_email:
        await db.set_oncall_config(_LAST_SENT_KEY, week_start.isoformat())

    # Step 11: always logged, success or failure.
    await db.log_event(
        "oncall_weekly_greeting",
        detail={
            "week_start": week_start.isoformat(),
            "members": members,
            "unresolved": unresolved,
            "sent": ok,
            "attempts": attempts,
            "target": to_email or chat_id,
            "dry_run": False,
            "force": force,
        },
    )

    # Step 12.
    return {
        "sent": ok,
        "skipped": False,
        "reason": "" if ok else "send_failed",
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "members": members,
        "unresolved": unresolved,
        "text": text,
        "target": to_email or chat_id,
        "dry_run": dry_run,
    }


async def oncall_weekly_greeting_loop():
    """Background loop: fire every Monday at 09:00 Asia/Shanghai.

    `send_weekly_greeting` never raises on a routine send failure (it returns
    `sent=False` instead), so the `except Exception` below only catches
    genuine bugs (DB errors etc.) — not routine send failures. Don't add
    extra try/except inside the loop body beyond what's already inside
    `send_weekly_greeting`.
    """
    while True:
        try:
            now_local = datetime.now(SHANGHAI_TZ)
            wait_s = _seconds_until_next_monday_9am(now_local)
            logger.info("Next weekly oncall greeting in %.1f hours (Monday 09:00 SH)", wait_s / 3600)
            await asyncio.sleep(wait_s)
            await send_weekly_greeting()
            await asyncio.sleep(60)  # 避免本次跑得飞快导致同一分钟内重复触发
        except asyncio.CancelledError:
            logger.info("Weekly oncall greeting loop cancelled")
            return
        except Exception as e:
            logger.error("Weekly oncall greeting loop error (retry in 1h): %s", e, exc_info=True)
            await asyncio.sleep(3600)
