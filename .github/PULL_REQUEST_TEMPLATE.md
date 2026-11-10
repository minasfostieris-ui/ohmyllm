## What & why

One paragraph: the behavior you're adding or removing, and the user-facing
reason. Link the Discussions idea thread or the issue this closes.

## How it works

The interesting part: which existing pieces you extended (`Services`,
`PolicyEngine`, `ToolRegistry`, `PromptBuilder`, the bridge…) and any new
state/invariant worth naming.

## Permission & safety impact (required)

- [ ] No new tool, **or** the tool is classified in `TOOL_CLASSES` and
      gated in all five permission modes with a test in `tests/test_safety.py`
- [ ] Catastrophic commands stay blocked (also through `/yolo`)
- [ ] Nothing spawns Ollama/LM Studio or phones home by default
- [ ] No secret ever reaches stdout, logs, or the UI unmasked

## Verification

Paste the commands you ran and their real output (failures included):

```
pytest tests/ -q
ruff check src tests
python -m mypy src/ohmyllm
python scripts/tui_smoke.py
node --check src/ohmyllm/gui_static/app.js   # if the GUI changed
```

## Screenshots / recordings

For anything user-visible: before/after of the actual surface.

## Notes for the reviewer

Risks, alternatives considered, follow-ups you deliberately left out.
