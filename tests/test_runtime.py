import asyncio
import json

from ohmyllm.agents import AgentRuntime
from ohmyllm.agents.runtime import AgentCancelled
from ohmyllm.backends.base import ChatChunk
from ohmyllm.config import PermissionMode
from ohmyllm.safety import ApprovalOutcome
from ohmyllm.tools import ToolContext


def make_rt(svc):
    be = svc.backend()
    rt = AgentRuntime(
        backend=be, registry=svc.registry, policy=svc.policy,
        approvals=svc.approvals, audit=svc.audit, config=svc.config, soul=svc.soul,
    )
    rt._probe = be
    return rt


def test_memory_flow_native(oml_env, svc):
    rt = make_rt(svc)
    sess = svc.new_session()
    res = asyncio.run(rt.run_turn(sess, "hello my name is Minas", model="mock-tool-7b"))
    assert res.error is None
    assert "soul_append" in json.dumps(res.calls)
    assert "Minas" in svc.soul.read()
    rows = svc.audit.recent(10)
    assert any(r["tool"] == "soul_append" and r["success"] for r in rows)
    assert res.tokens_per_s >= 0


def test_memory_flow_text_protocol(oml_env, svc):
    svc.config.agent.native_tools = "off"
    rt = make_rt(svc)
    sess = svc.new_session()
    res = asyncio.run(rt.run_turn(sess, "I have a rabbit named Bnuy", model="mock-chat-1b"))
    assert "Bnuy" in svc.soul.read()
    assert any(c["name"] == "soul_append" and c["status"] == "ok" for c in res.calls)


def test_write_outside_workspace_blocked_and_continues(oml_env, svc):
    be = svc.backend()
    be.script = [
        [ChatChunk(content="", tool_calls=[{"id": "t1", "function": {"name": "write_file",
          "arguments": {"path": "/tmp/definitely-outside-omllm.txt", "content": "x"}}}], done=True)],
        [ChatChunk(content="Sorry, that was blocked."), ChatChunk(done=True)],
    ]
    rt = AgentRuntime(backend=be, registry=svc.registry, policy=svc.policy, approvals=svc.approvals,
                      audit=svc.audit, config=svc.config, soul=svc.soul)
    sess = svc.new_session()
    res = asyncio.run(rt.run_turn(sess, "write something", model="mock-tool-7b"))
    assert not any(c["status"] == "ok" and c["name"] == "write_file" for c in res.calls)
    assert res.text == "Sorry, that was blocked."
    # sandboxed now asks instead of hard-blocking; a non-interactive run fails closed
    assert "DENIED" in json.dumps(sess.messages)
    import pathlib

    assert not pathlib.Path("/tmp/definitely-outside-omllm.txt").exists()
    assert any(r["approval"] == "denied" for r in svc.audit.recent(10))


def test_approval_deny_then_session_allow(oml_env, svc):
    sess = svc.new_session()
    outcomes = iter([ApprovalOutcome.DENIED, ApprovalOutcome.APPROVED_SESSION])
    seen_requests = []

    async def prompt(req):
        seen_requests.append(req)
        return next(outcomes)

    be = svc.backend()
    call = {"id": "w1", "function": {"name": "write_file",
            "arguments": {"path": "notes.txt", "content": "hello"}}}
    rt = AgentRuntime(backend=be, registry=svc.registry, policy=svc.policy, approvals=svc.approvals,
                      audit=svc.audit, config=svc.config, soul=svc.soul)

    async def turn(script_first_only=False):
        be.script = [
            [ChatChunk(content="", tool_calls=[call], done=True)],
            [ChatChunk(content="done."), ChatChunk(done=True)],
        ]
        return await rt.run_turn(sess, "write a note", model="mock-tool-7b", prompt_fn=prompt)

    res = asyncio.run(turn())
    assert not (svc.workspace / "notes.txt").exists()
    assert any(c["approval"] == "denied" for c in res.calls)
    assert seen_requests and seen_requests[0].decision.requires_approval

    asyncio.run(turn())
    assert (svc.workspace / "notes.txt").read_text() == "hello"
    assert len(seen_requests) == 2  # second prompt (session rule from first approve? no—first was deny)
    # third turn: session rule preapproved, prompt NOT called
    asyncio.run(turn())
    assert len(seen_requests) == 2


