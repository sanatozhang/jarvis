"""lark-cli 瞬时 API 错误重试。

飞书服务端偶发 `API call failed: server time out error`（lark-cli 本身成功、API 返回
可重试错误响应）。原 _run_cli 只重试进程级故障（超时/空输出/非 JSON），这类 API 级
瞬时错误会被直接判死 → 工单分析第一步 get_record 即失败（实测 rec27CT1lJmDVL）。
"""
import json
from unittest.mock import AsyncMock, patch

import pytest


def test_is_retryable_api_error_matches_transient():
    from app.services.feishu_cli import _is_retryable_api_error
    assert _is_retryable_api_error("API call failed: server time out error")
    assert _is_retryable_api_error("internal error, please try again")
    assert _is_retryable_api_error("rate limit exceeded")
    assert _is_retryable_api_error("service unavailable")


def test_is_retryable_api_error_rejects_hard_errors():
    from app.services.feishu_cli import _is_retryable_api_error
    assert not _is_retryable_api_error("record not found")
    assert not _is_retryable_api_error("invalid field name")
    assert not _is_retryable_api_error("permission denied")
    assert not _is_retryable_api_error("")


class _FakeProc:
    def __init__(self, stdout: bytes):
        self._stdout = stdout
        self.returncode = 0

    async def communicate(self):
        return self._stdout, b""

    def kill(self):
        pass


async def test_run_cli_retries_transient_api_error_then_succeeds():
    from app.services import feishu_cli
    transient = json.dumps({"code": 1254607, "msg": "API call failed: server time out error"}).encode()
    success = json.dumps({"code": 0, "msg": "success", "data": {"record": {"fields": {}}}}).encode()
    procs = [_FakeProc(transient), _FakeProc(success)]

    async def fake_exec(*args, **kwargs):
        return procs.pop(0)

    with patch.object(feishu_cli, "_ensure_cli_profile", new_callable=AsyncMock), \
         patch("asyncio.create_subprocess_exec", side_effect=fake_exec), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        result = await feishu_cli._run_cli("api", "GET", "/x", retries=3)

    assert result["code"] == 0           # 第二次成功
    assert procs == []                   # 两次都被消费 → 确实重试了


async def test_run_cli_does_not_retry_hard_api_error():
    from app.services import feishu_cli
    hard = json.dumps({"code": 1254005, "msg": "record not found"}).encode()
    procs = [_FakeProc(hard), _FakeProc(hard)]

    async def fake_exec(*args, **kwargs):
        return procs.pop(0)

    with patch.object(feishu_cli, "_ensure_cli_profile", new_callable=AsyncMock), \
         patch("asyncio.create_subprocess_exec", side_effect=fake_exec), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(RuntimeError, match="record not found"):
            await feishu_cli._run_cli("api", "GET", "/x", retries=3)

    assert len(procs) == 1               # 只消费 1 次 → 硬错误不重试
