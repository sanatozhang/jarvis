"""
Analytics API: event tracking + dashboard data.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.db import database as db

logger = logging.getLogger("jarvis.api.analytics")
router = APIRouter()


class TrackEventRequest(BaseModel):
    event_type: str       # page_visit, button_click, etc.
    issue_id: str = ""
    username: str = ""
    detail: dict = {}


@router.post("/track")
async def track_event(req: TrackEventRequest):
    """Track a frontend event (page visit, button click, etc.)."""
    await db.log_event(
        event_type=req.event_type,
        issue_id=req.issue_id,
        username=req.username,
        detail=req.detail,
    )
    return {"status": "ok"}


@router.get("/dashboard")
async def get_dashboard(
    days: int = Query(7, ge=1, le=3650, description="Number of days to look back"),
):
    """Get analytics dashboard data."""
    date_to = datetime.utcnow().strftime("%Y-%m-%d")
    date_from = (datetime.utcnow() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    data = await db.get_analytics(date_from, date_to)

    # Calculate value metrics
    success = data["successful_analyses"]
    failed = data["failed_analyses"]
    completed = success + failed  # total finished analyses (denominator for success rate)
    total = max(data["total_analyses"], completed)  # use whichever is larger (start events may be missing for old data)
    avg_min = data["avg_analysis_duration_min"]

    manual_time_min = total * 30
    ai_time_min = total * avg_min if avg_min else total * 5
    time_saved_min = max(0, manual_time_min - ai_time_min)
    time_saved_hours = round(time_saved_min / 60, 1)

    data["value_metrics"] = {
        "time_saved_hours": time_saved_hours,
        "time_saved_per_ticket_min": round(30 - avg_min, 1) if avg_min else 25,
        "success_rate": round(success / completed * 100, 1) if completed else 0,
        "estimated_manual_hours": round(manual_time_min / 60, 1),
        "estimated_ai_hours": round(ai_time_min / 60, 1),
    }

    return data


@router.get("/problem-types")
async def get_problem_type_stats(
    days: int = Query(30, ge=1, le=3650, description="Number of days to look back"),
):
    """Get problem type distribution, daily trend, and top 10."""
    date_to = datetime.utcnow().strftime("%Y-%m-%d")
    date_from = (datetime.utcnow() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    return await db.get_problem_type_stats(date_from, date_to)


@router.get("/classification-stats")
async def get_classification_stats(
    days: int = Query(30, ge=1, le=3650, description="Number of days to look back"),
):
    """Get problem category + device type classification stats (pie chart data)."""
    date_to = datetime.utcnow().strftime("%Y-%m-%d")
    date_from = (datetime.utcnow() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    return await db.get_classification_stats(date_from, date_to)


@router.post("/backfill-classifications")
async def backfill_classifications(
    limit: int = Query(500, ge=1, le=5000, description="Max records to process"),
):
    """Backfill problem_categories for old analyses using keyword mapping."""
    records = await db.get_analyses_for_backfill(limit=limit)
    if not records:
        return {"status": "ok", "updated": 0, "message": "No records need backfill"}

    updated = 0
    for rec in records:
        categories = _map_problem_type_to_categories(rec["problem_type"], rec.get("root_cause", ""))
        device_type = rec.get("device_type", "") or ""
        if categories:
            await db.update_analysis_classification(rec["id"], categories, device_type)
            updated += 1

    return {"status": "ok", "updated": updated, "total_candidates": len(records)}


def _map_problem_type_to_categories(problem_type: str, root_cause: str = "") -> list:
    """Map a free-text problem_type to structured categories using keyword matching."""
    text = f"{problem_type} {root_cause}".lower()
    categories = []

    _MAPPING = [
        ("蓝牙连接", [
            ("搜索不到设备", ["搜索不到", "搜不到", "找不到设备", "scan", "没有搜索", "nrf.*没有搜索"]),
            ("Token不匹配", ["token", "token不匹配", "token未清空"]),
            ("设备连接无响应", ["连接无响应", "连接超时", "connect.*timeout"]),
            ("配对失败", ["配对失败", "pair", "bonding"]),
            ("蓝牙不连接", ["蓝牙不连接", "蓝牙连接", "bluetooth", "ble"]),
        ]),
        ("固件升级", [
            ("升级失败", ["升级失败", "ota.*fail", "firmware.*fail"]),
            ("升级后搜索不到设备", ["升级.*搜索不到", "升级.*搜不到", "升级后找不到"]),
            ("OTA传输中断", ["ota.*中断", "ota.*断开"]),
            ("固件升级故障", ["固件升级", "固件", "firmware", "ota"]),
        ]),
        ("时间戳问题", [
            ("时钟偏移", ["时钟偏移", "clock.*drift", "时钟问题"]),
            ("文件名时间不一致", ["时间不一致", "文件名.*时间", "timestamp"]),
            ("时间戳问题", ["时间戳", "时间戳问题"]),
        ]),
        ("录音问题", [
            ("录音空白", ["录音空白", "录音为空", "empty.*recording"]),
            ("录音丢失", ["录音丢失", "录音缺失", "recording.*missing", "recording.*lost"]),
            ("录音文件损坏", ["录音.*损坏", "文件损坏", "corrupt"]),
            ("录音故障", ["录音故障", "录音", "recording"]),
        ]),
        ("设备故障", [
            ("硬件故障", ["硬件故障", "hardware"]),
            ("无法开机", ["无法开机", "不开机", "power"]),
            ("WiFi故障", ["wifi", "wi-fi"]),
            ("设备故障", ["设备故障", "设备异常", "device.*fault"]),
        ]),
        ("文件传输", [
            ("传输失败", ["传输失败", "transfer.*fail"]),
            ("USB传输异常", ["usb", "usb传输"]),
            ("文件传输", ["文件传输", "传输", "transfer"]),
        ]),
        ("云同步", [
            ("同步失败", ["同步失败", "sync.*fail"]),
            ("声纹上云失败", ["声纹上云", "voiceprint", "speaker.*cloud"]),
            ("云同步", ["云同步", "cloud.*sync", "同步"]),
        ]),
        ("转写问题", [
            ("语言识别错误", ["语言识别", "language.*recognition"]),
            ("转写失败", ["转写失败", "transcri.*fail"]),
            ("转写问题", ["转写", "transcri"]),
        ]),
        ("软件bug", [
            ("App崩溃", ["崩溃", "crash", "flutter.*crash"]),
            ("iOS兼容问题", ["ios", "iphone"]),
            ("Android兼容问题", ["android"]),
            ("前端接口异常", ["前端接口", "api.*error", "接口"]),
            ("LLM输出不稳定", ["llm", "输出不稳定"]),
            ("软件bug", ["软件bug", "bug"]),
        ]),
        ("用户操作", [
            ("用户误操作", ["用户误操作", "误操作"]),
            ("功能使用疑问", ["使用疑问", "怎么用", "如何"]),
            ("产品交互优化", ["产品交互", "交互优化", "体验"]),
            ("用户操作", ["用户操作", "操作问题"]),
        ]),
        ("会员与支付", [
            ("购买失败", ["购买失败", "purchase.*fail"]),
            ("会员状态异常", ["会员.*异常", "会员.*状态"]),
            ("会员与支付", ["会员", "支付", "payment", "membership"]),
        ]),
    ]

    import re
    matched_cats = set()
    for category, subcats in _MAPPING:
        for subcat_name, keywords in subcats:
            for kw in keywords:
                if re.search(kw, text):
                    key = f"{category}|{subcat_name}"
                    if key not in matched_cats:
                        matched_cats.add(key)
                        categories.append({"category": category, "subcategory": subcat_name})
                    break

    # Deduplicate: keep only the most specific subcategory per category
    seen_categories = {}
    for c in categories:
        cat = c["category"]
        if cat not in seen_categories:
            seen_categories[cat] = c
        elif c["subcategory"] != cat:  # prefer specific subcategory over generic
            seen_categories[cat] = c

    result = list(seen_categories.values())
    if not result and problem_type:
        result = [{"category": "其他", "subcategory": problem_type}]

    return result


@router.get("/rule-accuracy")
async def get_rule_accuracy(
    days: int = Query(30, ge=1, le=3650, description="Number of days to look back"),
):
    """Get rule accuracy statistics."""
    from app.services.rule_accuracy import get_rule_accuracy_stats
    return await get_rule_accuracy_stats(days=days)
