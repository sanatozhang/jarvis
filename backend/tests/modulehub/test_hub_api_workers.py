import asyncio

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.modulehub import Hub, register
from app.modulehub.adapters.notifier import FeishuNotifier
from app.modulehub.config import ModulehubSettings, get_modulehub_settings, to_port_settings
from app.modulehub.ports import BuildStatus
from app.modulehub.service import MirrorService, ReleaseService
from app.modulehub.workers.loops import run_forever
from tests.modulehub.fakes import SETTINGS, TOML, FakeBuild, FakeScm, MemStore, RecNotifier, result_json


def build_app(allow_anonymous=True, user=None):
    store, build, scm, note = MemStore(), FakeBuild(), FakeScm(), RecNotifier()
    scm.files[("Plaud-AI/shell-android", "main", "modules.versions.toml")] = TOML
    hub = Hub(ModulehubSettings(allow_anonymous=allow_anonymous), store=store,
              releases=ReleaseService(store=store, build=build, scm=scm, notifier=note, settings=SETTINGS),
              mirror=MirrorService(store=store, scm=scm, notifier=note, settings=SETTINGS))
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        if user:
            request.state.user = user
        return await call_next(request)

    register(app, hub)
    return app, store, build, scm


def http(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


BODY = {"module": "logger", "platform": "android", "branch": "main"}


async def test_modules_and_release_lifecycle_over_http():
    app, store, build, scm = build_app()
    async with http(app) as c:
        assert (await c.get("/api/modulehub/modules")).json()[0]["name"] == "logger"
        r = await c.post("/api/modulehub/releases", json=BODY)
        assert r.status_code == 201 and r.json()["state"] == "building" and r.json()["requestedBy"] == "anonymous"
        rid = r.json()["id"]
        assert (await c.post("/api/modulehub/releases", json=BODY)).status_code == 409
        build.next_status = BuildStatus("success", result_json=result_json())
        await app.state.modulehub.releases.tick_all()
        got = (await c.get("/api/modulehub/releases/%d" % rid)).json()
        assert got["state"] == "done" and got["version"] == "1.1.0" and got["bumpPrUrl"]
        assert [x["id"] for x in (await c.get("/api/modulehub/releases?limit=5")).json()] == [rid]
        assert (await c.get("/api/modulehub/releases/999")).status_code == 404


async def test_preview_validation_and_errors():
    app, *_ = build_app()
    async with http(app) as c:
        p = await c.post("/api/modulehub/releases:preview", json=BODY)
        assert p.status_code == 200 and p.json()["kind"] == "preview"
        assert (await c.post("/api/modulehub/releases", json={**BODY, "branch": "feature/a/b"})).status_code == 400
        assert (await c.post("/api/modulehub/releases/999:resume")).status_code == 404


async def test_resume_and_mirror_over_http():
    app, store, build, scm = build_app()
    async with http(app) as c:
        rid = (await c.post("/api/modulehub/releases", json=BODY)).json()["id"]
        build.next_status = BuildStatus("failure", log_tail="STEP=tag")
        await app.state.modulehub.releases.tick_all()
        r = await c.post("/api/modulehub/releases/%d:resume" % rid)
        assert r.status_code == 200 and r.json()["state"] == "building" and r.json()["resumeCount"] == 1
        assert (await c.post("/api/modulehub/mirror:sync")).json() == {"results": []}


async def test_login_is_required_unless_anonymous_allowed():
    app, *_ = build_app(allow_anonymous=False)
    async with http(app) as c:
        assert (await c.post("/api/modulehub/releases", json=BODY)).status_code == 401
        assert (await c.post("/api/modulehub/mirror:sync")).status_code == 401
    app, *_ = build_app(allow_anonymous=False, user={"email": "me@plaud.ai"})
    async with http(app) as c:
        assert (await c.post("/api/modulehub/releases", json=BODY)).json()["requestedBy"] == "me@plaud.ai"
    app, *_ = build_app(allow_anonymous=False, user={"username": "me"})
    async with http(app) as c:
        assert (await c.post("/api/modulehub/releases", json=BODY)).json()["requestedBy"] == "me"


async def test_run_forever_retries_after_failures_and_stops_on_cancel():
    calls = {"n": 0}

    async def step():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")

    async def sleep(_):
        if calls["n"] >= 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_forever(step, 1, sleep=sleep)
    assert calls["n"] == 3

    async def cancelled():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_forever(cancelled, 1, sleep=sleep)


async def test_hub_start_is_a_noop_when_disabled_and_cancels_when_enabled():
    hub = Hub(ModulehubSettings(enabled=False))
    await hub.start()
    assert hub._tasks == []
    store = MemStore()
    on = Hub(ModulehubSettings(enabled=True, poll_interval_seconds=3600, mirror_interval_minutes=60), store=store,
             releases=ReleaseService(store=store, build=FakeBuild(), scm=FakeScm(), notifier=RecNotifier(), settings=SETTINGS),
             mirror=MirrorService(store=store, scm=FakeScm(), notifier=RecNotifier(), settings=SETTINGS))
    await on.start()
    assert len(on._tasks) == 2
    await asyncio.sleep(0)
    await on.stop()
    assert on._tasks == []


async def test_notifier_swallows_send_failures():
    sent = []

    async def send(email, text):
        sent.append(email)
        if email == "bad@plaud.ai":
            raise RuntimeError("feishu down")
        return True

    await FeishuNotifier(["bad@plaud.ai", "ok@plaud.ai"], send=send).notify("hi")
    assert sent == ["bad@plaud.ai", "ok@plaud.ai"]


def test_settings_env_beats_yaml_and_ports_settings(monkeypatch):
    monkeypatch.setenv("MODULEHUB_JENKINS_JOB", "from-env")
    monkeypatch.setattr("app.modulehub.config._load_yaml", lambda: {"modulehub": {"jenkins_job": "from-yaml", "poll_interval_seconds": 5, "bogus": 1}})
    get_modulehub_settings.cache_clear()
    try:
        s = get_modulehub_settings()
        assert s.jenkins_job == "from-env" and s.poll_interval_seconds == 5 and s.enabled is False and s.mirror_interval_minutes == 10
        ps = to_port_settings(s)
        assert ps.shell_repos == {"android": "Plaud-AI/plaud-native-android", "ios": "Plaud-AI/plaud-native-ios"}
    finally:
        get_modulehub_settings.cache_clear()


def test_hub_wires_real_adapters_lazily(monkeypatch):
    from app.config import get_settings
    from app.modulehub.adapters.github_scm import GitHubScm
    from app.modulehub.adapters.store_sqlalchemy import SqlStore

    class J:  # stand-in for settings.jenkins.servers
        url, user, api_token = "http://jenkins:8080", "u", "t"

    s = get_settings()
    monkeypatch.setattr(s.jenkins, "servers", [J()])
    monkeypatch.setenv("GH_TOKEN", "tok")
    hub = Hub(ModulehubSettings(notify_emails=["a@plaud.ai"]))
    assert isinstance(hub.store, SqlStore) and isinstance(hub.releases.scm, GitHubScm)
    assert hub.mirror.scm is not hub.releases.scm and hub.releases.notifier._emails == ["a@plaud.ai"]
    assert hub.store is hub.store and hub.releases is hub.releases and hub.mirror is hub.mirror


async def test_notifier_default_sender_is_feishu_cli(monkeypatch):
    from app.services import feishu_cli

    sent = []

    async def fake(email="", text="", **kw):
        sent.append((email, text))
        return True

    monkeypatch.setattr(feishu_cli, "send_message", fake)
    await FeishuNotifier(["a@plaud.ai"]).notify("hello")
    assert sent == [("a@plaud.ai", "hello")]
