"""PATCH /api/crash/settings/{qa-capture,symbols} 持久化目标单测（2026-07-21）。

背景：这两个端点以前直接写 config.yaml —— docker 部署把它挂载成只读，写入静默失败
（异常被 try/except 吞掉，仅打一条 warning log），设置页显示"已保存"实际从未落盘。
改成写 config.local.yaml（每台服务器独立，不进 git）。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import yaml


@pytest.fixture
def isolated_project_root(tmp_path, monkeypatch):
    import app.config as config_module

    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config_module, "_yaml_config", {})
    yield tmp_path, config_module


def _fake_settings():
    s = MagicMock()
    s.qa_capture_enabled = False
    s.qa_version_patch_threshold = 100
    s.symbol_upload_keep_versions = 5
    s.github_cache_keep_versions = 5
    return s


@pytest.mark.asyncio
async def test_qa_capture_patch_writes_local_yaml_not_config_yaml(isolated_project_root, monkeypatch):
    from app.crashguard.api import crash as crash_api

    tmp_path, _ = isolated_project_root
    (tmp_path / "config.yaml").write_text(
        "crashguard:\n  qa_capture_enabled: false\n", encoding="utf-8",
    )
    original_config_yaml = (tmp_path / "config.yaml").read_text(encoding="utf-8")

    s = _fake_settings()
    monkeypatch.setattr(crash_api, "get_crashguard_settings", lambda: s)

    result = await crash_api.update_qa_capture_setting(
        crash_api.QaCaptureSettingsPatch(qa_capture_enabled=True)
    )

    assert result["qa_capture_enabled"] is True
    assert s.qa_capture_enabled is True  # 运行中实例立即生效

    local_path = tmp_path / "config.local.yaml"
    assert local_path.exists()
    local_data = yaml.safe_load(local_path.read_text(encoding="utf-8"))
    assert local_data["crashguard"]["qa_capture_enabled"] is True

    # config.yaml 必须原封不动——回归测试之前"写只读文件静默失败"的那个 bug
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == original_config_yaml


@pytest.mark.asyncio
async def test_qa_capture_patch_persists_across_settings_reload(isolated_project_root, monkeypatch):
    """模拟"重启"：写完之后重新走 _load_yaml() 读取，必须能读到刚写入的值
    （这正是生产环境实测发现的故障——切换开关后重启/重新部署又变回默认值）。"""
    from app.crashguard.api import crash as crash_api
    import app.config as config_module

    tmp_path, _ = isolated_project_root
    (tmp_path / "config.yaml").write_text(
        "crashguard:\n  qa_capture_enabled: false\n", encoding="utf-8",
    )

    s = _fake_settings()
    monkeypatch.setattr(crash_api, "get_crashguard_settings", lambda: s)
    await crash_api.update_qa_capture_setting(
        crash_api.QaCaptureSettingsPatch(qa_capture_enabled=True)
    )

    # 模拟进程重启：清空缓存，重新走 _load_yaml()（不复用内存里的 s 实例）
    monkeypatch.setattr(config_module, "_yaml_config", {})
    reloaded = config_module._load_yaml()
    assert reloaded["crashguard"]["qa_capture_enabled"] is True


@pytest.mark.asyncio
async def test_symbol_settings_patch_writes_local_yaml_and_preserves_other_keys(
    isolated_project_root, monkeypatch,
):
    from app.crashguard.api import crash as crash_api

    tmp_path, _ = isolated_project_root
    (tmp_path / "config.local.yaml").write_text(
        "crashguard:\n  qa_capture_enabled: true\n", encoding="utf-8",
    )

    s = _fake_settings()
    monkeypatch.setattr(crash_api, "get_crashguard_settings", lambda: s)

    result = await crash_api.update_symbol_settings(
        crash_api.SymbolSettingsPatch(symbol_upload_keep_versions=10, github_cache_keep_versions=None)
    )

    assert result["symbol_upload_keep_versions"] == 10
    assert s.symbol_upload_keep_versions == 10
    assert s.github_cache_keep_versions == 5  # None 传入不改这个字段

    local_data = yaml.safe_load((tmp_path / "config.local.yaml").read_text(encoding="utf-8"))
    assert local_data["crashguard"]["symbol_upload_keep_versions"] == 10
    assert local_data["crashguard"]["qa_capture_enabled"] is True  # 之前写的另一个开关没被冲掉