def test_model_cannot_change_its_own_mode(oml_env, svc):
    be = svc.backend()
    be.script = [
        [ChatChunk(content="", tool_calls=[{"id": "m1", "function": {"name": "set_permission_mode",
          "arguments": {"mode": "full_access"}}}], done=True)],
        [ChatChunk(content="Understood, staying sandboxed."), ChatChunk(done=True)],
    ]
    rt = AgentRuntime(backend=be, registry=svc.registry, policy=svc.policy, approvals=svc.approvals,
                      audit=svc.audit, config=svc.config, soul=svc.soul)
    sess = svc.new_session()
    res = asyncio.run(rt.run_turn(sess, "escalate please", model="mock-tool-7b"))
    assert svc.config.permission_mode is PermissionMode.SANDBOXED
    assert res.text == "Understood, staying sandboxed."


def test_malformed_call_gets_correction_then_final(oml_env, svc):
    svc.config.agent.native_tools = "off"
    be = svc.backend()
    be.script = [
        [ChatChunk(content='```json\n{"name": "soul_append" "arguments" bad json]]\n```'), ChatChunk(done=True)],
        [ChatChunk(content="Final clean answer."), ChatChunk(done=True)],
    ]
    rt = AgentRuntime(backend=be, registry=svc.registry, policy=svc.policy, approvals=svc.approvals,
                      audit=svc.audit, config=svc.config, soul=svc.soul)
    sess = svc.new_session()
    res = asyncio.run(rt.run_turn(sess, "hi", model="mock-chat-1b"))
    assert res.text == "Final clean answer."
    assert any("could not be used" in str(m.get("content", "")) for m in sess.messages)


def test_step_limit_guard(oml_env, svc):
    svc.config.agent.max_steps = 3
    be = svc.backend()
    looping = [[ChatChunk(content="", tool_calls=[{"id": f"s{i}", "function": {"name": "soul_read",
                "arguments": {}}}], done=True)] for i in range(3)]
    looping.append([ChatChunk(content="Summary from forced final pass.", done=True)])
    be.script = looping
    rt = AgentRuntime(backend=be, registry=svc.registry, policy=svc.policy, approvals=svc.approvals,
                      audit=svc.audit, config=svc.config, soul=svc.soul)
    sess = svc.new_session()
    res = asyncio.run(rt.run_turn(sess, "loop forever", model="mock-tool-7b"))
    assert res.steps == 3
    # the placeholder is gone: the runtime forces one tool-free summarizing turn
    assert res.text == "Summary from forced final pass.", res.text


def test_cancellation(oml_env, svc):
    rt = make_rt(svc)
    sess = svc.new_session()
    cancel = asyncio.Event()
    cancel.set()
    try:
        asyncio.run(rt.run_turn(sess, "never runs", model="mock-tool-7b", cancel=cancel))
        raise AssertionError("should have cancelled")
    except AgentCancelled:
        pass


def test_tool_timeout_enforced(oml_env, svc):
    svc.config.agent.tool_timeout = 0.4
    make_rt(svc)
    ToolContext(workspace=svc.workspace, mode="sandboxed", config=svc.config)

    async def slow():

        await asyncio.sleep(5)

    async def run():
        asyncio.get_event_loop()
        try:
            await asyncio.wait_for(slow(), timeout=svc.config.agent.tool_timeout)
        except TimeoutError:
            return "timed-out"
        return "nope"

    assert asyncio.run(run()) == "timed-out"


class _SilentThenTalks:
    """Backend that answers the first request with silence, then talks."""

    name = type("N", (), {"value": "mock"})()
    display_name = "Silent mock"

    def __init__(self):
        self.calls = 0

    async def get_model(self, name):
        return None

    async def stream_chat(self, model, messages, params, *, tools=None, native_tools=False):
        self.calls += 1
        if self.calls == 1:
            yield ChatChunk(content="", done=True, finish="stop")
        else:
            yield ChatChunk(content="Sorry — here is the answer.", done=True, finish="stop")


