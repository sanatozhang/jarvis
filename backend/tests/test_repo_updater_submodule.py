# backend/tests/test_repo_updater_submodule.py
from pathlib import Path
from app.services import repo_updater as ru

def test_submodule_shell_detected(tmp_path: Path):
    # 顶层 .git + .gitmodules → submodule 壳
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitmodules").write_text('[submodule "x"]\n')
    assert ru._is_submodule_shell(tmp_path) is True

def test_plain_git_not_shell(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    assert ru._is_submodule_shell(tmp_path) is False

def test_mt_workspace_not_shell(tmp_path: Path):
    sub = tmp_path / "common"; sub.mkdir(); (sub / ".git").mkdir()
    assert ru._is_submodule_shell(tmp_path) is False
    assert ru._is_mt_workspace(tmp_path) is True
