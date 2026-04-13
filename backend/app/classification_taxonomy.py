"""
Problem classification taxonomy for AI analysis results.

- CLASSIFICATION_TAXONOMY: written to workspace context/ for AI reference
- classify_problem(): keyword-based mapping, called by backend on save_analysis
  and by backfill endpoint for old data. Single source of truth.
"""

import re
from typing import List, Dict

CLASSIFICATION_TAXONOMY = {
    "version": "1.0",
    "instructions": "一个问题可属于多个分类。从 categories 中选择最匹配的一级 category，"
                    "二级 subcategory 可从示例中选择或自行新建。如无合适分类可新建。",
    "categories": [
        {
            "category": "蓝牙连接",
            "subcategories": [
                "搜索不到设备", "Token不匹配", "本地Token未清空",
                "设备连接无响应", "配对失败", "连接断开", "蓝牙不连接",
            ],
        },
        {
            "category": "固件升级",
            "subcategories": [
                "升级失败", "升级后搜索不到设备", "OTA传输中断", "版本回退",
            ],
        },
        {
            "category": "时间戳问题",
            "subcategories": [
                "时钟偏移", "文件名时间不一致", "时区错误",
            ],
        },
        {
            "category": "录音问题",
            "subcategories": [
                "录音空白", "录音丢失", "录音文件损坏", "录音时长异常",
            ],
        },
        {
            "category": "设备故障",
            "subcategories": [
                "硬件故障", "无法开机", "按键无响应", "充电异常", "WiFi故障",
            ],
        },
        {
            "category": "文件传输",
            "subcategories": [
                "传输失败", "传输中断", "文件损坏", "USB传输异常",
            ],
        },
        {
            "category": "云同步",
            "subcategories": [
                "同步失败", "声纹上云失败", "数据不一致",
            ],
        },
        {
            "category": "转写问题",
            "subcategories": [
                "语言识别错误", "转写失败", "转写结果不准确",
            ],
        },
        {
            "category": "软件bug",
            "subcategories": [
                "前端接口异常", "App崩溃", "LLM输出不稳定",
                "iOS兼容问题", "Android兼容问题",
            ],
        },
        {
            "category": "用户操作",
            "subcategories": [
                "用户误操作", "功能使用疑问", "产品交互优化",
            ],
        },
        {
            "category": "会员与支付",
            "subcategories": [
                "购买失败", "会员状态异常", "支付未到账",
            ],
        },
        {
            "category": "其他",
            "subcategories": [
                "无法归类",
            ],
        },
    ],
    "device_types": ["Note", "Note Pin", "Note Pro", "NotePin 2", "iZYREC"],
}


# ---------------------------------------------------------------------------
# Keyword → category mapping (used by backend, NOT by AI)
# ---------------------------------------------------------------------------
_CATEGORY_MAPPING = [
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


def classify_problem(problem_type: str, root_cause: str = "") -> List[Dict[str, str]]:
    """Map free-text problem_type + root_cause to structured categories.

    Called by backend on every save_analysis (zero AI cost).
    Also used by backfill endpoint for old data.
    """
    text = f"{problem_type} {root_cause}".lower()
    categories = []
    matched_cats: set = set()

    for category, subcats in _CATEGORY_MAPPING:
        for subcat_name, keywords in subcats:
            for kw in keywords:
                if re.search(kw, text):
                    key = f"{category}|{subcat_name}"
                    if key not in matched_cats:
                        matched_cats.add(key)
                        categories.append({"category": category, "subcategory": subcat_name})
                    break

    # Keep only most specific subcategory per category
    seen: Dict[str, Dict[str, str]] = {}
    for c in categories:
        cat = c["category"]
        if cat not in seen:
            seen[cat] = c
        elif c["subcategory"] != cat:  # prefer specific over generic
            seen[cat] = c

    result = list(seen.values())
    if not result and problem_type:
        result = [{"category": "其他", "subcategory": problem_type}]
    return result
