from app.crashguard.services.stability_targets import cf_emoji


def test_ios_sessions():
    assert cf_emoji(99.95, "ios", "sessions") == "🟩"
    assert cf_emoji(99.94, "ios", "sessions") == "🟨"
    assert cf_emoji(99.89, "ios", "sessions") == "🟥"


def test_ios_users():
    assert cf_emoji(99.90, "ios", "users") == "🟩"
    assert cf_emoji(99.50, "ios", "users") == "🟨"
    assert cf_emoji(99.49, "ios", "users") == "🟥"


def test_android():
    assert cf_emoji(99.90, "android", "sessions") == "🟩"
    assert cf_emoji(99.80, "android", "sessions") == "🟨"
    assert cf_emoji(99.79, "android", "sessions") == "🟥"
    assert cf_emoji(99.80, "android", "users") == "🟩"
    assert cf_emoji(99.79, "android", "users") == "🟨"


def test_summary_uses_android_bands():
    assert cf_emoji(99.85, None, "users") == cf_emoji(99.85, "android", "users")
    assert cf_emoji(99.85, "IOS", "sessions") == "🟥"
