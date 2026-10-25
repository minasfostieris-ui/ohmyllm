from ohmyllm.config import (
    BackendName,
    Config,
    ConfigStore,
    GenerationSettings,
    PermissionMode,
    tomlio,
)


def test_defaults_and_enums():
    cfg = Config()
    assert cfg.permission_mode is PermissionMode.SANDBOXED
    assert cfg.default_backend is BackendName.OLLAMA
    assert PermissionMode.FULL_ACCESS.rank > PermissionMode.DANGEROUS.rank


def test_toml_roundtrip(oml_env):
    store = ConfigStore(oml_env["home"] / "config.toml")
    cfg = store.load()
    cfg.default_backend = BackendName.LMSTUDIO
    cfg.permission_mode = PermissionMode.STRICT
    cfg.generation.temperature = 0.21
    cfg.generation.num_ctx = 8192
    cfg.ollama.default_model = "qwen2.5:7b-instruct"
    cfg.model_settings["ollama:qwen2.5:7b-instruct"] = GenerationSettings(temperature=0.1, num_ctx=16000)
    cfg.safety.command_allowlist_extra = ["just", "htop"]
    cfg.workspace = "/tmp/whatever"
    store.save(cfg)

    raw = (oml_env["home"] / "config.toml").read_text()
    assert "qwen2.5:7b-instruct" in raw
    assert "[model_settings." not in raw.replace('model_settings."ollama:qwen2.5:7b-instruct"', "MS")

    back = ConfigStore(oml_env["home"] / "config.toml").load()
    assert back.default_backend is BackendName.LMSTUDIO
    assert back.permission_mode is PermissionMode.STRICT
    assert back.generation.temperature == 0.21
    assert back.ollama.default_model == "qwen2.5:7b-instruct"
    assert back.safety.command_allowlist_extra == ["just", "htop"]
    assert back.workspace == "/tmp/whatever"
    eff = back.settings_for(BackendName.OLLAMA, "qwen2.5:7b-instruct")
    assert eff.temperature == 0.1 and eff.num_ctx == 16000
    other = back.settings_for(BackendName.OLLAMA, "other-model")
    assert other.temperature == 0.21


def test_corrupt_config_degrades_with_problems(oml_env):
    p = oml_env["home"] / "config.toml"
    p.write_text("this is not [ toml at all", encoding="utf-8")
    store = ConfigStore(p)
    cfg = store.load()
    assert cfg.permission_mode is PermissionMode.SANDBOXED
    assert store.last_problems


def test_invalid_values_partial_repair(oml_env):
    p = oml_env["home"] / "config.toml"
    p.write_text('permission_mode = "nonsense"\n[generation]\ntemperature = 0.5\n', encoding="utf-8")
    store = ConfigStore(p)
    cfg = store.load()
    assert cfg.generation.temperature == 0.5  # valid section kept
    assert store.last_problems  # invalid mode reported


def test_emitter_edge_cases():
    data = {"plain": 1, "quoted key": "v", "list": ["a", "b"], "none": None, "sub": {"x": True}}
    out = tomlio.dumps(data)
    assert '"quoted key"' in out and "[sub]" in out
    import tomllib

    parsed = tomllib.loads(out)
    assert parsed["plain"] == 1 and parsed["sub"]["x"] is True and "none" not in parsed
