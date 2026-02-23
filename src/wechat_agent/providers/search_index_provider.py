from __future__ import annotations

from datetime import date, datetime, timezone
from html.parser import HTMLParser
import re
import time
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from ..schemas import DiscoveredArticleRef

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2_1) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_MP_LINK_RE = re.compile(r"https?://mp\.weixin\.qq\.com/s\?[^\s\"'<>]+", re.IGNORECASE)
_MP_LINK_ESCAPED_RE = re.compile(r"https:\\/\\/mp\\.weixin\\.qq\\.com\\/s\\?[^\s\"'<>]+", re.IGNORECASE)


def _keyword_tokens(text: str) -> list[str]:
    value = (text or "").strip()
    if not value:
        return []
    tokens = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+", value)
    out: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        cleaned = token.strip()
        if len(cleaned) < 2:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(cleaned)
    return out


def _normalize_mp_link(raw: str) -> str | None:
    if not raw:
        return None
    href = raw.strip().rstrip(").,;]")
    href = href.replace("&amp;", "&").replace("&#38;", "&")
    href = href.replace("\\u002F", "/").replace("\\/", "/")
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


def _extract_mp_links_from_text(raw_text: str) -> list[str]:
    if not raw_text:
        return []
    text = raw_text.replace("&amp;", "&").replace("&#38;", "&")
    extracted: list[str] = []
    for match in _MP_LINK_RE.findall(text):
        extracted.append(match)
    for match in _MP_LINK_ESCAPED_RE.findall(text):
        extracted.append(match.replace("\\/", "/"))
    dedup: list[str] = []
    seen: set[str] = set()
    for item in extracted:
        normalized = _normalize_mp_link(item)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        dedup.append(normalized)
    return dedup


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

    def search(
        self,
        subscription_name: str,
        target_date: date,
        extra_keywords: list[str] | None = None,
        limit: int = 8,
    ) -> list[DiscoveredArticleRef]:
        keywords = [subscription_name.strip()]
        for keyword in extra_keywords or []:
            cleaned = (keyword or "").strip()
            if cleaned and cleaned not in keywords:
                keywords.append(cleaned)

        queries: list[str] = []
        primary = keywords[0]
        queries.append(f'site:mp.weixin.qq.com "{primary}"')
        primary_tokens = _keyword_tokens(primary)
        for token in primary_tokens[:2]:
            if token != primary:
                queries.append(f'site:mp.weixin.qq.com "{token}"')
        if len(keywords) > 1:
            queries.append(f'site:mp.weixin.qq.com "{keywords[1]}"')
        queries.append(f'"{primary}" "mp.weixin.qq.com/s?"')
        queries.append(f'site:mp.weixin.qq.com "{primary}" {target_date.isoformat()}')
        dedup_queries: list[str] = []
        seen_queries: set[str] = set()
        for item in queries:
            if item in seen_queries:
                continue
            seen_queries.add(item)
            dedup_queries.append(item)

        refs: list[DiscoveredArticleRef] = []
        seen: set[str] = set()
        for query_index, query in enumerate(dedup_queries[:2]):
            if len(refs) >= limit:
                break
            query_factor = max(0.6, 1.0 - (query_index * 0.08))
            for row in self.search_by_query(query=query, limit=limit, target_date=target_date):
                if row.url in seen:
                    continue
                seen.add(row.url)
                row.confidence = max(0.2, row.confidence * query_factor)
                refs.append(row)
                if len(refs) >= limit:
                    break
        return refs

    def search_by_query(self, query: str, limit: int = 6, target_date: date | None = None) -> list[DiscoveredArticleRef]:
        refs: list[DiscoveredArticleRef] = []
        seen: set[str] = set()

        engines = (
            ("brave", "https://search.brave.com/search", "q", 0.95),
            ("sogou_web", "https://www.sogou.com/web", "query", 0.90),
            ("duckduckgo", "https://duckduckgo.com/html/", "q", 0.80),
            ("bing", "https://www.bing.com/search", "q", 0.70),
        )
        for engine_name, endpoint, query_key, base_conf in engines:
            html_text = self._fetch_engine_html(
                endpoint=endpoint,
                query=query,
                query_key=query_key,
                engine_name=engine_name,
            )
            time.sleep(0.15)
            if not html_text:
                continue

            parser = _DuckResultParser()
            parser.feed(html_text)
            parser.close()

            rank = 0
            for raw_href, title in parser.items:
                normalized = _normalize_mp_link(raw_href)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                confidence = max(0.2, base_conf - (rank * 0.05))
                refs.append(
                    DiscoveredArticleRef(
                        url=normalized,
                        title_hint=title or None,
                        published_at_hint=(
                            datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
                            if target_date is not None
                            else None
                        ),
                        channel=self.name,
                        confidence=confidence,
                    )
                )
                rank += 1
                if len(refs) >= limit:
                    break
            if len(refs) >= limit:
                break

            # Regex fallback: some engines embed links in script/json, not plain anchors.
            for idx, normalized in enumerate(_extract_mp_links_from_text(html_text), start=1):
                if normalized in seen:
                    continue
                seen.add(normalized)
                confidence = max(0.2, base_conf - ((idx + 3) * 0.05))
                refs.append(
                    DiscoveredArticleRef(
                        url=normalized,
                        title_hint=None,
                        published_at_hint=(
                            datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
                            if target_date is not None
                            else None
                        ),
                        channel=self.name,
                        confidence=confidence,
                    )
                )
                if len(refs) >= limit:
                    break
            if len(refs) >= limit:
                break
        return refs

    def _fetch_engine_html(self, endpoint: str, query: str, query_key: str, engine_name: str) -> str:
        try:
            response = self.client.get(
                endpoint,
                params={query_key: query},
                headers={
                    "User-Agent": _UA,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
                    "Referer": (
                        "https://www.sogou.com/"
                        if engine_name == "sogou_web"
                        else ("https://search.brave.com/" if engine_name == "brave" else "https://www.bing.com/")
                    ),
                },
            )
            response.raise_for_status()
            text = response.text
            lowered_url = str(response.url).lower()
            lowered_text = text.lower()
            if "antispider" in lowered_url or "antispider" in lowered_text:
                return ""
            if "too many requests" in lowered_text or "rate limit" in lowered_text:
                return ""
            if "captcha" in lowered_text and "mp.weixin.qq.com" not in lowered_text:
                return ""
            return text
        except Exception:
            return ""

    # Backward-compatible helper for older tests/extensions.
    def extract_links(self, html_text: str) -> list[str]:
        links: list[str] = []
        seen: set[str] = set()
        parser = _DuckResultParser()
        parser.feed(html_text)
        parser.close()
        for raw_href, _title in parser.items:
            normalized = _normalize_mp_link(raw_href)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            links.append(normalized)
        for normalized in _extract_mp_links_from_text(html_text):
            if normalized not in seen:
                seen.add(normalized)
                links.append(normalized)
        return links
