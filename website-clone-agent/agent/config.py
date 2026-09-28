"""Central configuration, read from environment variables / .env.

Every knob the agent uses lives here so behaviour (models, budgets, retries)
can be changed without touching code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional
    pass

ROOT_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_json(name: str) -> dict:
    import json

    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except ValueError:
        return {}


def reload_settings() -> None:
    """Re-read .env into the shared settings object (used by the UI's reload button)."""
    try:
        from dotenv import load_dotenv

        load_dotenv(override=True)
    except ImportError:
        pass
    settings.__init__()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    # --- LLM (any OpenAI-compatible endpoint; DeepSeek by default) ---
    api_key: str = field(default_factory=lambda: _env("LLM_API_KEY") or _env("DEEPSEEK_API_KEY"))
    base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", "https://api.deepseek.com"))
    # Main model: section generation, edits.
    model: str = field(default_factory=lambda: _env("LLM_MODEL", "deepseek-flash"))
    # Cheap model: analysis, planning, repairs. Defaults to the main model.
    model_fast: str = field(default_factory=lambda: _env("LLM_MODEL_FAST") or _env("LLM_MODEL", "deepseek-flash"))
    # DeepSeek V4 thinks by default; non-thinking is faster/cheaper for codegen.
    thinking: str = field(default_factory=lambda: _env("LLM_THINKING", "disabled"))
    # Send screenshots to the model during analysis (only if the model supports images).
    # (DeepSeek V4.1 Flash is natively multimodal; falls back to text-only automatically.)
    vision: bool = field(default_factory=lambda: _env_bool("LLM_VISION", True))
    max_concurrency: int = field(default_factory=lambda: _env_int("LLM_MAX_CONCURRENCY", 4))
    # Extra provider-specific request fields as JSON, e.g. {"chat_template_kwargs": {"thinking": false}}
    extra_body: dict = field(default_factory=lambda: _env_json("LLM_EXTRA_BODY"))
    request_timeout: float = field(default_factory=lambda: _env_float("LLM_TIMEOUT", 180))
    # "auto" -> real LLM if a key is set, otherwise the offline mock.
    provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "auto"))
    # USD per 1M tokens, used for the cost report (DeepSeek flash, peak hours).
    price_input: float = field(default_factory=lambda: _env_float("LLM_PRICE_INPUT", 0.30))
    price_input_cached: float = field(default_factory=lambda: _env_float("LLM_PRICE_INPUT_CACHED", 0.006))
    price_output: float = field(default_factory=lambda: _env_float("LLM_PRICE_OUTPUT", 1.20))
    cache_enabled: bool = field(default_factory=lambda: _env_bool("LLM_CACHE", True))

    # --- pipeline budgets ---
    max_repair_attempts: int = field(default_factory=lambda: _env_int("MAX_REPAIR_ATTEMPTS", 3))
    refine_enabled: bool = field(default_factory=lambda: _env_bool("REFINE_ENABLED", True))
    refine_max_sections: int = field(default_factory=lambda: _env_int("REFINE_MAX_SECTIONS", 3))
    max_section_outline_chars: int = field(default_factory=lambda: _env_int("MAX_SECTION_CHARS", 28000))
    max_sections: int = field(default_factory=lambda: _env_int("MAX_SECTIONS", 18))

    # --- capture ---
    desktop_width: int = 1440
    desktop_height: int = 900
    mobile_width: int = 390
    mobile_height: int = 844
    max_screenshot_height: int = field(default_factory=lambda: _env_int("MAX_SCREENSHOT_HEIGHT", 12000))
    nav_timeout_ms: int = field(default_factory=lambda: _env_int("NAV_TIMEOUT_MS", 45000))
    headless: bool = field(default_factory=lambda: _env_bool("HEADLESS", True))

    # --- local runtime / preview ---
    workspace: Path = field(default_factory=lambda: Path(_env("WORKSPACE_DIR", str(ROOT_DIR / "workspace"))).resolve())
    preview_port_start: int = field(default_factory=lambda: _env_int("PREVIEW_PORT_START", 3100))

    @property
    def use_mock(self) -> bool:
        if self.provider == "mock":
            return True
        if self.provider == "auto":
            return not self.api_key
        return False

    @property
    def is_deepseek(self) -> bool:
        return "deepseek.com" in self.base_url

    @property
    def is_nvidia(self) -> bool:
        return "nvidia.com" in self.base_url

    @property
    def sites_dir(self) -> Path:
        return self.workspace / "sites"

    @property
    def cache_dir(self) -> Path:
        return self.workspace / "cache"


settings = Settings()
