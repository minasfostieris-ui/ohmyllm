import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from ohmyllm import bridge as B

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


class Client:
    def __init__(self, port: int, token: str) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.token = token

    def req(self, method: str, path: str, body: dict | None = None, auth: bool = True):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method)
        r.add_header("Content-Type", "application/json")
        if auth:
            r.add_header("X-OMLLM-Token", self.token)
        try:
            resp = urllib.request.urlopen(r, timeout=60)
            if "text/event-stream" in (resp.headers.get("Content-Type") or ""):
                return resp  # live stream
            with resp:
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            return {"__status__": e.code, **json.loads(e.read() or b"{}")}

    def get(self, p, auth=True):
        return self.req("GET", p, auth=auth)

    def post(self, p, body=None, auth=True):
        return self.req("POST", p, body or {}, auth=auth)

    def patch(self, p, body=None):
        return self.req("PATCH", p, body or {})

    def delete(self, p):
        return self.req("DELETE", p)



@pytest.fixture()
def bridge(oml_env, svc):
    """Boot the bridge against the isolated home; default backend is mock."""
    holder: dict = {}
    stop = threading.Event()

    def run() -> None:
        # ephemeral port; capture printed banner via port file
        B.serve(workspace=str(oml_env["ws"]), port=0, write_port_file=str(oml_env["home"] / "p.port"), stop=stop)

    th = threading.Thread(target=run, daemon=True)
    th.start()
    port_file = oml_env["home"] / "p.port"
    deadline = time.time() + 60  # macOS CI runners can take ~30s cold
    while time.time() < deadline:
        if port_file.exists():
            info = port_file.read_text().strip()
            if len(info.split(" ", 1)) == 2:
                holder["info"] = info
                break
        time.sleep(0.02)
    assert "info" in holder, "bridge never wrote port file"
    port, token = holder["info"].split(" ", 1)
    c = Client(int(port), token)
    # wait until the app answers
    for _ in range(100):
        try:
            if c.get("/api/health", auth=False)["ok"]:
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    else:
        pytest.fail("bridge health never came up")
    yield c
    stop.set()
    th.join(timeout=10)
    assert not th.is_alive(), "bridge thread refused to stop"


