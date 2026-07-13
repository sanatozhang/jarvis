"""Regression for fb_08344bb236: grep_log/extract_for_rules must keep the most
recent matches, not the first ones encountered scanning from the top of the file.

Logs append chronologically. This device had an old (2025-08-12) OTA-failure
incident that alone produced 200+ lines matching the `ota_state` pattern. The
old `head -n 200` cap filled up entirely on that stale cluster, so the device's
real, recent (2026-07) OTA failures never entered the L1 extraction at all —
which then poisoned the log_windower's coverage check and time-window guess
downstream. `tail`, not `head`, keeps the matches that are actually relevant.
"""

from pathlib import Path

from app.models.schemas import PreExtractPattern, Rule, RuleMeta
from app.services.extractor import extract_for_rules, grep_log


def _build_log_with_old_incident_and_recent_failure(path: Path) -> None:
    lines = []
    # Old incident: 250 matching lines, all from 2025-08-12 — more than the 200 cap.
    for i in range(250):
        lines.append(f"INFO: 2025-08-12 0{i % 9}:{i % 60:02d}:00.000000: ota_state old-incident-{i}")
    # The real, recent failure the ticket is actually about.
    lines.append("INFO: 2026-07-09 08:57:00.000000: ota_state BLE OTA watchdog triggered otaFailed")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_grep_log_keeps_most_recent_matches_not_earliest(tmp_path: Path):
    log = tmp_path / "plaud.log"
    _build_log_with_old_incident_and_recent_failure(log)

    matches = grep_log(log, "ota_state", max_lines=200)

    assert len(matches) == 200
    assert "2026-07-09" in matches[-1], "tail should end on the most recent (real) match"


def test_extract_for_rules_keeps_recent_matches_across_the_200_cap(tmp_path: Path):
    log = tmp_path / "plaud.log"
    _build_log_with_old_incident_and_recent_failure(log)

    rule = Rule(
        meta=RuleMeta(
            id="hardware-firmware",
            pre_extract=[PreExtractPattern(name="ota_state", pattern="ota_state")],
        )
    )

    extraction = extract_for_rules([rule], [log])
    matches = extraction["patterns"]["hardware-firmware.ota_state"]["matches"]

    assert any("2026-07-09" in m for m in matches), "recent evidence must survive the outer cap too"
