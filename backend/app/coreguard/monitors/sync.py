"""读 def yaml → builder → 幂等 create/update + 回写 id。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

import yaml

from app.coreguard.monitors.builder import build_monitor_payload

logger = logging.getLogger("coreguard.monitors.sync")

DEFS_DIR = Path(__file__).resolve().parent / "defs"


def _load(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _write_id_back(path: Path, monitor_id: int) -> None:
    d = _load(path)
    d["id"] = monitor_id
    path.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")


def sync_def(client, path: Path, dry_run: bool = False) -> Dict[str, Any]:
    """同步单个 def 文件。返回构造出的 payload。"""
    d = _load(path)
    payload = build_monitor_payload(d)
    if dry_run:
        logger.info("[dry-run] %s\n%s", path.name, payload)
        return payload

    existing_id = d.get("id")
    if existing_id:
        client.update(existing_id, payload)
        logger.info("updated monitor %s (%s)", existing_id, path.name)
    else:
        result = client.create(payload)
        new_id = result["id"]
        _write_id_back(path, new_id)
        logger.info("created monitor %s (%s)", new_id, path.name)
    return payload


def sync_all(client, defs_dir: Path = DEFS_DIR, dry_run: bool = False) -> List[str]:
    if not defs_dir.exists():
        logger.info("defs_dir %s does not exist, skipping", defs_dir)
        return []
    done = []
    for p in sorted(defs_dir.glob("*.yaml")):
        sync_def(client, p, dry_run=dry_run)
        done.append(p.name)
    return done
