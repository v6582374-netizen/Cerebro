from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import html
from html.parser import HTMLParser
import re
import time
from typing import Protocol
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ArticleRef, Subscription
from ..schemas import DiscoveredArticleRef, DiscoveryResult, RawArticle
from ..time_utils import shift_midnight_publish_time
from .session_vault import SessionVault

_TITLE_META_RE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_CT_RE = re.compile(r"\bct\s*=\s*\"?(\d{10})\"?")
_PUBLISH_TIME_RE = re.compile(r'"publish_time"\s*:\s*"([^"]+)"')
_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


class DiscoveryProvider(Protocol):
    name: str

    def search(self, subscription_name: str, target_date: date) -> list[DiscoveredArticleRef]:
        ...


class _ElementTextParser(HTMLParser):
    def __init__(self, *, target_id: str | None = None, target_tag: str | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.target_id = (target_id or "").strip().lower() or None
        self.target_tag = (target_tag or "").strip().lower() or None
        self.capture_depth = 0
        self.done = False
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.done:
            return
        lowered_tag = tag.lower()
        if self.capture_depth > 0:
            self.capture_depth += 1
            return
        attrs_map = {str(k).lower(): (v or "") for k, v in attrs}
        hit_by_id = self.target_id and attrs_map.get("id", "").strip().lower() == self.target_id
        hit_by_tag = self.target_tag and lowered_tag == self.target_tag
        if hit_by_id or hit_by_tag:
            self.capture_depth = 1

    def handle_endtag(self, _tag: str) -> None:
        if self.done or self.capture_depth <= 0:
            return
        self.capture_depth -= 1
        if self.capture_depth == 0:
            self.done = True

    def handle_data(self, data: str) -> None:
        if self.capture_depth > 0 and data.strip():
            self.chunks.append(data.strip())

    def text(self) -> str:
        return " ".join(self.chunks).strip()


class DiscoveryOrchestrator:
    def __init__(
        self,
        providers: list[DiscoveryProvider],
        session_vault: SessionVault,
        session_provider: str = "weread",
        timeout_seconds: int = 15,
        midnight_shift_days: int = 2,
    ) -> None:
        self.providers = providers
        self.session_vault = session_vault
        self.session_provider = session_provider
        self.timeout_seconds = timeout_seconds
        self.midnight_shift_days = midnight_shift_days
        self.http_client = httpx.Client(timeout=timeout_seconds, follow_redirects=True)

    def close(self) -> None:
        self.http_client.close()
        for provider in self.providers:
            close_fn = getattr(provider, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass

    def discover(
        self,
        session: Session,
        sub: Subscription,
        target_date: date,
        since: datetime,
    ) -> DiscoveryResult:
        started = time.perf_counter()
        last_error_kind = "SEARCH_EMPTY"
        last_error_message = "未发现文章链接"
        all_refs: list[DiscoveredArticleRef] = []
        provider_notes: list[str] = []

        for provider in self.providers:
            try:
                refs = self._search_with_provider(provider=provider, sub=sub, target_date=target_date, session=session)
            except Exception as exc:  # noqa: BLE001
                last_error_kind, last_error_message = self._classify_discovery_error(exc)
                provider_notes.append(f"{provider.name}=error({last_error_kind})")
                continue
            filtered = [ref for ref in refs if ref.url]
            provider_notes.append(f"{provider.name}={len(filtered)}")
            if filtered:
                all_refs = filtered
                break

        if not all_refs:
            history_refs = self._history_backtrack_refs(session=session, sub=sub, target_date=target_date)
            if history_refs:
                all_refs = history_refs
                provider_notes.append(f"history_backtrack={len(history_refs)}")
            else:
                provider_notes.append("history_backtrack=0")

        if not all_refs:
            latency_ms = int((time.perf_counter() - started) * 1000)
            note_text = ", ".join(provider_notes)
            error_message = last_error_message if not note_text else f"{last_error_message} ({note_text})"
            return DiscoveryResult(
                ok=False,
                refs=[],
                channel_used=None,
                error_kind=last_error_kind,
                error_message=error_message,
                latency_ms=latency_ms,
                status="FAILED",
            )

        dedup: dict[str, DiscoveredArticleRef] = {}
        for ref in all_refs:
            previous = dedup.get(ref.url)
            if previous is None or ref.confidence > previous.confidence:
                dedup[ref.url] = ref
            self._upsert_ref(session=session, sub=sub, ref=ref)

        latency_ms = int((time.perf_counter() - started) * 1000)
        refs = list(dedup.values())
        refs.sort(key=lambda item: item.confidence, reverse=True)
        return DiscoveryResult(
            ok=True,
            refs=refs,
            channel_used=refs[0].channel if refs else None,
            error_kind=None,
            error_message=None,
            latency_ms=latency_ms,
            status="SUCCESS",
        )

    def materialize_raw_articles(
        self,
        refs: list[DiscoveredArticleRef],
        since: datetime,
    ) -> list[RawArticle]:
        result: list[RawArticle] = []
        safe_since = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        for ref in refs:
            article = self._fetch_article(url=ref.url, title_hint=ref.title_hint)
            if article is None:
                continue
            article.published_at = shift_midnight_publish_time(
                article.published_at,
                is_midnight_publish=article.is_midnight_publish,
                shift_days=self.midnight_shift_days,
            )
            if article.published_at < safe_since:
                continue
            result.append(article)
        return result

    def _search_with_provider(
        self,
        provider: DiscoveryProvider,
        sub: Subscription,
        target_date: date,
        session: Session,
    ) -> list[DiscoveredArticleRef]:
        if provider.name == "weread":
            token = self.session_vault.get(self.session_provider)
            if token is None:
                raise RuntimeError("AUTH_EXPIRED: 登录态缺失")
            search_fn = getattr(provider, "search")
            return search_fn(sub.name, target_date, token)
        if provider.name == "search_index":
            search_fn = getattr(provider, "search")
            extra_keywords: list[str] = []
            wechat_id = (sub.wechat_id or "").strip()
            if wechat_id and not wechat_id.startswith("auto_"):
                extra_keywords.append(wechat_id)
            try:
                return search_fn(sub.name, target_date, extra_keywords=extra_keywords)
            except TypeError:
                return search_fn(sub.name, target_date)
        search_fn = getattr(provider, "search")
        return search_fn(sub.name, target_date)

    def _history_backtrack_refs(self, session: Session, sub: Subscription, target_date: date) -> list[DiscoveredArticleRef]:
        from ..providers.search_index_provider import SearchIndexProvider

        rows = session.execute(
            select(ArticleRef.url)
            .where(ArticleRef.subscription_id == sub.id)
            .order_by(ArticleRef.discovered_at.desc())
            .limit(30)
        ).all()
        biz_values: set[str] = set()
        for (url,) in rows:
            params = parse_qs(urlparse(str(url)).query)
            biz = (params.get("__biz") or [""])[0].strip()
            if biz:
                biz_values.add(biz)
        if not biz_values:
            return []

        provider = SearchIndexProvider(timeout_seconds=self.timeout_seconds, client=self.http_client)
        refs: list[DiscoveredArticleRef] = []
        for biz in sorted(biz_values):
            query = f"site:mp.weixin.qq.com __biz={biz} {target_date.isoformat()}"
            try:
                refs.extend(provider.search_by_query(query=query, limit=3))
            except Exception:
                continue
        for item in refs:
            item.channel = "history_backtrack"
            item.confidence = min(item.confidence, 0.55)
        return refs

    def _fetch_article(self, url: str, title_hint: str | None = None) -> RawArticle | None:
        try:
            response = self.http_client.get(
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
            body = response.text
        except Exception:
            return None

        title = self._extract_title(body, fallback=title_hint or "Untitled")
        published_at, is_midnight = self._extract_publish_time(body)
        excerpt = self._extract_excerpt(body)
        external_id = self._external_id_from_url(url)
        digest = hashlib.sha256(f"{title}|{url}|{excerpt}".encode("utf-8")).hexdigest()
        return RawArticle(
            external_id=external_id,
            title=title,
            url=url,
            published_at=published_at,
            content_excerpt=excerpt,
            raw_hash=digest,
            source_name=None,
            is_midnight_publish=is_midnight,
        )

    def _extract_title(self, html_text: str, fallback: str) -> str:
        match = _TITLE_META_RE.search(html_text)
        if match:
            return html.unescape(match.group(1)).strip() or fallback
        match = _TITLE_RE.search(html_text)
        if match:
            title = html.unescape(match.group(1))
            title = re.sub(r"\s+", " ", title).strip()
            title = title.replace(" - 微信公众号", "").replace("_微信公众平台", "").strip()
            return title or fallback
        return fallback

    def _extract_publish_time(self, html_text: str) -> tuple[datetime, bool]:
        match = _CT_RE.search(html_text)
        if match:
            timestamp = int(match.group(1))
            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            is_midnight = dt.astimezone().strftime("%H:%M:%S") == "00:00:00"
            return dt, is_midnight
        match = _PUBLISH_TIME_RE.search(html_text)
        if match:
            raw = match.group(1).strip()
            try:
                local = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                try:
                    local = datetime.strptime(raw, "%Y-%m-%d %H:%M")
                except ValueError:
                    now = datetime.now(timezone.utc)
                    return now, False
            safe_local = local.replace(tzinfo=datetime.now().astimezone().tzinfo or timezone.utc)
            dt = safe_local.astimezone(timezone.utc)
            is_midnight = safe_local.strftime("%H:%M:%S") == "00:00:00"
            return dt, is_midnight
        now = datetime.now(timezone.utc)
        return now, False

    def _extract_excerpt(self, html_text: str) -> str:
        text = _SCRIPT_STYLE_RE.sub(" ", html_text)
        excerpt = self._extract_element_text(text, target_id="js_content")
        if not excerpt:
            excerpt = self._extract_element_text(text, target_tag="article")
        if not excerpt:
            excerpt = _TAG_RE.sub(" ", text)
        excerpt = html.unescape(excerpt)
        excerpt = re.sub(r"\s+", " ", excerpt).strip()
        return excerpt[:2000]

    def _extract_element_text(self, html_text: str, target_id: str | None = None, target_tag: str | None = None) -> str:
        try:
            parser = _ElementTextParser(target_id=target_id, target_tag=target_tag)
            parser.feed(html_text)
            parser.close()
            return parser.text()
        except Exception:
            return ""

    def _upsert_ref(self, session: Session, sub: Subscription, ref: DiscoveredArticleRef) -> None:
        existing = session.scalar(
            select(ArticleRef).where(
                ArticleRef.subscription_id == sub.id,
                ArticleRef.url == ref.url,
            )
        )
        if existing is None:
            session.add(
                ArticleRef(
                    subscription_id=sub.id,
                    url=ref.url,
                    title_hint=ref.title_hint,
                    published_at_hint=ref.published_at_hint,
                    channel=ref.channel,
                    confidence=float(ref.confidence),
                )
            )
            return
        if ref.title_hint:
            existing.title_hint = ref.title_hint
        if ref.published_at_hint:
            existing.published_at_hint = ref.published_at_hint
        existing.channel = ref.channel
        existing.confidence = max(float(existing.confidence or 0.0), float(ref.confidence))

    def _external_id_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        sn = (params.get("sn") or [""])[0]
        idx = (params.get("idx") or [""])[0]
        mid = (params.get("mid") or [""])[0]
        biz = (params.get("__biz") or [""])[0]
        token = f"{biz}|{mid}|{idx}|{sn}".strip("|")
        if token:
            return token
        return hashlib.sha1(url.encode("utf-8")).hexdigest()

    def _classify_discovery_error(self, exc: Exception) -> tuple[str, str]:
        text = str(exc)
        lowered = text.lower()
        if "auth_expired" in lowered or "登录态" in text:
            return "AUTH_EXPIRED", text
        if "timed out" in lowered or "timeout" in lowered:
            return "TIMEOUT", text
        if "403" in lowered:
            return "FETCH_BLOCKED", text
        if "404" in lowered:
            return "NOT_FOUND", text
        return "SEARCH_EMPTY", text
