"""Read and surgically rewrite modules.versions.toml (flat `[name]` tables of version/sha256/repo)."""
from __future__ import annotations

import re
from typing import Dict, List

_LINE = re.compile(r'^([A-Za-z0-9_.-]+)\s*=\s*"([^"]*)"')
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


class TomlError(ValueError):
    pass


class ModuleNotInToml(KeyError):
    pass


def parse(text: str) -> Dict[str, Dict[str, str]]:
    tables: Dict[str, Dict[str, str]] = {}
    current = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = tables.setdefault(line[1:-1].strip(), {})
            continue
        m = _LINE.match(line)
        if not m or line[m.end():].strip():
            raise TomlError("cannot parse line: %r" % raw)
        if current is None:
            raise TomlError("key outside a [module] table: %r" % raw)
        current[m.group(1)] = m.group(2)
    return tables


def module_names(text: str) -> List[str]:
    return list(parse(text))


def read_module(text: str, name: str) -> Dict[str, str]:
    tables = parse(text)
    if name not in tables:
        raise ModuleNotInToml(name)
    return tables[name]


def rewrite_module(text: str, name: str, *, version: str, sha256: str) -> str:
    """Change `version` and `sha256` of one table; every other byte stays as it was."""
    if not _SEMVER.match(version):
        raise TomlError("version must be X.Y.Z: %r" % version)
    if sha256 and not _SHA.match(sha256):
        raise TomlError("sha256 must be 64 lowercase hex characters: %r" % sha256)
    if name not in parse(text):
        raise ModuleNotInToml(name)
    out: List[str] = []
    in_table = False
    done = set()
    for raw in text.splitlines(keepends=True):
        stripped = raw.split("#", 1)[0].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_table = stripped[1:-1].strip() == name
        elif in_table:
            m = _LINE.match(stripped)
            if m and m.group(1) in ("version", "sha256"):
                key = m.group(1)
                new = version if key == "version" else sha256
                raw = raw.replace('"%s"' % m.group(2), '"%s"' % new, 1)
                done.add(key)
        out.append(raw)
    if done != {"version", "sha256"}:
        raise TomlError("[%s] must contain both version and sha256" % name)
    return "".join(out)
