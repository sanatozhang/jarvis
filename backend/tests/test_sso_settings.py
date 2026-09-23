"""SSO settings parsing & defaults."""
from __future__ import annotations

from app.config import SSOSettings


def test_sso_enabled_by_default(monkeypatch):
    """**默认开**，不是默认关。

    2026-05-20（commit 2de7c02「SSO default true」）刻意把默认值从 False 翻成
    True：漏配 `ENABLE_SSO` 的部署应该是"进不去"，而不是"谁都进得去"。
    这条测试当时没跟着改，之后一直红着（断言的是已经作废的默认值）。

    `_env_file=None` 关掉 `.env` 读取、再清掉进程环境里的同名变量，
    这样断言的才是**字段默认值**，而不是本机配置。不清的话本机
    `.env` 里的 `ENABLE_SSO=false` 会让它"碰巧通过"。
    """
    # 清**所有** SSO_* / ENABLE_SSO / ADMIN_EMAILS，不是挑几个。
    # 单独跑这个文件时进程环境是干净的，全量跑时前面的用例（以及 app.config
    # 导入时的 dotenv 加载）会把真实值灌进 os.environ —— 只清几个的话这条
    # 测试会"单跑绿、全量红"，比一直红更难查。
    import os

    for key in [k for k in os.environ
                if k.startswith("SSO_") or k in ("ENABLE_SSO", "ENABLE_GMAIL_SSO",
                                                 "ADMIN_EMAILS",
                                                 # ↓ 见 test_sso_silently_accepts_
                                                 # bitable_credentials 的说明
                                                 "FEISHU_APP_ID", "FEISHU_APP_SECRET")]:
        monkeypatch.delenv(key, raising=False)
    s = SSOSettings(_env_file=None)
    assert s.enabled is True
    assert s.cookie_days == 365
    # 顺带钉住：默认不带任何凭证。带了就说明 .env 又漏进来了
    # ——这条测试失败时 pytest 会把整个 SSOSettings repr 打出来，
    # 里面是真的 app_secret。
    assert s.feishu_app_id == ""
    assert s.feishu_app_secret == ""


def test_sso_parses_allowed_domains_csv(monkeypatch):
    monkeypatch.setenv("SSO_ALLOWED_DOMAINS", "plaud.ai,foo.com")
    s = SSOSettings()
    assert s.allowed_domains == ["plaud.ai", "foo.com"]


def test_sso_parses_admin_emails_csv(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "a@plaud.ai, b@plaud.ai")
    s = SSOSettings()
    assert s.admin_emails == ["a@plaud.ai", "b@plaud.ai"]


def test_sso_parses_exempt_paths_csv(monkeypatch):
    monkeypatch.setenv("SSO_EXEMPT_PATHS", "/api/health,/api/v1/")
    s = SSOSettings()
    assert s.exempt_paths == ["/api/health", "/api/v1/"]


def test_sso_silently_accepts_bitable_credentials(monkeypatch):
    """⚠️ 已知隐患，这里**钉住现状**而不是断言它是对的。

    `SSOSettings` 带 `populate_by_name=True`，于是除了 alias
    （`SSO_FEISHU_APP_ID`）之外，**字段名本身**也会被当成 env 名去查——
    也就是 `FEISHU_APP_ID`。而 `FEISHU_APP_ID` 是**另一个完全不同的东西**：
    多维表格 / IM 机器人那个 app（见 `class FeishuSettings`）。

    后果：一台只配了 `FEISHU_APP_ID`、没配 `SSO_FEISHU_APP_ID` 的机器，
    SSO 会**静默**拿机器人 app 的凭证去做 OAuth。生产上两个都配了所以
    被掩盖，但这是个真实的脚坑。

    没有顺手修，因为修法有风险：如果现在有部署正好靠这个回落在跑，
    关掉它会让那台机器**登不进去**。要修得先确认所有部署都显式配了
    `SSO_FEISHU_APP_ID`。
    """
    import os

    for key in [k for k in os.environ if k.startswith("SSO_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FEISHU_APP_ID", "cli_bitable_bot")

    s = SSOSettings(_env_file=None)
    assert s.feishu_app_id == "cli_bitable_bot", (
        "如果这条挂了，说明有人修掉了这个回落——好事，把这个测试改成断言 '' "
        "并在 DEPLOY.md 里提醒所有部署显式配 SSO_FEISHU_APP_ID"
    )
