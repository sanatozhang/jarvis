from app.coreguard.monitors.builder import build_monitor_payload


def _base_def():
    return {
        "key": "hang_rate",
        "name": "[coreguard][P0] Hang Rate threshold",
        "type": "metric alert",
        "detection": "threshold",
        "query": "avg(last_15m):<q> > 1200000000",
        "priority": 1,
        "tags": ["source:coreguard", "tier:p0"],
        "notify": ["@sanato.zhang@plaud.ai"],
        "message": "Hang Rate 超红线",
        "muted_on_create": True,
    }


def test_threshold_payload_core_fields():
    p = build_monitor_payload(_base_def())
    assert p["name"] == "[coreguard][P0] Hang Rate threshold"
    assert p["type"] == "metric alert"
    assert p["query"] == "avg(last_15m):<q> > 1200000000"
    assert p["priority"] == 1
    assert set(["source:coreguard", "tier:p0"]).issubset(set(p["tags"]))
    assert "@sanato.zhang@plaud.ai" in p["message"]
    assert "Hang Rate 超红线" in p["message"]


def test_threshold_critical_parsed_into_options():
    p = build_monitor_payload(_base_def())
    assert p["options"]["thresholds"]["critical"] == 1200000000.0


def test_muted_on_create_sets_silenced():
    p = build_monitor_payload(_base_def())
    assert p["options"]["silenced"] == {"*": None}


def test_not_muted_has_no_silenced():
    d = _base_def()
    d["muted_on_create"] = False
    p = build_monitor_payload(d)
    assert "silenced" not in p["options"]


def test_evaluation_delay_default_900():
    p = build_monitor_payload(_base_def())
    assert p["options"]["evaluation_delay"] == 900


def test_anomaly_sets_threshold_windows_and_critical_1():
    d = _base_def()
    d["detection"] = "anomaly"
    d["type"] = "query alert"
    d["query"] = "avg(last_15m):anomalies(avg:foo{*}, 'agile', 2, seasonality='weekly') >= 1"
    p = build_monitor_payload(d)
    assert p["options"]["thresholds"]["critical"] == 1.0
    assert p["options"]["threshold_windows"] == {"trigger_window": "last_30m", "recovery_window": "last_30m"}


def test_options_override_merges():
    d = _base_def()
    d["options"] = {"renotify_interval": 120}
    p = build_monitor_payload(d)
    assert p["options"]["renotify_interval"] == 120
    assert p["options"]["evaluation_delay"] == 900
