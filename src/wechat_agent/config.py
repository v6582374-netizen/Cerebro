from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv


DEFAULT_TEMPLATE = "https://rsshub.app/wechat/mp/{wechat_id}"


@dataclass(frozen=True)
class Settings:
    db_url: str
    openai_api_key: str | None
    openai_base_url: str | None
    openai_chat_model: str
    openai_embed_model: str
    source_templates: tuple[str, ...]
    http_timeout_seconds: int
    max_concurrency: int
    default_view_mode: str
    wechat2rss_index_url: str


def _parse_source_templates(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return (DEFAULT_TEMPLATE,)

    templates: list[str] = []
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        if "{wechat_id}" not in candidate:
            continue
        templates.append(candidate)

    if not templates:
        return (DEFAULT_TEMPLATE,)
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(override=False)

    default_mode = os.getenv("DEFAULT_VIEW_MODE", "source").strip().lower()
    if default_mode not in {"source", "time", "recommend"}:
        default_mode = "source"

    return Settings(
        db_url=os.getenv("WECHAT_AGENT_DB_URL", "sqlite:///data/wechat_agent.db"),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_base_url=os.getenv("OPENAI_BASE_URL") or None,
        openai_chat_model=os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
        openai_embed_model=os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small"),
        source_templates=_parse_source_templates(os.getenv("SOURCE_TEMPLATES")),
        http_timeout_seconds=_to_int(os.getenv("HTTP_TIMEOUT_SECONDS"), 15),
        max_concurrency=_to_int(os.getenv("MAX_CONCURRENCY"), 5),
        default_view_mode=default_mode,
        wechat2rss_index_url=os.getenv("WECHAT2RSS_INDEX_URL", "https://wechat2rss.xlab.app/list/all/"),
    )
