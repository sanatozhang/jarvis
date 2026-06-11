"""Completeness guards for the log windower (validity, not just size reduction).

The windower must never hand downstream a head-truncated slice (the fb_56427d576f
failure mode). If folding cannot make the in-window content fit the line budget,
the window is incomplete and we fall back to the full log rather than silently
dropping the tail — and we flag it so operators can see it happened.
"""

from datetime import datetime
from pathlib import Path

from app.services.log_windower import window_log_file


def _w(n: int) -> str:
    """Distinct alpha token (no digits → distinct template)."""
    n += 1
    s = ""
    while n:
        s += chr(97 + n % 26)
        n //= 26
    return s


def test_truncated_window_falls_back_to_full_log(tmp_path: Path):
    log = tmp_path / "plaud.log"
    # 5000 DISTINCT-template lines, all in-window → folding cannot shrink them, so a
    # small line budget must truncate. A truncated window is invalid.
    lines = [
        f"INFO: 2026-06-04 11:0{i % 9}:{i % 60:02d}.000000: evt {_w(i)} {_w(i + 99999)} detail-tail"
        for i in range(5000)
    ]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    path, meta = window_log_file(
        log,
        tmp_path / "windowed",
        center_time=datetime(2026, 6, 4, 11, 4, 0),
        hours_before=4,
        hours_after=2,
        size_threshold=0,
        max_output_lines=1000,    # < 5000 distinct in-window → truncates even after folding
        max_per_template=200,
    )

    # Never return a head-truncated window: fall back to the complete original log.
    assert path == log
    assert meta["windowed"] is False
    assert meta["complete"] is False


def test_complete_window_is_flagged_complete(tmp_path: Path):
    log = tmp_path / "plaud.log"
    # Foldable storm (one template) + a few distinct lines → fits without truncation.
    storm = [
        f'INFO: 2026-06-04 11:04:{i % 60:02d}.000000: ║ "x": {i}, pad-pad-pad-{i}'
        for i in range(3000)
    ]
    distinct = [
        f"INFO: 2026-06-04 11:05:{i % 60:02d}.000000: signal {_w(i)} detail"
        for i in range(100)
    ]
    log.write_text("\n".join(storm + distinct) + "\n", encoding="utf-8")

    path, meta = window_log_file(
        log,
        tmp_path / "windowed",
        center_time=datetime(2026, 6, 4, 11, 4, 30),
        size_threshold=0,
        max_output_lines=100_000,
        max_per_template=200,
    )

    assert meta["windowed"] is True
    assert meta["complete"] is True


def test_empty_window_falls_back_to_recent_not_full_log(tmp_path: Path):
    """problem_date 窗口为空时，回退到「最近」的有界切片，而不是把全量长日志丢回 agent。

    复现 ① rec27zFZSkfFpN：半年长日志 + problem_date 落在空洞 → no_lines_in_window
    → 旧逻辑返回全量 42MB → agent 超时。新逻辑应锚到日志末尾(最近)重切。
    """
    from app.services.log_windower import window_log_files

    log = tmp_path / "plaud.log"
    lines = []
    # 半年前的老日志（窗口外，更不该被全量带出来）
    for i in range(50):
        lines.append(f"INFO: 2025-12-15 10:00:{i % 60:02d}.000000: old event {_w(i)}")
    # 最近一段（日志末尾）；problem center 落不到这里
    for i in range(40):
        lines.append(f"INFO: 2026-06-09 22:10:{i % 60:02d}.000000: recent event {_w(i + 500)}")
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    paths, metas = window_log_files(
        [log],
        tmp_path / "windowed",
        center_time=datetime(2026, 6, 6, 12, 0),  # 落在 Dec 与 June 之间的空洞
        hours_before=4,
        hours_after=2,
        size_threshold=0,
    )
    path, meta = paths[0], metas[0]

    assert path != log, "empty window must NOT return the full original log"
    assert meta["windowed"] is True
    assert meta.get("recent_fallback") or meta.get("center_time_source") == "log_tail"
    text = path.read_text(encoding="utf-8")
    assert "recent event" in text      # 最近的留下
    assert "old event" not in text     # 半年前的没被带出
