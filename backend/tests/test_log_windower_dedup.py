"""Tests for log_windower repeated-line collapsing (option 2).

Regression for fb_56427d576f: a 4-minute burst of a pretty-printed transcript
JSON payload (~200K boxed `║ ...` lines) consumed the entire output-line budget,
truncating the windowed log before it reached the 11:08 `BleState.connected`
event. The agent then only saw the transient 10:56 "ble permission missing"
error and wrongly concluded "Bluetooth Permission Denied".

Collapsing mass-repeated line templates frees the budget so distinct, later
events survive into the windowed log.
"""

from datetime import datetime
from pathlib import Path

from app.services.log_windower import DEFAULT_MAX_PER_TEMPLATE, window_log_file


def _build_storm_log(path: Path) -> None:
    """Early dense storm of two repeated templates, then one distinct event."""
    lines = []
    # 5000 iterations × 2 templates = 10000 near-identical structural lines @10:54
    for i in range(5000):
        lines.append(f'INFO: 2026-06-04 10:54:{i % 60:02d}.000000: ║  "start_time": {i},')
        lines.append(f'INFO: 2026-06-04 10:54:{i % 60:02d}.000000: ║  "end_time": {i + 1},')
    # The decisive distinct event, later but still inside the 07:08-13:08 window
    lines.append("INFO: 2026-06-04 11:08:32.000000: BleState.connected device=SN888317498365722882")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_dedup_lets_distinct_later_event_survive_budget(tmp_path: Path):
    log = tmp_path / "plaud.log"
    _build_storm_log(log)

    out_dir = tmp_path / "windowed"
    center = datetime(2026, 6, 4, 11, 8, 25)

    path, meta = window_log_file(
        log,
        out_dir,
        center_time=center,
        hours_before=4,
        hours_after=2,
        size_threshold=0,        # force windowing regardless of file size
        max_output_lines=2000,   # < 10000 storm lines: naive front-fill truncates before 11:08
        max_per_template=50,     # collapse each mass-repeated template
    )

    content = path.read_text(encoding="utf-8")
    # The critical evidence must survive the budget once repeats are collapsed.
    assert "BleState.connected" in content
    # And the storm must actually have been collapsed (not just passed through).
    assert content.count('"start_time"') <= 51  # 50 kept + at most one marker line


def test_collapsing_is_on_by_default(tmp_path: Path):
    """The pipeline relies on the default cap — collapsing must not require opt-in."""
    log = tmp_path / "plaud.log"
    _build_storm_log(log)

    path, meta = window_log_file(
        log,
        tmp_path / "windowed",
        center_time=datetime(2026, 6, 4, 11, 8, 25),
        size_threshold=0,
        max_output_lines=2000,
        # max_per_template intentionally omitted → DEFAULT_MAX_PER_TEMPLATE
    )

    content = path.read_text(encoding="utf-8")
    assert "BleState.connected" in content
    assert content.count('"start_time"') <= DEFAULT_MAX_PER_TEMPLATE + 1
    assert meta["collapsed_lines"] > 0


def test_distinct_templates_below_cap_are_never_collapsed(tmp_path: Path):
    """Lines that differ in wording (distinct templates) must pass through untouched.

    Note: lines differing only by numbers share a template by design (that is how
    a numeric payload dump is detected) — distinctness here means different words.
    """
    log = tmp_path / "plaud.log"
    words = ["connected", "scanning", "disconnect", "pairing", "handshake", "timeout"]
    lines = [
        f"INFO: 2026-06-04 11:0{i % 9}:{i % 60:02d}.000000: BleState.{words[i % len(words)]} step{i}"
        for i in range(300)
    ]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    path, meta = window_log_file(
        log,
        tmp_path / "windowed",
        center_time=datetime(2026, 6, 4, 11, 8, 25),
        size_threshold=0,
        max_per_template=50,
    )

    content = path.read_text(encoding="utf-8")
    # 6 distinct templates × 50 lines each = 300, each template stays under the cap.
    assert meta["collapsed_lines"] == 0
    for w in words:
        assert f"BleState.{w}" in content
