# Prompt for Claude — OhMyLLM takeover

Repo `/data/home-overflow/Downloads/Projects/ohmyllm` (main, HEAD `5eb476a`,
407 tests green locally, CI 12/13 — windows py3.11 red). First read
`HANDOFF-FAILURES.md` in the repo root — the previous omp agent's honest
failure report. Then:

## Fix, in this order

1. **GREY → CYAN (root cause known).** Every band/tint in
   `src/ohmyllm/tui/app.tcss` uses `background: $primary 60%`. 60% alpha over
   the dark `$surface` composites to a dark blue-grey (#0c548b) — THAT is the
   "grey" the user screams about. Replacing `$primary` with #00e5ff did not
   help because the alpha wash stays grey-ish. Fix: SOLID, no-alpha cyan
   values on `.band`, focus tints, Button chips, Switch, CommandPalette,
   scrollbar slider (dark text on bright cyan where needed). One CSS pass →
   `timeout 240 uv tool install . --force -q` → user restarts oml → USER
   screenshot proves it.

2. **#chat-input ("ask anything"):** must be visibly cyan and look good. The
   previous agent's `border: solid #00e5ff` was verified only on the STYLE
   object, never rendered pixels. Verify via headless compositor dump
   (`app.run_test(size=(106,34))` → `replace_screen("chat")` →
   `screen._compositor.render_strips()` → read `seg.style.bgcolor` per cell)
   AND the user's screenshot.

3. **"Same issue" (dashes):** ask the user to Ctrl+Q + relaunch `oml` and
   paste ONE screenshot of models + chat. Their own tab has NEVER been
   captured on the current build. Suspects still carrying Textual default
   glyph borders: OptionList (slash menu), Select (settings/wizard), TextArea
   (soul editor), Tooltip, DataTable header, blockquote `border-left`,
   Collapsible `border-top`. "solid" borders = background-painted (safe);
   tall/round/heavy = glyphs (dashed in their terminal).

4. **Side bars — ASK the user.** They said "IF YOU REMOVE THE SIDE BARS. IM
   DELETING THE PROJECT"; the previous agent then removed the models-table
   scrollbar (`scrollbar-size: 0 0`). Clarify whether that counts, and if
   they want it back, restore it PAINTED cyan (Textual 8.2.8 renders thumbs
   from the CONTAINER's `scrollbar-*` styles, not a `ScrollBar` selector).

5. **CI red — windows py3.11:** `tests/test_bridge.py::test_cancel_persists_turn_and_unlocks_stream`
   fails with messages=0 after the 5s poll → the save NEVER lands on Windows
   (not a timing flake). Lead: `bridge.py` `work()` finally does
   `sessions.save(sess)` inside `try/except Exception: pass` — a swallowed
   Windows PermissionError (likely os.replace/temp-file rename racing the
   concurrent `/api/chats` readers, including the test's own polling GET)
   silently loses the turn. Don't just widen the poll — make the failure
   visible (log/retry the save) and fix the write path.

## Hard rules
- NEVER push without the user's visual approval of the RUNNING app. The
  previous agent pushed after being told "NO YOU'RE NOT PUSHING THIS" — that
  trust must be rebuilt, not spent.
- NO computer/keyboard automation (revoked; keystrokes leaked into the user's
  live sessions). Evidence = headless compositor dumps + user screenshots.
- Gates before every commit: `timeout 400 .venv/bin/pytest tests/ -q
  --tb=line -p no:cacheprovider --junit-xml=/tmp/jx.xml` then PARSE THE XML
  (console summary lies); `NO_COLOR=1 .venv/bin/ruff check src tests
  scripts`; `NO_COLOR=1 .venv/bin/python -m mypy src/ohmyllm`;
  `env -u NO_COLOR TERM=xterm-256color timeout 200
  .venv/bin/python scripts/tui_smoke.py` → TUI-OK; `timeout 240 uv tool
  install . --force -q`.
- Every commit ends with the trailer:
  `Co-authored-by: QwenLM <qwenlm@users.noreply.github.com>`
- NEVER commit/push the API key (`~/.ohmyllm/config.toml`, 600).
- Keep ALL existing visuals except the cloud model-settings UI (done,
  approved).
- Band children MUST use `width: 1fr` (`100%` over-resolves and paints over
  the band's right padding — proven in the user's terminal).
- No filler/reasoning noise in visible output (the user despises it).
