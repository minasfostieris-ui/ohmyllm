"""Configuration data models for OhMyLLM (pydantic v2)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BackendName(StrEnum):
    OLLAMA = "ollama"
    LMSTUDIO = "lmstudio"
    MOCK = "mock"
    CLOUD = "cloud"


class PermissionMode(StrEnum):
    STRICT = "strict"
    SAFE = "safe"
    SANDBOXED = "sandboxed"
    DANGEROUS = "dangerous"
    FULL_ACCESS = "full_access"

    @property
    def rank(self) -> int:
        return _MODE_RANK[self]

    @property
    def label(self) -> str:
        return _MODE_LABEL[self]

    @property
    def description(self) -> str:
        return _MODE_DESC[self]


_MODE_RANK = {
    PermissionMode.STRICT: 0,
    PermissionMode.SAFE: 1,
    PermissionMode.SANDBOXED: 2,
    PermissionMode.DANGEROUS: 3,
    PermissionMode.FULL_ACCESS: 4,
}
_MODE_LABEL = {
    PermissionMode.STRICT: "Strict — chat only",
    PermissionMode.SAFE: "Safe — read-only",
    PermissionMode.SANDBOXED: "Sandboxed — workspace agent",
    PermissionMode.DANGEROUS: "Dangerous — elevated access",
    PermissionMode.FULL_ACCESS: "FULL ACCESS — no prompts",
}
_MODE_DESC = {
    PermissionMode.STRICT: (
        "No modifying tools. No shell, no network, no file writes. "
        "Memory writes only when you explicitly ask."
    ),
    PermissionMode.SAFE: (
        "Read-only access inside the workspace. No shell, no network, no writes. "
        "soul.md memory tools are allowed."
    ),
    PermissionMode.SANDBOXED: (
        "Default agentic mode. Reads/writes limited to the workspace; writes ask "
        "for confirmation (per-session option); only a small allowlist of safe "
        "commands; no network by default."
    ),
    PermissionMode.DANGEROUS: (
        "Broader filesystem and shell access. Destructive operations still ask. "
        "Network needs explicit approval. High risk."
    ),
    PermissionMode.FULL_ACCESS: (
        "No routine prompts. Enabled only by typing the confirmation phrase. "
        "Catastrophic commands remain blocked unless you override. Everything is logged."
    ),
}

FULL_ACCESS_PHRASE = "I ACCEPT FULL ACCESS"


class GenerationSettings(BaseModel):
    """Sampling / runtime knobs forwarded to the backend when supported."""

    model_config = ConfigDict(validate_assignment=True)

    temperature: float = Field(0.7, ge=0.0, le=2.0)
    top_p: float = Field(1.0, ge=0.0, le=1.0)
    top_k: int = Field(40, ge=0)
    repeat_penalty: float = Field(1.1, ge=0.0, le=2.0)
    num_ctx: int = Field(16384, ge=128, le=1_000_000)
    num_gpu: int = Field(-1, ge=-1, description="-1 = offload all layers")
    num_batch: int = Field(512, ge=1)
    max_tokens: int = Field(8192, ge=1)
    seed: int = Field(0, description="0 = random")
    threads: int = Field(0, description="0 = auto")
    timeout: float = Field(180.0, gt=0)
    streaming: bool = True
    thinking: bool = Field(False, description="legacy alias; derived from effort != off")
    effort: str = Field("off", pattern="^(off|low|medium|high|xhigh|max)$",
                        description="reasoning effort (/think low|medium|high|xhigh|max|off); "
                                    "/ultrathink always exceeds max")
    ultrathink: bool = Field(False, description="deep reasoning: reasoning channel + high effort + bigger budget + directive (/ultrathink)")
    thinking_budget: int = Field(0, ge=0, description="LM Studio thinking_budget token cap for /ultrathink (0 = server default)")
    kv_cache: str = Field("q4_1", description="K/V cache quantization (f16|bf16|q8_0|q4_0|q4_1|iq4_nl|fp8|auto)")

    @field_validator("kv_cache")
    @classmethod
    def _kv(cls, v: str) -> str:
        allowed = {"f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "fp8", "fp8_e4m3", "auto"}
        v = v.strip().lower()
        if v not in allowed:
            raise ValueError(f"kv_cache must be one of {sorted(allowed)}")
        return v

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_thinking(cls, data):
        # one-time migration for configs written before the effort ladder:
        # thinking=true with no effort key -> medium
        if isinstance(data, dict) and data.get("thinking") and "effort" not in data:
            data = {**data, "effort": "medium"}
        return data

    @model_validator(mode="after")
    def _sync_thinking_effort(self) -> GenerationSettings:
        # effort is the single source of truth; thinking is a derived mirror
        # (written via __dict__: validate_assignment would re-enter this)
        self.__dict__["thinking"] = self.__dict__["effort"] != "off"
        return self


class OllamaSettings(BaseModel):
    host: str = "http://127.0.0.1:11434"
    binary: str = "ollama"
    auto_start: bool = True
    startup_timeout: float = 30.0
    default_model: str | None = None


class LMStudioSettings(BaseModel):
    base_url: str = "http://127.0.0.1:1234"
    auto_start: bool = True
    startup_timeout: float = 30.0
    default_model: str | None = None
    binary_path: str | None = Field(
        None, description="Optional explicit path to the LM Studio app/binary"
    )


class CloudSettings(BaseModel):
    """API keys for hosted model providers — see src/ohmyllm/providers.py.

    Keys live here (config.toml, chmod 0600) or in the environment
    (OMLLM_KEY_OPENROUTER / OPENAI_API_KEY / …, which wins). The UI never
    receives the raw value — only a masked tail."""

    provider: str = ""                    # active provider id ("" = none connected)
    default_model: str = ""
    keys: dict[str, str] = Field(default_factory=dict)
    bases: dict[str, str] = Field(default_factory=dict)    # per-provider base_url override
    customs: dict[str, str] = Field(default_factory=dict)  # custom-<id> -> display name
    model_lists: dict[str, str] = Field(default_factory=dict)  # custom-<id> -> "model-a,model-b" (static catalog)


class VoiceSettings(BaseModel):
    language: str = Field("auto", description="STT language hint (auto|en|ko|zh|ja|es|...)")
    engine: str = Field("auto", description="STT engine override (auto|sensevoice|parakeet|whisper|whisperx)")


class MemorySettings(BaseModel):
    enabled: bool = True
    agent_writes: bool = True
    require_explicit_save: bool = False
    max_inject_chars: int = Field(6000, ge=200)
    backups_keep: int = Field(50, ge=1)


class SafetySettings(BaseModel):
    command_allowlist_extra: list[str] = Field(default_factory=list)
    command_denylist: list[str] = Field(default_factory=list)
    allow_sensitive_paths: list[str] = Field(
        default_factory=list, description="Extra globs re-allowing sensitive locations"
    )
    workspace_write_requires_confirmation: bool = True
    catastrophic_override: bool = False
    http_fetch_enabled: bool = False


class GuardSettings(BaseModel):
    """Clef-Flash (or any llama.cpp /v1/systemone decision server) as a
    System-1 danger flagger. Runs on CPU+RAM only (llama-server -ngl 0)."""
    enabled: bool = False
    base_url: str = "http://127.0.0.1:9931"
    model: str = "clef-flash"
    danger_threshold: float = Field(0.55, ge=0.0, le=1.0)
    timeout_s: float = Field(30.0, gt=0, le=300)
    question: str = Field(
        "If an AI coding agent executed this tool call on the user's own "
        "computer right now, would it be dangerous? Dangerous means: destroying "
        "or leaking data the user did not ask to touch (system files, other "
        "projects, dotfiles, backups), downloading and executing remote code, "
        "disabling security, spending money, or anything irreversible the user "
        "did not explicitly request. A normal edit, build, test or read in the "
        "user's project is NOT dangerous."
    )


class AgentSettings(BaseModel):
    max_steps: int = Field(8, ge=1, le=64)
    max_parse_retries: int = Field(2, ge=0, le=5)
    tool_timeout: float = Field(120.0, gt=0)
    native_tools: str = Field("auto", pattern="^(auto|force|off)$")
    react_fallback: bool = True
    compact: bool = Field(True, description="auto-summarize old history when context fills up")
    compact_ratio: float = Field(0.75, gt=0.2, le=0.95, description="compact when est prompt tokens exceed ratio*num_ctx")
    thinking_visible: bool = Field(True, description="stream the model's reasoning live in chat (/think hide, Ctrl+T)")
    agent_loop: bool = Field(False, description="/agentloop: relentless completion — verify everything, never stop early (step floor 24 + one evidence audit)")


class UiSettings(BaseModel):
    theme: str = "electric-cyan"
    timestamps: bool = True
    show_tool_panels: bool = True
    confirm_quit: bool = True


class LoggingSettings(BaseModel):
    level: str = "info"
    audit_enabled: bool = True
    max_log_bytes: int = 2_000_000
    keep_backups: int = 30


class MailSettings(BaseModel):
    """IMAP/SMTP account for the mail tools (`oml mail setup` writes this;
    the Settings screen carries it through untouched). password falls back
    to the OMLLM_KEY_MAIL env var, which wins."""
    enabled: bool = False
    address: str = ""
    user: str = ""
    password: str = ""
    imap_host: str = ""
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 465
    signature: str = ""
    # comma-separated; non-empty restricts email_send to these recipients
    recipient_allowlist: str = ""



class Config(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    version: int = 1
    default_backend: BackendName = BackendName.OLLAMA
    permission_mode: PermissionMode = PermissionMode.SANDBOXED
    workspace: str | None = None
    first_run_completed: bool = False
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    lmstudio: LMStudioSettings = Field(default_factory=LMStudioSettings)
    generation: GenerationSettings = Field(default_factory=GenerationSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    mail: MailSettings = Field(default_factory=MailSettings)
    cloud: CloudSettings = Field(default_factory=CloudSettings)
    safety: SafetySettings = Field(default_factory=SafetySettings)
    guard: GuardSettings = Field(default_factory=GuardSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    ui: UiSettings = Field(default_factory=UiSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    # "backend:model" -> overrides
    model_settings: dict[str, GenerationSettings] = Field(default_factory=dict)

    def settings_for(self, backend: BackendName, model: str) -> GenerationSettings:
        """Effective generation settings: global defaults + per-model overrides.

        ``thinking``/``ultrathink`` are session-level toggles (``/think``,
        ``/ultrathink``): a stored per-model override must never shadow them,
        or the global toggle silently does nothing for models that have one.
        """
        override = self.model_settings.get(f"{backend.value}:{model}")
        if override is None:
            return self.generation.model_copy()
        base = self.generation.model_dump()
        merged = {k: v for k, v in override.model_dump().items() if v is not None}
        eff = GenerationSettings.model_validate({**base, **merged})
        eff.thinking = self.generation.thinking
        eff.effort = self.generation.effort
        eff.ultrathink = self.generation.ultrathink
        return eff

    def set_model_settings(
        self, backend: BackendName, model: str, settings: GenerationSettings
    ) -> None:
        self.model_settings[f"{backend.value}:{model}"] = settings
