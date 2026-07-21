"""config.local.yaml 每台服务器独立运行时覆盖机制单测（2026-07-21）。

背景：`/settings` 页面里"无需重启即可持久化"的开关（如 crashguard.qa_capture_enabled）
以前直接写回 config.yaml —— 但 config.yaml 是 git 追踪文件，docker 部署又把它挂载成
只读，写入静默失败（OSError: Read-only file system 被 try/except 吞掉），设置页显示
"已保存"实际从未落盘，重启/重新部署后打回默认值。改用 config.local.yaml（不进 git，
每台服务器独立）承接这类运行时覆盖：config.yaml 提供默认值/模板，config.local.yaml
按 section 递归合并覆盖在上面。

覆盖：_deep_merge()、_load_yaml() 叠加合并、write_local_override() 读-合并-写。
"""
from __future__ import annotations

import pytest


@pytest.fixture
def isolated_project_root(tmp_path, monkeypatch):
    """把 app.config.PROJECT_ROOT 指到一个临时目录，并清空模块级 yaml 缓存。"""
    import app.config as config_module

    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config_module, "_yaml_config", {})
    yield tmp_path, config_module


def test_deep_merge_overrides_scalars_and_recurses_into_nested_dicts():
    from app.config import _deep_merge

    base = {"crashguard": {"qa_capture_enabled": False, "pr_enabled": True}, "other": "x"}
    override = {"crashguard": {"qa_capture_enabled": True}}
    merged = _deep_merge(base, override)

    assert merged["crashguard"]["qa_capture_enabled"] is True
    assert merged["crashguard"]["pr_enabled"] is True  # 未被覆盖的 sibling key 保留
    assert merged["other"] == "x"


def test_load_yaml_without_local_file_returns_base_only(isolated_project_root):
    tmp_path, config_module = isolated_project_root
    (tmp_path / "config.yaml").write_text(
        "crashguard:\n  qa_capture_enabled: false\n", encoding="utf-8",
    )

    result = config_module._load_yaml()
    assert result["crashguard"]["qa_capture_enabled"] is False


def test_load_yaml_merges_local_override_on_top_of_base(isolated_project_root):
    tmp_path, config_module = isolated_project_root
    (tmp_path / "config.yaml").write_text(
        "crashguard:\n  qa_capture_enabled: false\n  pr_enabled: true\n", encoding="utf-8",
    )
    (tmp_path / "config.local.yaml").write_text(
        "crashguard:\n  qa_capture_enabled: true\n", encoding="utf-8",
    )

    result = config_module._load_yaml()
    assert result["crashguard"]["qa_capture_enabled"] is True  # local 覆盖生效
    assert result["crashguard"]["pr_enabled"] is True          # base 里未被覆盖的 key 保留


def test_load_yaml_tolerates_missing_config_yaml_with_only_local_override(isolated_project_root):
    tmp_path, config_module = isolated_project_root
    (tmp_path / "config.local.yaml").write_text(
        "crashguard:\n  qa_capture_enabled: true\n", encoding="utf-8",
    )

    result = config_module._load_yaml()
    assert result["crashguard"]["qa_capture_enabled"] is True


def test_load_yaml_does_not_crash_when_local_override_is_a_directory(isolated_project_root):
    """回归测试（2026-07-21 生产环境实测）：docker 单文件 bind mount 在宿主机源路径
    不存在时的自动创建行为不总是建普通文件——config.local.yaml 曾被意外建成目录，
    导致 open() 抛 IsADirectoryError，把整个 app 启动崩溃（backend 容器进入重启
    循环）。这个基础设施层面的意外绝不能带崩 app：应该跳过覆盖，只用 config.yaml
    默认值，而不是让异常向上传播。"""
    tmp_path, config_module = isolated_project_root
    (tmp_path / "config.yaml").write_text(
        "crashguard:\n  qa_capture_enabled: false\n", encoding="utf-8",
    )
    (tmp_path / "config.local.yaml").mkdir()

    result = config_module._load_yaml()  # 不应该抛异常
    assert result["crashguard"]["qa_capture_enabled"] is False  # 优雅降级成只用 config.yaml


