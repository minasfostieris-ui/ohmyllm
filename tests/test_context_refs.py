"""D1: workspace-bounded @-references — traversal, symlinks, secrets, caps, fake git."""

from __future__ import annotations

import asyncio
import io
import subprocess

import pytest

from ohmyllm.agents import context_refs as CR
from ohmyllm.agents.context_refs import expand_context_refs


@pytest.fixture()
def ws(tmp_path):
    w = tmp_path / "ws"
    w.mkdir()
    (w / "a.txt").write_text("hello world")
    return w


def test_file_expansion(ws):
    out = expand_context_refs("see @file:a.txt please", workspace=ws)
    assert "[context @file:a.txt]" in out
    assert "hello world" in out
    assert "[/context]" in out
    assert out.startswith("see ") and out.endswith(" please")


def test_no_refs_unchanged(ws):
    text = "plain message, no references"
    assert expand_context_refs(text, workspace=ws) == text


def test_traversal_denied_and_reported(ws, tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("TOP-SECRET")
    out = expand_context_refs("@file:../outside.txt", workspace=ws)
    assert "denied" in out
    assert "TOP-SECRET" not in out
    out2 = expand_context_refs("@file:../../etc/passwd", workspace=ws)
    assert "denied" in out2


def test_absolute_path_denied(ws):
    out = expand_context_refs("@file:/etc/passwd", workspace=ws)
    assert "denied" in out


def test_symlink_escape_refused(ws, tmp_path):
    target = tmp_path / "outside.txt"
    target.write_text("OUTSIDE")
    (ws / "link.txt").symlink_to(target)
    out = expand_context_refs("@file:link.txt", workspace=ws)
    assert "denied" in out
    assert "OUTSIDE" not in out


@pytest.mark.parametrize("name", [".env", ".env.local", "key.pem", "credentials.json", "secrets.yaml"])
def test_secret_blocklist(ws, name):
    (ws / name).write_text("SECRET-MATERIAL")
    out = expand_context_refs(f"@file:{name}", workspace=ws)
    assert "denied" in out
    assert "SECRET-MATERIAL" not in out


def test_per_ref_cap_truncates(ws):
    (ws / "big.txt").write_text("x" * 5000)
    out = expand_context_refs("@file:big.txt", workspace=ws, per_ref_cap=100)
    assert "[truncated]" in out
    assert out.count("x") <= 110


def test_total_cap_budget(ws):
    (ws / "one.txt").write_text("a" * 100)
    (ws / "two.txt").write_text("b" * 100)
    out = expand_context_refs("@file:one.txt @file:two.txt", workspace=ws,
                              per_ref_cap=1000, total_cap=150)
    assert "[truncated]" in out
    assert out.count("b") <= 60
    exhausted = expand_context_refs("@file:one.txt", workspace=ws, total_cap=0)
    assert "budget exhausted" in exhausted


def test_folder_listing(ws):
    pkg = ws / "pkg"
    pkg.mkdir()
    (pkg / "a.txt").write_text("ALPHA")
    (pkg / ".env").write_text("SECRET")
    (pkg / "link").symlink_to(ws / "a.txt")
    out = expand_context_refs("@folder:pkg", workspace=ws)
    assert "--- a.txt ---" in out
    assert "ALPHA" in out
    assert "[skipped blocklisted file: .env]" in out
    assert "SECRET" not in out
    assert "hello world" not in out  # symlink skipped


def test_empty_folder(ws):
    (ws / "empty").mkdir()
    assert "(empty folder)" in expand_context_refs("@folder:empty", workspace=ws)


# Task #14: every allowlisted shape carries the config-neutralizing hardening.
_HARDEN_HEAD = ["git", "-c", "diff.external=", "-c", "core.attributesFile=/dev/null"]
_HARDEN_TAIL = ["--no-ext-diff", "--no-textconv"]
ALLOWED_ARGV = (
    [*_HARDEN_HEAD, "diff", *_HARDEN_TAIL],
    [*_HARDEN_HEAD, "diff", "--staged", *_HARDEN_TAIL],
    [*_HARDEN_HEAD, "show", "HEAD~1", *_HARDEN_TAIL],
)


def test_git_allowlisted_shapes_only(ws):
    seen: list[list[str]] = []

    def runner(argv):
        seen.append(list(argv))
        assert list(argv) in ALLOWED_ARGV, argv
        return f"BODY:{argv[-1]}"

    out = expand_context_refs("@diff @staged @git:HEAD~1", workspace=ws, git_runner=runner)
    assert seen == [list(a) for a in ALLOWED_ARGV]
    assert out.count("BODY:--no-textconv") == 3  # hardened tail is the last argv slot


@pytest.mark.parametrize("ref", ["../evil", "--upload-pack=evil", ""])
def test_invalid_git_ref_denied_without_runner(ws, ref):
    called = []

    def runner(argv):
        called.append(argv)
        return "x"

    out = expand_context_refs(f"@git:{ref}", workspace=ws, git_runner=runner)
    assert not called
    if ref:  # empty ref does not match the reference regex at all
        assert "denied" in out


def test_git_failure_reported(ws):
    def runner(argv):
        raise RuntimeError("git not found")

    out = expand_context_refs("@diff", workspace=ws, git_runner=runner)
    assert "[context @diff denied: git unavailable" in out


def test_runtime_expands_before_prompt(svc):
    from ohmyllm.agents.runtime import AgentRuntime

    ws = svc.workspace
    (ws / "note.txt").write_text("RUNTIME-CONTEXT-MARKER")
    seen: list[list[str]] = []

    def fake_git(argv):
        seen.append(list(argv))
        return "diff-body"

    rt = AgentRuntime(backend=svc.backend(), registry=svc.registry, policy=svc.policy,
                      approvals=svc.approvals, audit=svc.audit, config=svc.config,
                      soul=svc.soul, skills=svc.skills, git_runner=fake_git)
    sess = svc.new_session()
    res = asyncio.run(rt.run_turn(sess, "summarize @file:note.txt and @diff"))
    assert res.error is None
    first = sess.messages[0]["content"]
    assert "RUNTIME-CONTEXT-MARKER" in first
    assert "[context @file:note.txt]" in first
    assert "diff-body" in first
    assert seen == [[*_HARDEN_HEAD, "diff", *_HARDEN_TAIL]]


def test_runtime_reports_denied_ref(svc, tmp_path):
    from ohmyllm.agents.runtime import AgentRuntime

    (tmp_path / "outside.txt").write_text("OUTSIDE-SECRET")
    rt = AgentRuntime(backend=svc.backend(), registry=svc.registry, policy=svc.policy,
                      approvals=svc.approvals, audit=svc.audit, config=svc.config,
                      soul=svc.soul, skills=svc.skills)
    sess = svc.new_session()
    asyncio.run(rt.run_turn(sess, "read @file:../outside.txt"))
    first = sess.messages[0]["content"]
    assert "denied" in first
    assert "OUTSIDE-SECRET" not in first


# ------------------------- Task #14 F2: policy-sensitive parity regressions
@pytest.mark.parametrize("name", [
    "deploy.key", "id_rsa", ".env.d/prod", ".git-credentials",
    ".ssh/id_ed25519", "server.pem", ".netrc", "secrets.yaml",
])
def test_policy_sensitive_files_refused(ws, name):
    target = ws / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("SECRET-MATERIAL")
    out = expand_context_refs(f"@file:{name}", workspace=ws)
    assert "denied" in out, name
    assert "SECRET-MATERIAL" not in out


def test_folder_nested_secret_components_skipped(ws):
    cfg = ws / "config"
    (cfg / ".env.d").mkdir(parents=True)
    (cfg / "nested").mkdir()
    (cfg / ".env.d" / "prod").write_text("SECRET-ENV-PROD")
    (cfg / "nested" / "deploy.key").write_text("SECRET-KEY")
    (cfg / "readme.txt").write_text("BENIGN-README")
    out = expand_context_refs("@folder:config", workspace=ws)
    assert "BENIGN-README" in out
    assert "[skipped blocklisted file: .env.d/prod]" in out
    assert "[skipped blocklisted file: nested/deploy.key]" in out
    assert "SECRET-ENV-PROD" not in out
    assert "SECRET-KEY" not in out


def test_folder_root_with_sensitive_ancestor_refused(ws):
    d = ws / ".ssh" / "sub"
    d.mkdir(parents=True)
    (d / "note.txt").write_text("SECRET-SSH-NOTE")
    out = expand_context_refs("@folder:.ssh/sub", workspace=ws)
    assert "denied" in out
    assert "SECRET-SSH-NOTE" not in out


@pytest.mark.parametrize("ref", [
    "HEAD:.env", "HEAD:.git-credentials", "HEAD:config/deploy.key",
    "HEAD:.env.d/prod", "HEAD:id_rsa",
])
def test_git_ref_path_portion_sensitive_refused(ws, ref):
    called: list = []

    def runner(argv):
        called.append(argv)
        return "SECRET-GIT-BODY"

    out = expand_context_refs(f"@git:{ref}", workspace=ws, git_runner=runner)
    assert not called, ref
    assert "denied" in out
    assert "SECRET-GIT-BODY" not in out


def test_git_ref_benign_path_still_expands(ws):
    def runner(argv):
        return "BENIGN-SHOW-BODY"

    out = expand_context_refs("@git:HEAD:src/main.py", workspace=ws, git_runner=runner)
    assert "BENIGN-SHOW-BODY" in out


# ---------------- Task #14 F3: production git seam (fake Popen — never spawns)
class _FakeProc:
    def __init__(self, argv, kwargs, out="diff-body", err="", code=0, stall=False):
        self.argv = list(argv)
        self.kwargs = kwargs
        self.stdout = io.StringIO(out)
        self.stderr = io.StringIO(err)
        self._code = code
        self._stall = stall
        self.killed = 0

    def kill(self):
        self.killed += 1

    def wait(self, timeout=None):
        if self._stall and not self.killed:
            raise subprocess.TimeoutExpired(cmd=self.argv, timeout=timeout or 0)
        return self._code


@pytest.fixture()
def fake_popen(monkeypatch):
    created: list[_FakeProc] = []

    def factory(argv, **kwargs):
        proc = _FakeProc(argv, kwargs)
        created.append(proc)
        return proc

    monkeypatch.setattr(CR.subprocess, "Popen", factory)
    return created


def test_git_seam_hardens_argv_and_env(ws, fake_popen, monkeypatch):
    monkeypatch.setenv("OMLLM_API_TOKEN", "leak-me")
    monkeypatch.setenv("GIT_DIR", "/hostile/.git")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/hostile/gitconfig")
    # Hostile repo config on disk must not influence argv or env.
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("[diff]\n\texternal = /bin/evil\n")
    (ws / ".gitattributes").write_text("*.txt diff=evil\n")
    out = expand_context_refs("@diff @staged @git:HEAD~1", workspace=ws)
    assert out.count("diff-body") == 3
    assert len(fake_popen) == 3
    for proc, want in zip(fake_popen, ALLOWED_ARGV, strict=True):
        assert proc.argv == want
        env = proc.kwargs["env"]
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert "OMLLM_API_TOKEN" not in env
        assert "GIT_DIR" not in env
        assert not any("leak-me" in v for v in env.values())
        assert proc.kwargs["cwd"] == str(ws)


def test_git_seam_overflow_kills_and_reports_truncation(ws, monkeypatch):
    created: list[_FakeProc] = []
    big = "x" * (CR._GIT_OUTPUT_CAP + 100)

    def factory(argv, **kwargs):
        proc = _FakeProc(argv, kwargs, out=big)
        created.append(proc)
        return proc

    monkeypatch.setattr(CR.subprocess, "Popen", factory)
    text = CR._subprocess_runner(ws)(["git", "diff"])
    assert created[0].killed >= 1
    assert text.endswith("[truncated]")
    assert len(text) <= CR._GIT_OUTPUT_CAP + len("\n[truncated]")
    out = expand_context_refs("@diff", workspace=ws)
    assert "[truncated]" in out
    assert "x" * (CR._GIT_OUTPUT_CAP + 1) not in out


def test_git_seam_timeout_kills_stalled_child(ws, monkeypatch):
    monkeypatch.setattr(CR, "_GIT_TIMEOUT", 0.05)
    created: list[_FakeProc] = []

    def factory(argv, **kwargs):
        proc = _FakeProc(argv, kwargs, stall=True)
        created.append(proc)
        return proc

    monkeypatch.setattr(CR.subprocess, "Popen", factory)
    with pytest.raises(RuntimeError, match="timed out"):
        CR._subprocess_runner(ws)(["git", "diff"])
    assert created[0].killed >= 1


def test_drain_bounded_caps_incrementally():
    text, over = CR._drain_bounded(io.StringIO("abc"), 10)
    assert text == "abc" and not over
    text, over = CR._drain_bounded(io.StringIO("y" * 200_000), 100_000)
    assert over and len(text) == 100_000
