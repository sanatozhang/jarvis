"""L1-signal coverage cross-check for the windower (catches a wrong center_time).

Folding/completeness guards handle "window too dense". This guard handles the
orthogonal failure: if center_time/window bounds are wrong, the decisive events
fall *outside* the window entirely. We detect that by checking whether the L1
grep hits (the rule-matched high-signal lines) survived into the windowed output.
"""

from app.services.log_windower import signal_lines_from_extraction, window_coverage_ratio


def test_signal_lines_pulled_from_l1_patterns():
    extraction = {
        "patterns": {
            "ble_error": {"matches": ["10:56 ble permission missing", "10:57 ble retry"]},
            "sd_error": {"matches": ["10:58 PathAccessException denied"]},
        }
    }
    sigs = signal_lines_from_extraction(extraction)
    assert "10:56 ble permission missing" in sigs
    assert "10:58 PathAccessException denied" in sigs
    assert len(sigs) == 3


def test_coverage_high_when_signal_lines_in_window():
    # Real L1 hits are full raw lines; the window holds the same lines verbatim.
    windowed = (
        "INFO: 2026-06-04 11:08:01 ble permission missing\n"
        "INFO: 2026-06-04 11:08:02 ble retry\n"
    )
    # Same templates, only the timestamp differs (normalized away) → fully covered.
    sigs = [
        "INFO: 2026-06-04 09:00:00 ble permission missing",
        "INFO: 2026-06-04 09:00:01 ble retry",
    ]
    assert window_coverage_ratio(windowed, sigs) == 1.0


def test_coverage_low_when_window_misses_signal_region():
    windowed = (
        "INFO: 2026-06-04 07:00:00 app started\n"
        "INFO: 2026-06-04 07:00:01 wifi connected\n"
    )
    sigs = [
        "INFO: 2026-06-04 10:56:00 ble permission missing",
        "INFO: 2026-06-04 10:58:00 PathAccessException denied",
    ]
    assert window_coverage_ratio(windowed, sigs) == 0.0


def test_coverage_is_one_when_no_signal_lines():
    # No L1 hits → nothing to verify, don't trigger a false fallback.
    assert window_coverage_ratio("anything", []) == 1.0
