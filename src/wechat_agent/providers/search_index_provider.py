from __future__ import annotations

from datetime import date, datetime, timezone
import html
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import httpx

from ..schemas import DiscoveredArticleRef


def _normalize_mp_link(raw: str) -> str | None:
    if not raw:
        return None
    href = html.unescape(raw).strip()
    if href.startswith("//"):
        href = f"https:{href}"
    if href.startswith("/l/?"):
        params = parse_qs(urlparse(href).query)
        target = params.get("uddg", [])
        href = unquote(target[0]) if target else href
    parsed = urlparse(href)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc.lower() != "mp.weixin.qq.com":
        return None
    if not parsed.path.startswith("/s"):
        return None
    return href


class _DuckResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[tuple[str, str]] = []
        self._in_anchor = False
        self._anchor_href = ""
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_map = {str(k).lower(): (v or "") for k, v in attrs}
        href = attrs_map.get("href", "").strip()
        if not href:
            return
        self._in_anchor = True
        self._anchor_href = href
        self._anchor_text = []

    def handle_data(self, data: str) -> None:
        if self._in_anchor and data.strip():
            self._anchor_text.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._in_anchor:
            return
        title = " ".join(self._anchor_text).strip()
        self.items.append((self._anchor_href, title))
        self._in_anchor = False
        self._anchor_href = ""
        self._anchor_text = []


class SearchIndexProvider:
    name = "search_index"

    def __init__(self, timeout_seconds: int = 15, client: httpx.Client | None = None) -> None:
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=True)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def search(self, subscription_name: str, target_date: date, limit: int = 8) -> list[DiscoveredArticleRef]:
        query = f'site:mp.weixin.qq.com "{subscription_name}" {target_date.isoformat()}'
        params = urlencode({"q": query})
        url = f"https://duckduckgo.com/html/?{params}"
        response = self.client.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2_1) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        response.raise_for_status()
        parser = _DuckResultParser()
        parser.feed(response.text)
        parser.close()

        refs: list[DiscoveredArticleRef] = []
        seen: set[str] = set()
        for rank, (raw_href, title) in enumerate(parser.items, start=1):
            normalized = _normalize_mp_link(raw_href)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            confidence = max(0.2, 1.0 - ((rank - 1) * 0.1))
            refs.append(
                DiscoveredArticleRef(
                    url=normalized,
                    title_hint=title or None,
                    published_at_hint=datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc),
                    channel=self.name,
                    confidence=confidence,
                )
            )
            if len(refs) >= limit:
                break
        return refs

    def search_by_query(self, query: str, limit: int = 6) -> list[DiscoveredArticleRef]:
        params = urlencode({"q": query})
        url = f"https://duckduckgo.com/html/?{params}"
        response = self.client.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2_1) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            },
        )
        response.raise_for_status()
        parser = _DuckResultParser()
        parser.feed(response.text)
        parser.close()

        refs: list[DiscoveredArticleRef] = []
        seen: set[str] = set()
        for rank, (raw_href, title) in enumerate(parser.items, start=1):
            normalized = _normalize_mp_link(raw_href)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            confidence = max(0.2, 1.0 - ((rank - 1) * 0.1))
            refs.append(
                DiscoveredArticleRef(
                    url=normalized,
                    title_hint=title or None,
                    published_at_hint=None,
                    channel=self.name,
                    confidence=confidence,
                )
            )
            if len(refs) >= limit:
                break
        return refs
