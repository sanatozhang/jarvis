import pytest

C, D, X = "c" * 64, "d" * 64, "e" * 64

from app.modulehub.core import versions_toml as vt

SAMPLE = '''# shell module versions
[logger]
version = "1.0.0"   # pinned
sha256 = "aaaa"
repo = "Plaud-AI/logger-android"

[network]
version = "2.3.1"
sha256 = "bbbb"
repo = "Plaud-AI/network-android"
'''


def test_parse_returns_tables_in_order():
    t = vt.parse(SAMPLE)
    assert list(t) == ["logger", "network"]
    assert t["logger"] == {"version": "1.0.0", "sha256": "aaaa", "repo": "Plaud-AI/logger-android"}


def test_parse_rejects_key_outside_table():
    with pytest.raises(vt.TomlError):
        vt.parse('version = "1"\n')


def test_parse_rejects_unparsable_line():
    with pytest.raises(vt.TomlError):
        vt.parse('[a]\nversion = 1\n')


def test_module_names():
    assert vt.module_names(SAMPLE) == ["logger", "network"]


def test_read_module_missing():
    with pytest.raises(vt.ModuleNotInToml):
        vt.read_module(SAMPLE, "nope")


def test_rewrite_changes_only_the_target_table_and_keeps_comments():
    out = vt.rewrite_module(SAMPLE, "logger", version="1.1.0", sha256=C)
    assert 'version = "1.1.0"   # pinned' in out
    assert 'sha256 = "%s"' % C in out
    # untouched table is byte-identical
    assert out.split("[network]")[1] == SAMPLE.split("[network]")[1]
    assert vt.read_module(out, "logger")["repo"] == "Plaud-AI/logger-android"


def test_rewrite_is_idempotent():
    once = vt.rewrite_module(SAMPLE, "network", version="2.4.0", sha256=D)
    assert vt.rewrite_module(once, "network", version="2.4.0", sha256=D) == once


def test_rewrite_unknown_module():
    with pytest.raises(vt.ModuleNotInToml):
        vt.rewrite_module(SAMPLE, "nope", version="1.0.0", sha256=X)


def test_rewrite_requires_both_keys_present():
    with pytest.raises(vt.TomlError):
        vt.rewrite_module('[a]\nversion = "1.0.0"\nrepo = "x"\n', "a", version="1.1.0", sha256=X)


def test_rewrite_rejects_non_semver_and_bad_sha():
    with pytest.raises(vt.TomlError):
        vt.rewrite_module(SAMPLE, "logger", version="1.0", sha256=C)
    with pytest.raises(vt.TomlError):
        vt.rewrite_module(SAMPLE, "logger", version="1.0.1", sha256='bad"quote')
