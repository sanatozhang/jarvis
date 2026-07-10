"""Tests for mt_runner async-safe workspace lock primitives.

These cover the cross-process file lock that pr_drafter and repo_updater share.
The async pair (acquire/release) must never block the event loop and must
support holding the lock across an `await` boundary.
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_acquire_release_roundtrip(tmp_path):
    from app.services.mt_runner import (
        acquire_workspace_lock_async,
        release_workspace_lock_async,
    )

    fd = await acquire_workspace_lock_async(tmp_path, timeout_sec=5)
    assert isinstance(fd, int)
    await release_workspace_lock_async(fd)


@pytest.mark.asyncio
async def test_second_acquire_waits_for_release(tmp_path):
    """并发两个 task 抢同一把锁：第二个要等第一个释放才能拿到。"""
    from app.services.mt_runner import (
        acquire_workspace_lock_async,
        release_workspace_lock_async,
    )

    fd1 = await acquire_workspace_lock_async(tmp_path, timeout_sec=5)
    order: list[str] = []

    async def _second():
        fd2 = await acquire_workspace_lock_async(tmp_path, timeout_sec=5)
        order.append("second_acquired")
        await release_workspace_lock_async(fd2)

    task = asyncio.create_task(_second())
    await asyncio.sleep(0.3)
    assert "second_acquired" not in order  # 还没释放，第二个应该还在等
    order.append("released_first")
    await release_workspace_lock_async(fd1)
    await task
    assert order == ["released_first", "second_acquired"]


@pytest.mark.asyncio
async def test_acquire_times_out_if_never_released(tmp_path):
    from app.services.mt_runner import acquire_workspace_lock_async

    fd1 = await acquire_workspace_lock_async(tmp_path, timeout_sec=5)
    try:
        with pytest.raises(TimeoutError):
            await acquire_workspace_lock_async(tmp_path, timeout_sec=1)
    finally:
        import fcntl
        import os

        fcntl.flock(fd1, fcntl.LOCK_UN)
        os.close(fd1)


@pytest.mark.asyncio
async def test_acquire_does_not_block_event_loop(tmp_path):
    """持锁时第二个 acquire 在等，但事件循环仍能跑其他协程（没被 flock 卡死）。"""
    from app.services.mt_runner import (
        acquire_workspace_lock_async,
        release_workspace_lock_async,
    )

    fd1 = await acquire_workspace_lock_async(tmp_path, timeout_sec=5)
    ticks: list[int] = []

    async def _ticker():
        for i in range(5):
            ticks.append(i)
            await asyncio.sleep(0.05)

    async def _waiter():
        fd2 = await acquire_workspace_lock_async(tmp_path, timeout_sec=5)
        await release_workspace_lock_async(fd2)

    ticker = asyncio.create_task(_ticker())
    waiter = asyncio.create_task(_waiter())
    await asyncio.sleep(0.3)
    # 事件循环没被阻塞：ticker 在 waiter 还在等锁时已经跑了好几次
    assert len(ticks) >= 3
    await release_workspace_lock_async(fd1)
    await waiter
    await ticker


@pytest.mark.asyncio
async def test_workspace_lock_sync_contract_unchanged(tmp_path):
    """既有同步 contextmanager workspace_lock 行为不变：正常获取+释放，
    释放后能再次获取（不残留锁）。"""
    from app.services.mt_runner import LOCK_FILENAME, workspace_lock

    with workspace_lock(tmp_path, timeout_sec=5):
        assert (tmp_path / LOCK_FILENAME).exists()
    # 释放后可再次获取
    with workspace_lock(tmp_path, timeout_sec=5):
        pass
