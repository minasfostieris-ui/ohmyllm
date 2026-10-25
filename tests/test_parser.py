from ohmyllm.agents.parser import ParsedCall, ToolCallParser, repair_json
from ohmyllm.agents.validator import ToolCallValidator
from ohmyllm.tools import build_default_registry


def test_repair_json_cases():
    ok = repair_json('{"a": 1,}')
    assert ok[0] == {"a": 1}
    ok = repair_json("{'a': True, 'b': None}")
    assert ok[0] == {"a": True, "b": None}
    ok = repair_json('{"name": "soul_append", "arguments": {"section": "User", "text": "hi"}')
    assert ok[0]["name"] == "soul_append"  # unbalanced repaired
    ok = repair_json('{name: "x", value: 5}')
    assert ok[0]["name"] == "x"
    ok = repair_json('{"text": "smart “quotes”"}')
    assert ok[0] is not None
    ok = repair_json('{"a": [1, 2,] }')
    assert ok[0] == {"a": [1, 2]}
    ok = repair_json("this is not json at all")
    assert ok[0] is None and ok[1]


def test_parse_fenced_json_single_and_multi():
    p = ToolCallParser()
    text = (
        "Sure!\n```json\n"
        '{"name": "soul_append", "arguments": {"section": "User", "text": "name is K"}}'
        "\n```\n"
    )
    rep = p.from_text(text)
    assert len(rep.calls) == 1 and rep.calls[0].name == "soul_append"
    assert "Sure!" in rep.leftover_text
    multi = '```json\n{"tool_calls": [{"name": "read_file", "arguments": {"path": "a"}}, {"name": "list_dir", "arguments": {"path": "."}}]}\n```'
    rep = p.from_text(multi)
    assert [c.name for c in rep.calls] == ["read_file", "list_dir"]


def test_parse_react():
    p = ToolCallParser()
    text = (
        "Thought: I should save this.\n"
        "Action: soul_append\n"
        "Action Input: {\"section\": \"Pets\", \"text\": \"rabbit Bnuy\"}\n"
    )
    rep = p.from_text(text)
    assert rep.calls[0].name == "soul_append"
    assert rep.calls[0].args["section"] == "Pets"
    assert rep.calls[0].source == "react"


def test_parse_malformed_sets_flag():
    p = ToolCallParser()
    rep = p.from_text('```json\n{"name": "soul_append", "arguments": {"text": oops}}\n```')
    # unquoted value: repair tries; either parsed or flagged malformed
    assert rep.had_malformed or rep.calls[0].name == "soul_append"
    rep2 = p.from_text('```json\n[[["garbage"]]]\n```')
    assert rep2.calls == [] and not rep2.had_malformed  # valid JSON, not a tool call


def test_native_passthrough():
    p = ToolCallParser()
    calls = p.from_native([{"id": "c1", "function": {"name": "x", "arguments": "{\"a\": 1}"}}])
    assert calls[0].args == {"a": 1} and calls[0].id == "c1"
    bad = p.from_native([{"function": {"name": "y", "arguments": "nope not json {"}}])
    assert bad[0].parse_error


# ------------------------------------------------------------------ validate
def test_validator_coercion_and_required():
    reg = build_default_registry()
    v = ToolCallValidator(reg)
    r = v.validate(ParsedCall(name="run_command", args={"command": "ls", "timeout": "12"}))
    assert r.ok and r.args["timeout"] == 12.0
    r2 = v.validate(ParsedCall(name="soul_append", args={"section": "user", "text": "x"}))
    assert r2.ok
    r3 = v.validate(ParsedCall(name="soul_append", args={"section": "user"}))
    assert not r3.ok and "text" in r3.error_text()
    r4 = v.validate(ParsedCall(name="not_a_tool", args={}))
    assert not r4.ok and "not_a_tool" in r4.error_text()
    r5 = v.validate(ParsedCall(name="list_dir", args={"path": ".", "limit": "30"}))
    assert r5.ok and r5.args["limit"] == 30
    r6 = v.validate(ParsedCall(name="soul_search", args={"query": "x", "parse_error_none": "extra"}))
    assert r6.ok  # extra tolerated


def test_validator_section_normalization():
    reg = build_default_registry()
    v = ToolCallValidator(reg)
    r = v.validate(ParsedCall(name="soul_append", args={"section": "important_notes", "text": "q"}))
    assert r.ok and r.args["section"] == "Important Notes"
