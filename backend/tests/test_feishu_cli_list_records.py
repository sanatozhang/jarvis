"""list_records() 不能再往 url 里拼查询字符串（2026-08-25 生产事故）。

背景：`backend/Dockerfile` 不锁 lark-cli 版本，一次常规重建拉到 1.0.89，其 `api`
命令新增了 "path must not contain a query string or fragment" 校验，直接把
list_records() 手拼的 `?view_id=...&page_size=...` 干挂了，`/api/oncall/feishu-tickets`
500 了一片。修复：view_id 走 --params，page_size 走 lark-cli 原生的 --page-size，
url 本身必须是纯路径。
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services import feishu_cli


@pytest.fixture(autouse=True)
def _reset_cache():
    feishu_cli.FeishuCLI.invalidate_cache()
    yield
    feishu_cli.FeishuCLI.invalidate_cache()


async def test_list_records_url_has_no_query_string():
    client = feishu_cli.FeishuCLI()
    run_cli_mock = AsyncMock(return_value={"data": {"items": []}})

    with patch.object(feishu_cli, "_run_cli", new=run_cli_mock):
        await client.list_records(page_size=200, force_refresh=True)

    args, kwargs = run_cli_mock.call_args
    # args = ("api", "GET", url, "--params", "...", "--page-size", "200", "--page-all")
    assert args[0:2] == ("api", "GET")
    url = args[2]
    assert "?" not in url
    assert "#" not in url


async def test_list_records_passes_view_id_via_params_and_page_size_via_flag():
    client = feishu_cli.FeishuCLI()
    run_cli_mock = AsyncMock(return_value={"data": {"items": []}})

    with patch.object(feishu_cli, "_run_cli", new=run_cli_mock):
        await client.list_records(page_size=123, force_refresh=True)

    args, kwargs = run_cli_mock.call_args
    assert "--params" in args
    params_json = args[args.index("--params") + 1]
    assert json.loads(params_json) == {"view_id": client._view_id}

    assert "--page-size" in args
    assert args[args.index("--page-size") + 1] == "123"

    assert "--page-all" in args
