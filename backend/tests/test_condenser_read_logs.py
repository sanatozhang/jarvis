"""Tests for ContextCondenser._read_logs coverage-preserving reduction (L1.5 Step B).

Companion to the log_windower fix. The condenser used to read `content[:budget]`
from the *head* of the windowed log; on fb_56427d576f that meant its structured
summary stopped at 10:56 and never saw the 11:08 `BleState.connected` event, so
L1.5 actively reinforced the wrong "bluetooth permission" conclusion.

Reduction must cut by redundancy + sample across the whole window — never by
file position.
"""

from pathlib import Path

from app.services.context_condenser import CondensationConfig, ContextCondenser


def _w(n: int) -> str:
    """Distinct alpha token from an int (no digits, so templates stay distinct)."""
    n += 1
    s = ""
    while n:
        s += chr(97 + n % 26)
        n //= 26
    return s


def test_read_logs_keeps_late_event_by_folding_repeated_storm(tmp_path: Path):
    """A head storm of one repeated template must not crowd out a later distinct event."""
    log = tmp_path / "plaud.log"
    lines = [
        f'INFO: 2026-06-04 10:54:{i % 60:02d}.000000: ║  "start_time": {i},  padding-padding-padding-{i}'
        for i in range(5000)  # one template, ~70 chars each ≈ 350K chars >> budget
    ]
    lines.append("INFO: 2026-06-04 11:08:32.000000: BleState.connected device=SN888317498365722882")
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # provider=anthropic → budget collapses to ~105K chars, far below the 350K storm.
    cond = ContextCondenser(CondensationConfig(provider="anthropic", max_input_chars=10_000_000))
    out = cond._read_logs([log])

    assert "BleState.connected" in out


def test_read_logs_samples_across_window_not_just_head(tmp_path: Path):
    """With many distinct lines over budget, late-window content must still be represented."""
    log = tmp_path / "plaud.log"
    early = [
        f"INFO: 2026-06-04 10:5{i % 5}:{i % 60:02d}.000000: earlyitem {_w(i)} {_w(i + 100000)} tail-text-here"
        for i in range(5000)  # distinct templates → folding cannot collapse them
    ]
    late = [
        f"INFO: 2026-06-04 11:1{i % 6}:{i % 60:02d}.000000: LATEZONE {_w(i)} something-happened tail"
        for i in range(1000)
    ]
    log.write_text("\n".join(early + late) + "\n", encoding="utf-8")

    cond = ContextCondenser(CondensationConfig(provider="anthropic", max_input_chars=10_000_000))
    out = cond._read_logs([log])

    # Head-only truncation would stop deep inside the early block and never reach this.
    assert "LATEZONE" in out


def test_l1_signal_lines_are_guaranteed_present(tmp_path: Path):
    """Lines L1 already flagged as high-signal must reach the prompt even if sampling
    would drop them (they may be a single line at the tail of a multi-MB log)."""
    log = tmp_path / "plaud.log"
    storm = [
        f'INFO: 2026-06-04 10:54:{i % 60:02d}.000000: ║ "x": {i}, noise padding {_w(i)} {_w(i + 7000)}'
        for i in range(8000)  # many distinct templates → sampling, budget pressure
    ]
    needle = "INFO: 2026-06-04 11:08:32.000000: BleState.connected device=SN888 NEEDLE-XYZ"
    storm.append(needle)
    log.write_text("\n".join(storm) + "\n", encoding="utf-8")

    # L1 already grepped the needle as a high-signal match.
    l1 = {"patterns": {"ble": {"matches": [needle], "match_count": 1}}}

    cond = ContextCondenser(CondensationConfig(provider="anthropic", max_input_chars=10_000_000))
    out = cond._read_logs([log], l1_extraction=l1)

    assert "NEEDLE-XYZ" in out
