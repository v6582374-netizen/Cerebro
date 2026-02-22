from __future__ import annotations

import calendar
import html
import hashlib
import re
from datetime import datetime, timezone

import feedparser

from ..schemas import RawArticle


def _to_utc_datetime(value) -> datetime | None:
    if value is None:
        return None
    try:
        timestamp = calendar.timegm(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def _entry_published_at(entry) -> datetime | None:
    candidate = _to_utc_datetime(entry.get("published_parsed"))
    if candidate is not None:
        return candidate

    candidate = _to_utc_datetime(entry.get("updated_parsed"))
    if candidate is not None:
        return candidate

    return None


_TAG_RE = re.compile(r"<[^>]+>")
_MIDNIGHT_TEXT_RE = re.compile(r"(?:^|\s)00:00(?::00)?(?:\s|$)")


def _clean_excerpt(text: str) -> str:
    unescaped = html.unescape(text)
    no_tag = _TAG_RE.sub(" ", unescaped)
    no_space = re.sub(r"\s+", " ", no_tag).strip()
    return no_space


def _entry_excerpt(entry) -> str:
    content_items = entry.get("content") or []
    if content_items and isinstance(content_items, list):
        first = content_items[0]
        if isinstance(first, dict):
            value = first.get("value")
            if isinstance(value, str) and value.strip():
                return _clean_excerpt(value)

    for key in ("summary", "description"):
        candidate = entry.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return _clean_excerpt(candidate)

    return ""


def parse_feed(content: str | bytes, source_url: str, source_name: str | None = None) -> list[RawArticle]:
    parsed = feedparser.parse(content)

    results: list[RawArticle] = []
    for entry in parsed.entries:
        title = str(entry.get("title") or "Untitled").strip() or "Untitled"
        url = str(entry.get("link") or source_url).strip() or source_url
        published_at = _entry_published_at(entry) or datetime.now(timezone.utc)
        published_text = str(entry.get("published") or entry.get("updated") or "")
        is_midnight_publish = bool(_MIDNIGHT_TEXT_RE.search(published_text))
        excerpt = _entry_excerpt(entry)

        external_id = str(entry.get("id") or "").strip()
        if not external_id:
            external_id = f"{url}#{published_at.isoformat()}"

        digest = hashlib.sha256(f"{title}|{url}|{excerpt}".encode("utf-8")).hexdigest()

        results.append(
            RawArticle(
                external_id=external_id,
                title=title,
                url=url,
                published_at=published_at,
                content_excerpt=excerpt,
                raw_hash=digest,
                source_name=source_name,
                is_midnight_publish=is_midnight_publish,
            )
        )

    return results
