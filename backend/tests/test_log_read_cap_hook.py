"""深度模式 PreToolUse 读取上限 hook：第 N+1 次读 logs/ 被 deny，计数跨调用累加，异常 fail-open。"""
from pathlib import Path
from app.agents.log_read_cap import classify_and_count


def _ev(tool, **inp):
    return {"tool_name": tool, "tool_input": inp}


def test_read_under_cap_allows(tmp_path: Path):
    counter = tmp_path / ".log_read_count"
    for i in range(1, 31):
        d = classify_and_count(_ev("Grep", path="logs/plaud.log", pattern="x"), counter=counter, cap=30)
        assert d["allow"] is True, f"read #{i} should pass"
    assert counter.read_text().strip() == "30"


def test_read_over_cap_denies(tmp_path: Path):
    counter = tmp_path / ".log_read_count"
    counter.write_text("30")
    d = classify_and_count(_ev("Read", file_path="logs/plaud.log"), counter=counter, cap=30)
    assert d["allow"] is False
    assert "上限" in d["reason"]


def test_non_log_tool_not_counted(tmp_path: Path):
    counter = tmp_path / ".log_read_count"
    assert classify_and_count(_ev("Write", file_path="output/result.json"), counter=counter, cap=30)["allow"] is True
    assert classify_and_count(_ev("Read", file_path="rules/bluetooth.md"), counter=counter, cap=30)["allow"] is True
    assert (not counter.exists()) or counter.read_text().strip() == "0"


def test_bash_grep_on_logs_counted(tmp_path: Path):
    counter = tmp_path / ".log_read_count"
    d = classify_and_count(_ev("Bash", command="grep -n boot logs/plaud.log"), counter=counter, cap=30)
    assert d["allow"] is True
    assert counter.read_text().strip() == "1"


def test_corrupt_counter_fails_open(tmp_path: Path):
    counter = tmp_path / ".log_read_count"
    counter.write_text("not-a-number")
    d = classify_and_count(_ev("Read", file_path="logs/plaud.log"), counter=counter, cap=30)
    assert d["allow"] is True
