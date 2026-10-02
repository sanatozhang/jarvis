"""Isolation contract of modulehub, enforced from both directions (no import-linter in this repo)."""
import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
HUB = APP / "modulehub"
OTHER_DOMAINS = ("app.crashguard", "app.coreguard", "app.graygate", "app.platform_tickets")
# jarvis infrastructure that adapters (and only adapters/config/models) may use
INFRA_ALLOWED_IN_ADAPTERS = ("app.config", "app.db", "app.services")
PURE_FILES = [HUB / "service.py", HUB / "ports.py"]


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module


def _py(root: Path):
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def test_modulehub_never_imports_other_business_domains():
    bad = [(p.relative_to(APP).as_posix(), m) for p in _py(HUB) for m in _imports(p)
           if m.startswith(OTHER_DOMAINS) or m.startswith("app.api.release") or m.startswith("app.workers")]
    assert not bad, bad


def test_no_other_domain_imports_modulehub():
    offenders = []
    for p in _py(APP):
        if HUB in p.parents or p == APP / "main.py":
            continue
        offenders += [(p.relative_to(APP).as_posix(), m) for m in _imports(p) if m.startswith("app.modulehub")]
    assert not offenders, offenders


def test_main_py_only_uses_the_register_entry_and_models():
    allowed = {"app.modulehub", "app.modulehub.models"}
    used = {m for m in _imports(APP / "main.py") if m.startswith("app.modulehub")}
    assert used <= allowed, used


def test_core_is_pure():
    """core/ imports only the stdlib and other core modules (plus release_rules inside core)."""
    import sys

    stdlib = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else None
    bad = []
    for p in _py(HUB / "core"):
        for m in _imports(p):
            top = m.split(".")[0]
            if m.startswith("app.modulehub.core"):
                continue
            if stdlib is not None and top in stdlib:
                continue
            if stdlib is None and top not in ("app", "pydantic", "sqlalchemy", "fastapi", "httpx"):
                continue
            bad.append((p.name, m))
    assert not bad, bad


def test_service_and_ports_only_depend_on_core_and_ports():
    bad = []
    for p in PURE_FILES:
        for m in _imports(p):
            if m.startswith("app.") and not m.startswith(("app.modulehub.core", "app.modulehub.ports")):
                bad.append((p.name, m))
    assert not bad, bad


def test_only_adapters_config_models_and_package_root_touch_jarvis_infrastructure():
    allowed_files = {"config.py", "models.py", "__init__.py", "store_sqlalchemy.py", "notifier.py", "jenkins_runner.py", "github_scm.py"}
    bad = []
    for p in _py(HUB):
        for m in _imports(p):
            if m.startswith(INFRA_ALLOWED_IN_ADAPTERS) and p.name not in allowed_files:
                bad.append((p.relative_to(HUB).as_posix(), m))
    assert not bad, bad


def test_tables_use_the_mh_prefix_and_only_reference_mh_tables():
    from app.modulehub import models

    for table in (models.MhRelease.__table__, models.MhReleaseEvent.__table__, models.MhMirrorLog.__table__):
        assert table.name.startswith("mh_")
        for col in table.columns:
            for fk in col.foreign_keys:
                assert fk.column.table.name.startswith("mh_")
