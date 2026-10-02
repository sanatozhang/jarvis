import pytest

from app.modulehub.adapters.store_sqlalchemy import SqlStore
from app.modulehub.ports import ReleaseRecord


@pytest.fixture
async def store(db_session):
    import app.modulehub.models  # noqa: F401  (register tables on Base before the fixture engine created them)
    return SqlStore(db_session)


async def _fresh(db_engine):
    from app.db.database import Base

    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def test_roundtrip_and_active_lookup(db_engine, store):
    await _fresh(db_engine)
    rec = await store.create(ReleaseRecord(module="logger", repo="Plaud-AI/mobile_logger", platforms=["android", "ios"], branch="main", requested_by="a@b"))
    assert rec.id and rec.created_at
    rec.state, rec.version = "building", "1.1.0"
    rec.artifacts = {"ios": {"coordinate": "c", "sha256": "d" * 64, "apiChanges": ["x"]}}
    rec.previous_versions, rec.bump_prs = {"ios": "1.0.0"}, {"ios": "https://pr/1", "android": ""}
    await store.save(rec, "building")
    got = await store.get(rec.id)
    assert (got.state, got.version, got.platforms, got.repo) == ("building", "1.1.0", ["android", "ios"], "Plaud-AI/mobile_logger")
    assert got.artifacts == rec.artifacts and got.previous_versions == {"ios": "1.0.0"}
    assert got.bump_prs == {"android": "", "ios": "https://pr/1"}
    assert (await store.find_active("logger")).id == rec.id
    assert await store.find_active("network") is None
    assert [r.id for r in await store.list_in_flight()] == [rec.id]
    assert await store.get(999) is None


async def test_previews_do_not_lock(db_engine, store):
    await _fresh(db_engine)
    prev = await store.create(ReleaseRecord(module="logger", platforms=["ios"], branch="main", kind="preview", state="building"))
    assert await store.find_active("logger") is None
    done = await store.create(ReleaseRecord(module="logger", platforms=["android"], branch="main", state="done"))
    assert await store.find_active("logger") is None
    assert [r.id for r in await store.list_recent(10)] == [done.id, prev.id]


async def test_mirror_log(db_engine, store):
    await _fresh(db_engine)
    await store.log_mirror("logger", "release/1", "create", "created")
    from sqlalchemy import select

    from app.modulehub.models import MhMirrorLog

    async with store._sf() as s:
        rows = (await s.execute(select(MhMirrorLog))).scalars().all()
    assert [(r.module, r.branch, r.action) for r in rows] == [("logger", "release/1", "create")]
