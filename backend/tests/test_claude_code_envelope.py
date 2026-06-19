"""claude_code CLI `--output-format json` 信封解析。

stdout 从纯文本变为 {type:result, result, usage, total_cost_usd, ...}；
`.result` 字段等于原 text 模式 stdout（喂给下游 salvage/error/fixup 逻辑，行为不变），
usage/cost 另抽出供计费。非 JSON / 畸形 → fallback 当纯文本，cost_source=partial。
"""
import json


def test_parse_valid_envelope_extracts_text_usage_cost():
    from app.agents.claude_code import parse_cli_result_envelope
    env = {
        "type": "result",
        "subtype": "success",
        "result": "分析正文 markdown ...",
        "total_cost_usd": 0.0123,
        "num_turns": 4,
        "usage": {
            "input_tokens": 1500,
            "output_tokens": 800,
            "cache_read_input_tokens": 6000,
            "cache_creation_input_tokens": 200,
        },
    }
    text, usage, cost, source = parse_cli_result_envelope(json.dumps(env))
    assert text == "分析正文 markdown ..."        # .result == 原 stdout
    assert usage["input_tokens"] == 1500
    assert usage["output_tokens"] == 800
    assert cost == 0.0123
    assert source == "cli_reported"


def test_parse_plain_text_falls_back():
    from app.agents.claude_code import parse_cli_result_envelope
    raw = "I wrote the analysis to result.json.\n\nMore markdown text here."
    text, usage, cost, source = parse_cli_result_envelope(raw)
    assert text == raw          # 原样当正文
    assert usage == {}
    assert cost is None
    assert source == "partial"


def test_parse_malformed_json_falls_back():
    from app.agents.claude_code import parse_cli_result_envelope
    raw = '{"type": "result", "result": "oops"'  # 缺右括号
    text, usage, cost, source = parse_cli_result_envelope(raw)
    assert text == raw
    assert source == "partial"


def test_parse_envelope_without_usage():
    from app.agents.claude_code import parse_cli_result_envelope
    env = {"type": "result", "result": "正文", "total_cost_usd": 0.05}
    text, usage, cost, source = parse_cli_result_envelope(json.dumps(env))
    assert text == "正文"
    assert usage == {}
    assert cost == 0.05
    assert source == "cli_reported"
