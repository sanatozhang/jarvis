import yaml
from pathlib import Path
from app.coreguard.monitors.sync import sync_def


class FakeClient:
    def __init__(self):
        self.created = []
        self.updated = []

    def create(self, payload):
        self.created.append(payload)
        return {"id": 999}

    def update(self, monitor_id, payload):
        self.updated.append((monitor_id, payload))
        return {"id": monitor_id}


def _write_def(tmp_path: Path, extra: dict) -> Path:
    d = {
        "key": "hang_rate",
        "name": "[coreguard][P0] Hang Rate",
        "type": "metric alert",
        "detection": "threshold",
        "query": "avg(last_15m):avg:foo{*} > 100",
        "priority": 1,
        "tags": ["source:coreguard"],
        "notify": ["@x@y.com"],
        "muted_on_create": True,
    }
    d.update(extra)
    p = tmp_path / "hang_rate.threshold.yaml"
    p.write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")
    return p


def test_sync_creates_when_no_id_and_writes_id_back(tmp_path):
    p = _write_def(tmp_path, {})  # 无 id
    client = FakeClient()

    sync_def(client, p, dry_run=False)

    assert len(client.created) == 1
    assert len(client.updated) == 0
    reloaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert reloaded["id"] == 999


def test_sync_updates_when_id_present(tmp_path):
    p = _write_def(tmp_path, {"id": 555})
    client = FakeClient()

    sync_def(client, p, dry_run=False)

    assert len(client.created) == 0
    assert len(client.updated) == 1
    assert client.updated[0][0] == 555


def test_dry_run_calls_nothing(tmp_path):
    p = _write_def(tmp_path, {})
    client = FakeClient()

    payload = sync_def(client, p, dry_run=True)

    assert client.created == []
    assert client.updated == []
    assert payload["name"] == "[coreguard][P0] Hang Rate"
