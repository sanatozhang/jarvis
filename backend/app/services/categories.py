"""Issue category single source of truth (backend).

Frontend 已经在 page/tracking/feedback 三处复用过 CATEGORIES_DATA，但 backend
之前没有等价的映射——通知、邮件、Slack 等场景需要把 `issues.category` 字段
（可能是稳定 key 如 "hardware"，也可能是历史的长中文串）翻译为可读 label。

入参兼容三种值：
- 稳定 key（新工单）：`"hardware"` / `"file_mgmt"` / ...
- 长中文串（老工单）：`"硬件交互（蓝牙连接，...）"`
- 未识别的值：原样返回，不抛错
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class CategoryOption:
    key: str        # stable identifier — new tickets store this in issues.category
    cn: str         # full Chinese label — legacy tickets store this in issues.category
    en: str         # full English label
    cn_short: str   # short Chinese label (badges / lists)
    en_short: str   # short English label


CATEGORY_OPTIONS = (
    CategoryOption(
        key="hardware",
        cn="硬件交互（蓝牙连接，固件升级，文件传输，音频播放，音频剪辑、音质不佳等）",
        en="Hardware (Bluetooth, firmware, file transfer, audio playback, clipping, sound quality)",
        cn_short="硬件交互",
        en_short="Hardware",
    ),
    CategoryOption(
        key="file_home",
        cn="文件首页（首页所有功能，列表显示，移动文件夹，批量转写，重命名，合并音频，删除文件，导入音频，时钟问题导致文件名不一致）",
        en="File Home (listing, folders, batch transcription, rename, merge, delete, import, clock issues)",
        cn_short="文件首页",
        en_short="File Home",
    ),
    CategoryOption(
        key="file_mgmt",
        cn="文件管理（转写，总结，文件编辑，分享导出，更多菜单，ASK Plaud，PCS）",
        en="File Management (transcription, summary, edit, share/export, ASK Plaud, PCS)",
        cn_short="文件管理",
        en_short="File Mgmt",
    ),
    CategoryOption(
        key="user_system",
        cn="用户系统与管理（账号登录注册，Onboarding，个人资料，偏好设置，app push 通知）",
        en="User System (login, onboarding, profile, preferences, push notifications)",
        cn_short="用户系统",
        en_short="User System",
    ),
    CategoryOption(
        key="monetization",
        cn="商业化（会员购买，会员转化）",
        en="Monetization (membership purchase, conversion)",
        cn_short="商业化",
        en_short="Monetization",
    ),
    CategoryOption(
        key="other",
        cn="其他通用模块（Autoflow，模版社区，Plaud WEB、集成、功能许愿池、推荐朋友、隐私与安全、帮助与支持等其他功能）",
        en="Other (Autoflow, templates, Plaud Web, integrations, wishlist, referral, privacy, help)",
        cn_short="其他",
        en_short="Other",
    ),
    CategoryOption(
        key="izyrec",
        cn="iZYREC 硬件问题",
        en="iZYREC Hardware Issues",
        cn_short="iZYREC",
        en_short="iZYREC",
    ),
)

_BY_KEY: Dict[str, CategoryOption] = {c.key: c for c in CATEGORY_OPTIONS}
_BY_CN: Dict[str, CategoryOption]  = {c.cn:  c for c in CATEGORY_OPTIONS}


def resolve_category(value: str) -> Optional[CategoryOption]:
    """Look up a category option by either stable key or legacy long-CN string."""
    if not value:
        return None
    return _BY_KEY.get(value) or _BY_CN.get(value)


def category_label(value: str, lang: str = "en", short: bool = False) -> str:
    """Translate `issues.category` to a human label in the given language.

    Falls back to the input value as-is when unrecognized — keeps unknown
    categories visible rather than silently dropping them.
    """
    opt = resolve_category(value)
    if opt is None:
        return value or ""
    if lang == "en":
        return opt.en_short if short else opt.en
    return opt.cn_short if short else opt.cn