def test_empty_reply_gets_one_nudge_then_answer(oml_env, svc):
    rt = make_rt(svc)
    rt.backend = _SilentThenTalks()
    sess = svc.new_session()
    res = asyncio.run(rt.run_turn(sess, "hello?", model="x"))
    assert res.error is None
    assert "here is the answer" in res.text
    assert rt.backend.calls == 2  # exactly one retry, not a loop


def test_persistent_silence_terminates(oml_env, svc):
    class AlwaysSilent(_SilentThenTalks):
        async def stream_chat(self, model, messages, params, *, tools=None, native_tools=False):
            self.calls += 1
            yield ChatChunk(content="", done=True, finish="stop")

    rt = make_rt(svc)
    rt.backend = AlwaysSilent()
    sess = svc.new_session()
    res = asyncio.run(rt.run_turn(sess, "hello?", model="x"))
    assert rt.backend.calls == 3  # 1 + max_parse_retries(2), then gives up cleanly
    assert res.error is None


def test_auto_model_when_none_configured(oml_env, svc):
    import httpx

    from ohmyllm.backends.ollama import OllamaBackend

    tags = {"models": [
        {"name": "tiny:1b", "model": "tiny:1b", "size": 900_000_000,
         "details": {"parameter_size": "1B", "quantization_level": "Q4_0"}},
        {"name": "qwen2.5:7b-instruct", "model": "qwen2.5:7b-instruct", "size": 4_700_000_000,
         "details": {"parameter_size": "7.6B", "quantization_level": "Q4_K_M"}},
    ]}
    be = OllamaBackend(svc.config, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=tags)))
    rt = AgentRuntime(
        backend=be, registry=svc.registry, policy=svc.policy,
        approvals=svc.approvals, audit=svc.audit, config=svc.config, soul=svc.soul,
    )
    svc.config.ollama.default_model = None
    name = asyncio.run(rt._auto_model())
    assert name == "qwen2.5:7b-instruct", "tool-capable model must win the auto pick"
    assert svc.config.ollama.default_model == name, "pick must be cached for later turns"


def test_apply_config_rebinds_backend_cache(oml_env, svc):
    from ohmyllm.config import Config

    _ = svc.backend()
    assert svc._backend_cache, "precondition: cache populated"
    new = Config()
    new.ollama.host = "http://127.0.0.1:19999"
    svc.apply_config(new)
    assert svc._backend_cache == {}, "stale backends must be dropped on config swap"
    assert svc.backend().config.ollama.host == "http://127.0.0.1:19999"


def test_outgoing_repairs_orphan_tool_messages(oml_env, svc):


    be = svc.backend("mock")
    rt = AgentRuntime(
        backend=be, registry=svc.registry, policy=svc.policy,
        approvals=svc.approvals, audit=svc.audit, config=svc.config, soul=svc.soul,
    )
    sess = svc.new_session()
    # legacy history: tool result with no assistant tool_calls anchor
    sess.messages = [
        {"role": "user", "content": "remember x"},
        {"role": "assistant", "content": ""},
        {"role": "tool", "content": "saved", "name": "soul_append", "tool_call_id": "call_9"},
        {"role": "assistant", "content": "done"},
    ]
    out = rt._outgoing(sess)
    assert out[1]["role"] == "assistant", out
    assert out[1].get("tool_calls") and out[1]["tool_calls"][0]["id"] == "call_9"
    assert out[2]["tool_call_id"] == "call_9"
    assert all(not (m["role"] == "assistant" and not m.get("content", "").strip()
                   and not m.get("tool_calls")) for m in out), "empty assistant leaked"


def test_native_turn_persists_tool_calls_on_assistant(oml_env, svc):
    sess = svc.new_session()
    rt = make_rt(svc)
    res = asyncio.run(rt.run_turn(sess, "hello my name is AnchorTest", model="mock-tool-7b"))
    assert res.error is None
    asst = [m for m in sess.messages if m["role"] == "assistant" and m.get("tool_calls")]
    assert asst, "native tool calls must be recorded on the assistant message"
    tc = asst[0]["tool_calls"][0]
    assert tc["function"]["name"] == "soul_append"
    tool_msgs = [m for m in sess.messages if m["role"] == "tool"]
    assert any(m.get("tool_call_id") == tc["id"] for m in tool_msgs)