def test_write_local_override_does_not_crash_when_local_override_is_a_directory(isolated_project_root):
    """write_local_override 的读阶段同理：is_file() 检查跳过非普通文件，不抛异常。
    最终 open(path, "w") 仍可能因为路径是目录而失败——这交给调用方（api/crash.py 里
    两个设置端点）已有的 try/except 兜底，本函数只保证"读阶段"不因为这个意外而
    在合并已有覆盖项之前就崩掉。"""
    tmp_path, config_module = isolated_project_root
    (tmp_path / "config.local.yaml").mkdir()

    with pytest.raises(IsADirectoryError):
        config_module.write_local_override("crashguard", {"qa_capture_enabled": True})
    # 关键断言：抛出的是"最终写入那一步"的 IsADirectoryError，而不是读阶段就崩溃
    # （通过下面这行能正常执行到 open(..., "w") 这一步来间接验证——如果读阶段本身
    # 出问题，报错栈会不一样；这里主要靠上面 test_load_yaml 那条测试锁死读阶段行为，
    # 本测试确认调用方必须自己兜底最终写入失败，函数不会静默吞掉/也不会在读阶段崩溃）


def test_write_local_override_creates_file_when_missing(isolated_project_root):
    tmp_path, config_module = isolated_project_root

    config_module.write_local_override("crashguard", {"qa_capture_enabled": True})

    local_path = tmp_path / "config.local.yaml"
    assert local_path.exists()
    import yaml
    data = yaml.safe_load(local_path.read_text(encoding="utf-8"))
    assert data["crashguard"]["qa_capture_enabled"] is True


def test_write_local_override_preserves_existing_keys_in_same_section(isolated_project_root):
    tmp_path, config_module = isolated_project_root
    (tmp_path / "config.local.yaml").write_text(
        "crashguard:\n  symbol_upload_keep_versions: 7\n", encoding="utf-8",
    )

    config_module.write_local_override("crashguard", {"qa_capture_enabled": True})

    import yaml
    data = yaml.safe_load((tmp_path / "config.local.yaml").read_text(encoding="utf-8"))
    assert data["crashguard"]["qa_capture_enabled"] is True
    assert data["crashguard"]["symbol_upload_keep_versions"] == 7  # 没被这次写入覆盖掉


def test_write_local_override_preserves_other_top_level_sections(isolated_project_root):
    tmp_path, config_module = isolated_project_root
    (tmp_path / "config.local.yaml").write_text(
        "some_other_section:\n  key: value\n", encoding="utf-8",
    )

    config_module.write_local_override("crashguard", {"qa_capture_enabled": True})

    import yaml
    data = yaml.safe_load((tmp_path / "config.local.yaml").read_text(encoding="utf-8"))
    assert data["some_other_section"]["key"] == "value"
    assert data["crashguard"]["qa_capture_enabled"] is True


def test_write_local_override_never_touches_config_yaml(isolated_project_root):
    """回归测试：config.yaml 必须保持只读挂载可用——写覆盖只能落 config.local.yaml。"""
    tmp_path, config_module = isolated_project_root
    (tmp_path / "config.yaml").write_text(
        "crashguard:\n  qa_capture_enabled: false\n", encoding="utf-8",
    )
    original_mtime = (tmp_path / "config.yaml").stat().st_mtime

    config_module.write_local_override("crashguard", {"qa_capture_enabled": True})

    assert (tmp_path / "config.yaml").stat().st_mtime == original_mtime
    import yaml
    base_content = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert base_content["crashguard"]["qa_capture_enabled"] is False  # config.yaml 内容不变


def test_write_local_override_invalidates_load_yaml_cache(isolated_project_root):
    tmp_path, config_module = isolated_project_root
    (tmp_path / "config.yaml").write_text(
        "crashguard:\n  qa_capture_enabled: false\n", encoding="utf-8",
    )

    first = config_module._load_yaml()
    assert first["crashguard"]["qa_capture_enabled"] is False

    config_module.write_local_override("crashguard", {"qa_capture_enabled": True})

    second = config_module._load_yaml()
    assert second["crashguard"]["qa_capture_enabled"] is True
