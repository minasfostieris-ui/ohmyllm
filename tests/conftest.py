import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture()
def oml_env(tmp_path, monkeypatch):
    """Isolated global home + workspace for every test."""
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("OMLLM_HOME", str(home))
    monkeypatch.setenv("OMLLM_WORKSPACE", str(ws))
    from ohmyllm import paths

    paths.ensure_global_dirs()
    return {"home": home, "ws": ws}


@pytest.fixture()
def svc(oml_env):
    from ohmyllm.core import Services

    s = Services.boot()
    s.config.default_backend = "mock"
    s.config.first_run_completed = True
    s.save_config()
    return s
