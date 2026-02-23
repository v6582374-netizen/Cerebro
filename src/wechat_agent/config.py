from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


DEFAULT_SOURCE_TEMPLATES = (
    "https://rsshub.app/wechat/mp/{wechat_id}",
    "https://rsshub.rssforever.com/wechat/mp/{wechat_id}",
)
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"


@dataclass(frozen=True)
class Settings:
    db_url: str
    ai_provider: str
    openai_api_key: str | None
    openai_base_url: str | None
    openai_chat_model: str
    openai_embed_model: str
    deepseek_api_key: str | None
    deepseek_base_url: str
    deepseek_chat_model: str
    deepseek_embed_model: str
    source_templates: tuple[str, ...]
    http_timeout_seconds: int
    max_concurrency: int
    default_view_mode: str
    wechat2rss_index_url: str
    article_fetch_timeout_seconds: int
    summary_source_char_limit: int
    midnight_shift_days: int
    sync_overlap_seconds: int
    incremental_sync_enabled: bool
    source_max_candidates: int
    source_retry_backoff_ms: int
    source_circuit_fail_threshold: int
    source_cooldown_minutes: int

    def resolved_ai_provider(self) -> str:
        provider = self.ai_provider.strip().lower()
        if provider in {"openai", "deepseek"}:
            return provider
        if self.openai_api_key:
            return "openai"
        if self.deepseek_api_key:
            return "deepseek"
        return "none"

    def resolved_api_key(self) -> str | None:
        provider = self.resolved_ai_provider()
        if provider == "openai":
            return self.openai_api_key
        if provider == "deepseek":
            return self.deepseek_api_key
        return None

    def resolved_base_url(self) -> str | None:
        provider = self.resolved_ai_provider()
        if provider == "openai":
            return self.openai_base_url
        if provider == "deepseek":
            return self.deepseek_base_url
        return None

    def resolved_chat_model(self) -> str:
        provider = self.resolved_ai_provider()
        if provider == "openai":
            return self.openai_chat_model
        if provider == "deepseek":
            return self.deepseek_chat_model
        return "fallback"

    def resolved_embed_model(self) -> str | None:
        provider = self.resolved_ai_provider()
        if provider == "openai":
            return self.openai_embed_model.strip() or None
        if provider == "deepseek":
            return self.deepseek_embed_model.strip() or None
        return None


def _parse_source_templates(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return DEFAULT_SOURCE_TEMPLATES

    templates: list[str] = []
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        if "{wechat_id}" not in candidate:
            continue
        templates.append(candidate)

    if not templates:
        return DEFAULT_SOURCE_TEMPLATES
    return tuple(templates)


def _to_int(raw: str | None, default: int) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value <= 0:
        return default
    return value


def _to_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def get_default_env_file() -> Path:
    custom_path = os.getenv("WECHAT_AGENT_ENV_FILE", "").strip()
    if custom_path:
        return Path(custom_path).expanduser()

    xdg_root = os.getenv("XDG_CONFIG_HOME", "").strip()
    if xdg_root:
        return Path(xdg_root).expanduser() / "wechat-agent" / ".env"
    return Path.home() / ".config" / "wechat-agent" / ".env"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # Prefer a local .env for development; fill missing values from global config.
    load_dotenv(override=False)
    load_dotenv(dotenv_path=get_default_env_file(), override=False)

    default_mode = os.getenv("DEFAULT_VIEW_MODE", "source").strip().lower()
    if default_mode not in {"source", "time", "recommend"}:
        default_mode = "source"

    return Settings(
        db_url=os.getenv("WECHAT_AGENT_DB_URL", "sqlite:///data/wechat_agent.db"),
        ai_provider=os.getenv("AI_PROVIDER", "auto"),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_base_url=os.getenv("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL),
        openai_chat_model=os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
        openai_embed_model=os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small"),
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY") or None,
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL),
        deepseek_chat_model=os.getenv("DEEPSEEK_CHAT_MODEL", "deepseek-chat"),
        deepseek_embed_model=os.getenv("DEEPSEEK_EMBED_MODEL", ""),
        source_templates=_parse_source_templates(os.getenv("SOURCE_TEMPLATES")),
        http_timeout_seconds=_to_int(os.getenv("HTTP_TIMEOUT_SECONDS"), 15),
        max_concurrency=_to_int(os.getenv("MAX_CONCURRENCY"), 5),
        default_view_mode=default_mode,
        wechat2rss_index_url=os.getenv("WECHAT2RSS_INDEX_URL", "https://wechat2rss.xlab.app/list/all/"),
        article_fetch_timeout_seconds=_to_int(os.getenv("ARTICLE_FETCH_TIMEOUT_SECONDS"), 15),
        summary_source_char_limit=_to_int(os.getenv("SUMMARY_SOURCE_CHAR_LIMIT"), 6000),
        midnight_shift_days=_to_int(os.getenv("MIDNIGHT_SHIFT_DAYS"), 2),
        sync_overlap_seconds=_to_int(os.getenv("SYNC_OVERLAP_SECONDS"), 120),
        incremental_sync_enabled=_to_bool(os.getenv("INCREMENTAL_SYNC_ENABLED"), True),
        source_max_candidates=_to_int(os.getenv("SOURCE_MAX_CANDIDATES"), 3),
        source_retry_backoff_ms=_to_int(os.getenv("SOURCE_RETRY_BACKOFF_MS"), 800),
        source_circuit_fail_threshold=_to_int(os.getenv("SOURCE_CIRCUIT_FAIL_THRESHOLD"), 3),
        source_cooldown_minutes=_to_int(os.getenv("SOURCE_COOLDOWN_MINUTES"), 30),
    )
