import asyncio

from ohmyllm.plain import PlainApp


def make_app(svc):
    app = PlainApp(svc)
    return app


def test_plain_memory_turn(oml_env, svc):
    app = make_app(svc)
    asyncio.run(app._turn("hello my name is PlainTesta"))
    assert "PlainTesta" in svc.soul.read()
    assert (app.sess.messages[0]["content"]) == "hello my name is PlainTesta"
    # session saved to disk
    loaded = svc.sessions.get(app.sess.id)
    assert loaded and loaded.message_count >= 2


def test_plain_slash_navigation(oml_env, svc):
    app = make_app(svc)
    assert asyncio.run(app._slash("/help")) is False
    assert asyncio.run(app._slash("/mode safe")) is False
    assert svc.config.permission_mode.value == "safe"
    asyncio.run(app._slash("/mode sandboxed"))
    assert asyncio.run(app._slash("/model")) is False  # lists
    assert asyncio.run(app._slash("/backend")) is False
    quit_res = asyncio.run(app._slash("/quit"))
    assert quit_res is True
    assert asyncio.run(app._slash("/bogus")) is False


def test_plain_export_and_chats(oml_env, svc):
    app = make_app(svc)
    asyncio.run(app._turn("say something worth exporting"))
    asyncio.run(app._slash("/export json"))
    sess = svc.sessions.get(app.sess.id)
    assert any(p.endswith(".json") for p in sess.exported_paths)
    asyncio.run(app._slash("/chats"))
    asyncio.run(app._slash("/rename my great chat"))
    assert svc.sessions.get(app.sess.id).title == "my great chat"


def test_plain_write_requires_approval_denied(oml_env, svc):
    app = make_app(svc)
    be = svc.backend()
    from ohmyllm.backends.base import ChatChunk

    call = {"id": "p1", "function": {"name": "write_file",
            "arguments": {"path": "sneaky.txt", "content": "x"}}}
    be.script = [
        [ChatChunk(content="", tool_calls=[call], done=True)],
        [ChatChunk(content="ok then."), ChatChunk(done=True)],
    ]

    async def deny(req):
        from ohmyllm.safety import ApprovalOutcome

        return ApprovalOutcome.DENIED

    rt = svc.runtime()
    res = asyncio.run(rt.run_turn(app.sess, "try to write", prompt_fn=deny))
    assert not (svc.workspace / "sneaky.txt").exists()
    assert res.text == "ok then."
