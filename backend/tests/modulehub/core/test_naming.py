from app.modulehub.core import naming


def test_slug_flattens_branch():
    assert naming.slug("main") == "main"
    assert naming.slug("release/4.0.3_0925") == "release-4.0.3_0925"
    assert naming.slug("Release/X Y") == "release-x-y"


def test_bump_branch_and_title():
    assert naming.bump_branch("logger", "android", "release/1.2") == "chore/jarvis/bump-logger-android-release-1.2"
    assert naming.bump_title("logger", "android", "1.0.0", "1.1.0") == "chore: bump logger-android 1.0.0 -> 1.1.0"


def test_backport_branch():
    assert naming.backport_branch("logger", "1.0.1") == "chore/jarvis/backport-logger-1.0.1"


def test_bump_body_contains_required_sections():
    body = naming.bump_body(
        name="logger", platform="android", old="1.0.0", new="1.1.0", sha256="ab" * 32,
        coordinate="ai.plaud.module:logger:1.1.0", changelog=["feat: a (#1)", "fix: b"],
        api_changes=["METHOD_REMOVED x"], release_url="https://jarvis/releases/3",
    )
    for needle in ("1.0.0", "1.1.0", "ab" * 32, "ai.plaud.module:logger:1.1.0", "feat: a (#1)", "METHOD_REMOVED x", "https://jarvis/releases/3"):
        assert needle in body


def test_bump_body_without_optional_parts():
    body = naming.bump_body(name="n", platform="ios", old="-", new="1.0.0", sha256="", coordinate="",
                            changelog=[], api_changes=[], release_url="")
    assert "No API changes" in body and "No changelog" in body
