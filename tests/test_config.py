import pytest
from pydantic import ValidationError

from ohmyllm.config import (
    BackendName,
    Config,
    ConfigStore,
    GenerationSettings,
    PermissionMode,
    deep_merge,
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


def test_corrupt_config_fails_closed_without_overwriting_evidence(oml_env):
    p = oml_env["home"] / "config.toml"
    evidence = "this is not [ toml at all"
    p.write_text(evidence, encoding="utf-8")
    store = ConfigStore(p)
    cfg = store.load()
    assert cfg.permission_mode is PermissionMode.STRICT
    assert cfg.memory.agent_writes is False
    assert cfg.safety.http_fetch_enabled is False
    assert store.last_problems
    assert p.read_text() == evidence


def test_invalid_values_partial_repair(oml_env):
    p = oml_env["home"] / "config.toml"
    p.write_text('permission_mode = "nonsense"\n[generation]\ntemperature = 0.5\n', encoding="utf-8")
    store = ConfigStore(p)
    cfg = store.load()
    assert cfg.generation.temperature == 0.5  # valid section kept
    assert store.last_problems  # invalid mode reported


def test_deep_merge_preserves_nested_security_siblings():
    base = Config().model_dump(mode="json")
    merged = deep_merge(base, {"generation": {"temperature": 0.2}})
    assert merged["generation"]["temperature"] == 0.2
    assert merged["generation"]["top_p"] == base["generation"]["top_p"]
    assert merged["safety"] == base["safety"]


def test_security_change_advances_generation_and_retires_live_runtime(svc):
    runtime = svc.runtime()
    original = svc.config
    old_generation = original.policy_generation
    updated = original.model_copy(deep=True)
    updated.safety.workspace_write_requires_confirmation = not updated.safety.workspace_write_requires_confirmation
    svc.apply_config(updated)
    assert svc.config is original
    assert runtime.config is original
    assert runtime._retired is True
    assert runtime.config.safety.workspace_write_requires_confirmation == updated.safety.workspace_write_requires_confirmation
    assert svc.config.policy_generation == old_generation + 1
    assert svc.runtime()._retired is False


def test_nested_config_is_strict_for_types_extras_and_nonfinite_numbers():
    with pytest.raises(ValidationError):
        Config.model_validate({"generation": {"top_k": True}})
    with pytest.raises(ValidationError):
        Config.model_validate({"safety": {"unknown_control": True}})
    with pytest.raises(ValidationError):
        Config.model_validate({"ollama": {"startup_timeout": float("inf")}})


def test_config_path_symlinks_and_nonregular_files_fail_closed(tmp_path):
    target = tmp_path / "target.toml"
    target.write_text('permission_mode = "full_access"\n')
    linked = tmp_path / "config.toml"
    linked.symlink_to(target)
    symlinked = ConfigStore(linked).load()
    assert symlinked.permission_mode is PermissionMode.STRICT
    assert symlinked.safety.allow_remote_backends is False

    directory = tmp_path / "config-dir"
    directory.mkdir()
    nonregular = ConfigStore(directory).load()
    assert nonregular.permission_mode is PermissionMode.STRICT
    assert nonregular.memory.agent_writes is False


def test_persisted_full_access_is_deactivated_and_invalidated(oml_env):
    from ohmyllm.core import Services

    store = ConfigStore(oml_env["home"] / "config.toml")
    cfg = Config(permission_mode=PermissionMode.FULL_ACCESS, policy_generation=7)
    store.save(cfg)
    svc = Services.boot(workspace=oml_env["ws"])
    assert svc.config.permission_mode is PermissionMode.DANGEROUS
    assert svc.config.policy_generation == 8
    assert any("fresh process-local confirmation" in item for item in svc.config_store.last_problems)


def test_emitter_edge_cases():
    data = {"plain": 1, "quoted key": "v", "list": ["a", "b"], "none": None, "sub": {"x": True}}
    out = tomlio.dumps(data)
    assert '"quoted key"' in out and "[sub]" in out
    import tomllib

    parsed = tomllib.loads(out)
    assert parsed["plain"] == 1 and parsed["sub"]["x"] is True and "none" not in parsed