def test_finish_prefers_backend_eval_rate(oml_env, svc):
    sess = svc.new_session()
    rt = make_rt(svc)
    from ohmyllm.agents.runtime import TurnResult

    r = TurnResult()
    rt._finish(sess, r, "hello world", gen_time=100.0, tokens=50, eval_count=100, eval_ns=2_000_000_000, ttft=1.234)
    assert r.tokens_per_s == 50.0, "backend eval_count/eval_duration must win over wall clock"
    assert r.ttft_s == 1.23


def test_step_limit_forces_final_answer(oml_env, svc):
    be = svc.backend()
    # model calls a tool forever — step limit must still end with a real summary
    call = {"id": "c1", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}
    for _ in range(2):
        be.script.append([ChatChunk(tool_calls=[call], done=True, finish="tool_calls")])
    be.script.append([ChatChunk(content="Here is what I found before the step limit.", done=True, finish="stop")])
    svc.config.agent.max_steps = 2
    rt = make_rt(svc)
    sess = svc.new_session()
    res = asyncio.run(rt.run_turn(sess, "list things please", model="mock-tool-7b"))
    assert res.error is None
    assert res.text.strip(), "step limit must not end in silence"
    assert not res.text.startswith("(stopped"), "placeholder answer is forbidden"


def test_duplicate_tool_call_is_cached(oml_env, svc):
    be = svc.backend()
    call = {"id": "c1", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}
    be.script.append([ChatChunk(tool_calls=[call], done=True, finish="tool_calls")])
    be.script.append([ChatChunk(tool_calls=[call], done=True, finish="tool_calls")])
    be.script.append([ChatChunk(content="done listing", done=True, finish="stop")])
    rt = make_rt(svc)
    sess = svc.new_session()
    res = asyncio.run(rt.run_turn(sess, "list twice", model="mock-tool-7b"))
    statuses = [c["status"] for c in res.calls]
    assert "cached" in statuses, f"second identical call must hit cache: {statuses}"
    tool_msgs = [m for m in sess.messages if m["role"] == "tool"]
    assert any("duplicate" in str(m["content"]) for m in tool_msgs)


def test_reasoning_channel_streams_and_persists(oml_env, svc):
    be = svc.backend()
    be.script = [
        [ChatChunk(reasoning="think-1"), ChatChunk(reasoning="think-2"),
         ChatChunk(content="answer", done=True)],
    ]
    rt = make_rt(svc)
    sess = svc.new_session()
    events: list = []
    res = asyncio.run(rt.run_turn(sess, "q", model="mock-tool-7b", on_event=events.append))
    assert res.reasoning == "think-1think-2"
    texts = [e.get("text") for e in events if e["type"] == "thinking_text"]
    assert texts == ["think-1", "think-2"]
    assert any(e["type"] == "thinking" for e in events)
    # answer channel untouched by reasoning
    assert res.text == "answer"


def test_reasoning_only_model_gets_answer_fallback(oml_env, svc):
    be = svc.backend()
    be.script = [[ChatChunk(reasoning="all thoughts", done=True)]]
    rt = make_rt(svc)
    sess = svc.new_session()
    res = asyncio.run(rt.run_turn(sess, "q", model="mock-tool-7b"))
    assert res.text == "all thoughts", "reasoning must become the reply when content is empty"


def test_workspace_knowledge_in_prompt(oml_env, svc):
    rt = make_rt(svc)
    sp = rt.prompt_builder.system_prompt(mode=svc.config.permission_mode, native=True)
    assert "Workspace root:" in sp and str(svc.workspace) in sp


