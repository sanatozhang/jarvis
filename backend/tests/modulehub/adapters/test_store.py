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
    rec = await store.create(ReleaseRecord(module="logger", platform="android", branch="main", requested_by="a@b"))
    assert rec.id and rec.created_at
    rec.state, rec.version, rec.api_changes = "building", "1.1.0", ["x"]
    await store.save(rec, "building")
    got = await store.get(rec.id)
    assert (got.state, got.version, got.api_changes) == ("building", "1.1.0", ["x"])
    assert (await store.find_active("logger", "android")).id == rec.id
    assert await store.find_active("logger", "ios") is None
    assert [r.id for r in await store.list_in_flight()] == [rec.id]
    assert await store.get(999) is None


async def test_previews_do_not_lock_and_done_releases_are_recalled(db_engine, store):
    await _fresh(db_engine)
    prev = await store.create(ReleaseRecord(module="logger", platform="android", branch="main", kind="preview", state="building"))
    assert await store.find_active("logger", "android") is None
    done = await store.create(ReleaseRecord(module="logger", platform="android", branch="main", state="pending"))
    done.state, done.version = "done", "1.0.0"
    await store.save(done)
    assert await store.last_released_version("logger", "android", "main") == "1.0.0"
    assert await store.last_released_version("logger", "android", "release/x") == ""
    assert [r.id for r in await store.list_recent(10)] == [done.id, prev.id]


async def test_mirror_log(db_engine, store):
    await _fresh(db_engine)
    await store.log_mirror("logger", "android", "release/1", "create", "created")
    from sqlalchemy import select

    from app.modulehub.models import MhMirrorLog

    async with store._sf() as s:
        rows = (await s.execute(select(MhMirrorLog))).scalars().all()
    assert [(r.branch, r.action) for r in rows] == [("release/1", "create")]