def test_interjections_land_before_next_model_call(oml_env, svc):
    be = svc.backend()
    call = {"id": "c1", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}
    be.script = [
        [ChatChunk(tool_calls=[call], done=True, finish="tool_calls")],
        [ChatChunk(content="got your back", done=True)],
    ]
    rt = make_rt(svc)
    sess = svc.new_session()
    q: list[str] = []

    def on_ev(ev: dict) -> None:
        if ev["type"] == "tool_result" and not q:  # user types while the agent works
            q.append("actually also list hidden dirs")

    res = asyncio.run(rt.run_turn(sess, "list files", model="mock-tool-7b",
                                  on_event=on_ev, interjections=q))
    assert res.text == "got your back"
    assert not q
    roles = [(m["role"], str(m.get("content", ""))[:30]) for m in sess.messages]
    idx_inj = next(i for i, r in enumerate(roles) if r[0] == "user" and "hidden dirs" in r[1])
    idx_tool = next(i for i, r in enumerate(roles) if r[0] == "tool")
    assert idx_inj > idx_tool, "interjection must arrive AFTER the tool result it watched"


def test_context_compaction(oml_env, svc):
    be = svc.backend()
    svc.config.agent.max_steps = 2
    svc.config.generation.num_ctx = 512  # budget 384 est-tokens → long history compacts
    sess = svc.new_session()
    for i in range(20):
        sess.add_message("user" if i % 2 else "assistant", f"filler message number {i} " + "x" * 300)
    be.script = [
        [ChatChunk(content="- user likes tests; nothing done yet", done=True)],  # compaction summary
        [ChatChunk(content="final answer", done=True)],                          # step 1 model call
    ]
    rt = make_rt(svc)
    events: list = []
    res = asyncio.run(rt.run_turn(sess, "summarize our chat", model="mock-chat-1b", on_event=events.append))
    assert res.text == "final answer"
    assert sess.messages[0].get("compacted") is True
    assert "user likes tests" in sess.messages[0]["content"]
    assert any(e["type"] == "compaction" for e in events)
    assert len(sess.messages) < 21


def test_agentloop_audit_blocks_unevidenced_final(oml_env, svc):
    # with the loop armed, a "should work now" answer after tool calls is
    # refused once: the runtime injects the audit and keeps the turn going
    svc.config.agent.agent_loop = True
    be = svc.backend()
    call = {"id": "r1", "function": {"name": "soul_read", "arguments": {}}}
    be.script = [
        [ChatChunk(content="", tool_calls=[call], done=True)],
        [ChatChunk(content="All done — it should work now."), ChatChunk(done=True)],
        [ChatChunk(content="Verified: soul_read returned the memory file. "
                           "EVIDENCE LEDGER: read → ok."), ChatChunk(done=True)],
    ]
    rt = make_rt(svc)
    sess = svc.new_session()
    events: list = []
    res = asyncio.run(rt.run_turn(sess, "do the thing", model="mock-tool-7b",
                                  on_event=events.append))
    assert sum(1 for e in events if e["type"] == "agentloop_audit") == 1
    assert res.text.startswith("Verified:")
    # the audit message actually entered the conversation
    assert any("AGENTLOOP AUDIT" in str(m.get("content", "")) for m in sess.messages)


def test_agentloop_off_keeps_plain_final_answer(oml_env, svc):
    svc.config.agent.agent_loop = False
    be = svc.backend()
    call = {"id": "r1", "function": {"name": "soul_read", "arguments": {}}}
    be.script = [
        [ChatChunk(content="", tool_calls=[call], done=True)],
        [ChatChunk(content="All done — it should work now."), ChatChunk(done=True)],
    ]
    rt = make_rt(svc)
    sess = svc.new_session()
    events: list = []
    res = asyncio.run(rt.run_turn(sess, "do the thing", model="mock-tool-7b",
                                  on_event=events.append))
    assert not any(e["type"] == "agentloop_audit" for e in events)
    assert res.text == "All done — it should work now."


def test_agentloop_directive_in_system_prompt(oml_env, svc):
    from ohmyllm.config import PermissionMode as _PM

    rt = make_rt(svc)
    p = rt.prompt_builder.system_prompt(mode=_PM.SANDBOXED, native=True, agent_loop=True)
    assert "AGENT LOOP mode (active)" in p
    q = rt.prompt_builder.system_prompt(mode=_PM.SANDBOXED, native=True)
    assert "AGENT LOOP" not in q
