from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import html
import json
import re
import time
from typing import Protocol
from urllib.parse import urljoin

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    FETCH_STATUS_FAILED,
    FETCH_STATUS_SKIPPED,
    FETCH_STATUS_SUCCESS,
    HEALTH_STATE_CLOSED,
    HEALTH_STATE_HALF_OPEN,
    HEALTH_STATE_OPEN,
    FetchAttempt,
    SOURCE_MODE_MANUAL,
    SourceHealth,
    Subscription,
    SubscriptionSource,
    utcnow,
)
from ..providers.template_feed_provider import TemplateFeedProvider
from ..schemas import ProbeResult, RawArticle, SourceCandidate, SourceFetchResult

MANUAL_PROVIDER = "manual"
RSSHUB_MIRROR_PROVIDER = "rsshub_mirror"
WECHAT2RSS_PROVIDER = "wechat2rss_index"
WECHAT2RSS_MIN_SCORE = 6


class SourceProvider(Protocol):
    name: str

    def discover(self, session: Session, sub: Subscription) -> list[SourceCandidate]:
        ...

    def probe(self, candidate: SourceCandidate) -> ProbeResult:
        ...

    def fetch(self, candidate: SourceCandidate, since: datetime) -> list[RawArticle]:
        ...


def _normalize_name(value: str) -> str:
    lowered = value.strip().lower()
    lowered = re.sub(r"\s+", "", lowered)
    lowered = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", lowered)
    return lowered


def _extract_ascii_tokens(value: str) -> list[str]:
    normalized = _normalize_name(value)
    if not normalized:
        return []
    return [token for token in re.findall(r"[a-z0-9]{3,}", normalized)]


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def classify_error(exc: Exception | None, message: str | None = None) -> tuple[str, int | None, str]:
    if exc is not None:
        if isinstance(exc, httpx.TimeoutException):
            return "TIMEOUT", None, str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            if 400 <= code < 500:
                if code in {401, 403}:
                    return "BLOCKED", code, str(exc)
                if code == 404:
                    return "NOT_FOUND", code, str(exc)
                return "HTTP_4XX", code, str(exc)
            if 500 <= code < 600:
                return "HTTP_5XX", code, str(exc)
            return "HTTP_ERROR", code, str(exc)
        if isinstance(exc, httpx.RequestError):
            return "NETWORK", None, str(exc)
        return "UNKNOWN", None, str(exc)

    text = (message or "").strip()
    lowered = text.lower()
    if not text:
        return "UNKNOWN", None, "未知错误"
    if "timeout" in lowered or "timed out" in lowered:
        return "TIMEOUT", None, text
    if "403" in lowered or "forbidden" in lowered:
        return "BLOCKED", 403, text
    if "404" in lowered or "not found" in lowered:
        return "NOT_FOUND", 404, text
    if "5" in lowered and "http" in lowered:
        return "HTTP_5XX", None, text
    if "未解析到文章" in text or "parse" in lowered:
        return "PARSE_EMPTY", None, text
    return "UNKNOWN", None, text


class ManualSourceProvider:
    name = MANUAL_PROVIDER

    def __init__(self, feed_provider: TemplateFeedProvider) -> None:
        self.feed_provider = feed_provider

    def discover(self, session: Session, sub: Subscription) -> list[SourceCandidate]:
        now = utcnow()
        rows = session.scalars(
            select(SubscriptionSource).where(
                SubscriptionSource.subscription_id == sub.id,
                SubscriptionSource.provider == self.name,
                SubscriptionSource.is_active.is_(True),
            )
        ).all()

        candidates: list[SourceCandidate] = []
        for row in rows:
            candidates.append(
                SourceCandidate(
                    subscription_id=sub.id,
                    provider=self.name,
                    url=row.source_url,
                    priority=row.priority,
                    is_pinned=row.is_pinned,
                    confidence=float(row.confidence or 1.0),
                    discovered_at=row.discovered_at,
                    metadata_json=row.metadata_json,
                )
            )

        if sub.source_url and sub.source_mode == SOURCE_MODE_MANUAL:
            if not any(c.url == sub.source_url for c in candidates):
                candidates.append(
                    SourceCandidate(
                        subscription_id=sub.id,
                        provider=self.name,
                        url=sub.source_url,
                        priority=0,
                        is_pinned=True,
                        confidence=1.0,
                        discovered_at=now,
                        metadata_json=json.dumps({"legacy": True}, ensure_ascii=False),
                    )
                )

        return candidates

    def probe(self, candidate: SourceCandidate) -> ProbeResult:
        started = time.perf_counter()
        ok, error = self.feed_provider.probe(candidate.url)
        latency_ms = int((time.perf_counter() - started) * 1000)
        if ok:
            return ProbeResult(ok=True, article_count=1, latency_ms=latency_ms)
        error_kind, _, error_message = classify_error(None, error)
        return ProbeResult(ok=False, article_count=0, latency_ms=latency_ms, error_kind=error_kind, error_message=error_message)

    def fetch(self, candidate: SourceCandidate, since: datetime) -> list[RawArticle]:
        return self.feed_provider.fetch(source_url=candidate.url, since=since)


class TemplateMirrorSourceProvider:
    name = RSSHUB_MIRROR_PROVIDER

    def __init__(self, templates: tuple[str, ...], feed_provider: TemplateFeedProvider) -> None:
        self.templates = templates
        self.feed_provider = feed_provider

    def discover(self, session: Session, sub: Subscription) -> list[SourceCandidate]:
        now = utcnow()
        candidates: list[SourceCandidate] = []
        for idx, template in enumerate(self.templates):
            try:
                url = template.format(wechat_id=sub.wechat_id)
            except KeyError:
                continue
            candidates.append(
                SourceCandidate(
                    subscription_id=sub.id,
                    provider=self.name,
                    url=url,
                    priority=20 + idx,
                    is_pinned=False,
                    confidence=0.55,
                    discovered_at=now,
                    metadata_json=json.dumps({"template": template}, ensure_ascii=False),
                )
            )
        return candidates

    def probe(self, candidate: SourceCandidate) -> ProbeResult:
        started = time.perf_counter()
        ok, error = self.feed_provider.probe(candidate.url)
        latency_ms = int((time.perf_counter() - started) * 1000)
        if ok:
            return ProbeResult(ok=True, article_count=1, latency_ms=latency_ms)
        error_kind, _, error_message = classify_error(None, error)
        return ProbeResult(ok=False, latency_ms=latency_ms, error_kind=error_kind, error_message=error_message)

    def fetch(self, candidate: SourceCandidate, since: datetime) -> list[RawArticle]:
        return self.feed_provider.fetch(source_url=candidate.url, since=since)


@dataclass(frozen=True, slots=True)
class _Wechat2RssItem:
    name: str
    url: str
    normalized_name: str


_ANCHOR_PATTERN = re.compile(
    r'<a href="(?P<url>https://wechat2rss\.xlab\.app/feed/[^"]+\.xml)"[^>]*>(?P<name>.*?)</a>',
    re.IGNORECASE,
)
_VITEPRESS_HASH_MAP_PATTERN = re.compile(r'window\.__VP_HASH_MAP__=JSON\.parse\("(?P<data>.*?)"\);', re.DOTALL)


class Wechat2RssIndexProvider:
    name = WECHAT2RSS_PROVIDER

    def __init__(self, index_url: str | None, feed_provider: TemplateFeedProvider) -> None:
        self.index_url = index_url
        self.feed_provider = feed_provider
        self._cache: list[_Wechat2RssItem] | None = None

    def discover(self, session: Session, sub: Subscription) -> list[SourceCandidate]:
        if not self.index_url:
            return []
        try:
            items = self._load_items()
        except Exception:
            return []
        if not items:
            return []

        normalized_name = _normalize_name(sub.name)
        normalized_id = _normalize_name(sub.wechat_id)
        ascii_tokens = sorted(set(_extract_ascii_tokens(sub.name) + _extract_ascii_tokens(sub.wechat_id)))
        now = utcnow()
        ranked: list[tuple[int, _Wechat2RssItem]] = []
        for item in items:
            if ascii_tokens and not all(token in item.normalized_name for token in ascii_tokens):
                continue
            score = self._candidate_score(
                normalized_name=normalized_name,
                normalized_id=normalized_id,
                item_name=item.normalized_name,
            )
            if score <= 0:
                continue
            ranked.append((score, item))

        ranked.sort(key=lambda row: row[0], reverse=True)
        results: list[SourceCandidate] = []
        for idx, (score, item) in enumerate(ranked[:3]):
            confidence = _clamp(score / 100.0, 0.2, 0.95)
            results.append(
                SourceCandidate(
                    subscription_id=sub.id,
                    provider=self.name,
                    url=item.url,
                    priority=60 + idx,
                    is_pinned=False,
                    confidence=confidence,
                    discovered_at=now,
                    metadata_json=json.dumps({"name": item.name, "score": score}, ensure_ascii=False),
                )
            )
        return results

    def probe(self, candidate: SourceCandidate) -> ProbeResult:
        started = time.perf_counter()
        ok, error = self.feed_provider.probe(candidate.url)
        latency_ms = int((time.perf_counter() - started) * 1000)
        if ok:
            return ProbeResult(ok=True, article_count=1, latency_ms=latency_ms)
        error_kind, _, error_message = classify_error(None, error)
        return ProbeResult(ok=False, latency_ms=latency_ms, error_kind=error_kind, error_message=error_message)

    def fetch(self, candidate: SourceCandidate, since: datetime) -> list[RawArticle]:
        return self.feed_provider.fetch(source_url=candidate.url, since=since)

    def _match_score(self, a: str, b: str) -> int:
        if not a or not b:
            return 0
        if a == b:
            return 100
        if a in b or b in a:
            return min(len(a), len(b))
        return 0

    def _candidate_score(self, normalized_name: str, normalized_id: str, item_name: str) -> int:
        id_score = self._match_score(normalized_id, item_name)
        name_score = self._match_score(normalized_name, item_name)

        # For subscriptions with explicit wechat_id, demand stronger matching to avoid false positives.
        if normalized_id and len(normalized_id) >= 4 and id_score < 4:
            return 0
        # Baseline threshold to avoid low-confidence accidental matches.
        if max(id_score, name_score) < WECHAT2RSS_MIN_SCORE:
            return 0
        return max(id_score, name_score)

    def _load_items(self) -> list[_Wechat2RssItem]:
        if self._cache is not None:
            return self._cache
        response = httpx.get(self.index_url, timeout=20, follow_redirects=True)
        response.raise_for_status()
        items = self._extract_items(response.text)
        if not items:
            for asset_url in self._extract_assets(response.text):
                try:
                    asset_resp = httpx.get(asset_url, timeout=20, follow_redirects=True)
                    asset_resp.raise_for_status()
                except Exception:
                    continue
                items = self._extract_items(asset_resp.text)
                if items:
                    break
        self._cache = items
        return items

    def _extract_items(self, text: str) -> list[_Wechat2RssItem]:
        dedup: dict[str, _Wechat2RssItem] = {}
        for match in _ANCHOR_PATTERN.finditer(text):
            raw_name = html.unescape(match.group("name")).strip()
            url = match.group("url").strip()
            if not raw_name:
                continue
            normalized = _normalize_name(raw_name)
            if not normalized:
                continue
            dedup[url] = _Wechat2RssItem(name=raw_name, url=url, normalized_name=normalized)
        return list(dedup.values())

    def _extract_assets(self, index_html: str) -> list[str]:
        if not self.index_url:
            return []
        match = _VITEPRESS_HASH_MAP_PATTERN.search(index_html)
        if not match:
            return []
        try:
            escaped = match.group("data")
            hash_map = json.loads(escaped.encode("utf-8").decode("unicode_escape"))
        except Exception:
            return []
        hash_value = hash_map.get("list_all.md")
        if not hash_value:
            return []
        return [
            urljoin(self.index_url, f"/assets/list_all.md.{hash_value}.js"),
            urljoin(self.index_url, f"/assets/list_all.md.{hash_value}.lean.js"),
        ]


class SourceRouter:
    def rank(self, sub: Subscription, candidates: list[SourceCandidate], health: dict[tuple[str, str], SourceHealth]) -> list[SourceCandidate]:
        def sort_key(candidate: SourceCandidate) -> tuple[int, float, int, float]:
            h = health.get((candidate.provider, candidate.url))
            score = float(h.score) if h is not None else (candidate.confidence * 100.0)
            preferred_bonus = 1 if sub.preferred_provider and sub.preferred_provider == candidate.provider else 0
            discovered = candidate.discovered_at.timestamp() if candidate.discovered_at else 0.0
            return (
                1 if candidate.is_pinned else 0,
                preferred_bonus * 1000 + score,
                -candidate.priority,
                discovered,
            )

        return sorted(candidates, key=sort_key, reverse=True)

    def pick_best(
        self,
        sub: Subscription,
        candidates: list[SourceCandidate],
        health: dict[tuple[str, str], SourceHealth],
    ) -> SourceCandidate | None:
        ranked = self.rank(sub=sub, candidates=candidates, health=health)
        return ranked[0] if ranked else None


class SourceHealthService:
    def __init__(
        self,
        fail_threshold: int = 3,
        cooldown_minutes: int = 30,
    ) -> None:
        self.fail_threshold = max(fail_threshold, 1)
        self.cooldown_minutes = max(cooldown_minutes, 1)

    def load_health_map(self, session: Session, subscription_id: int) -> dict[tuple[str, str], SourceHealth]:
        rows = session.scalars(
            select(SourceHealth).where(SourceHealth.subscription_id == subscription_id)
        ).all()
        return {(row.provider, row.source_url): row for row in rows}

    def should_skip_for_circuit(self, session: Session, candidate: SourceCandidate, now: datetime | None = None) -> bool:
        reference = now or utcnow()
        health = self._get_health(session, candidate)
        if health is None:
            return False
        if health.state != HEALTH_STATE_OPEN:
            return False
        cooldown_until = _ensure_aware(health.cooldown_until)
        if cooldown_until is not None and cooldown_until > reference:
            return True
        health.state = HEALTH_STATE_HALF_OPEN
        health.updated_at = reference
        session.flush()
        return False

    def record_attempt(
        self,
        session: Session,
        *,
        sync_run_id: int,
        candidate: SourceCandidate,
        status: str,
        latency_ms: int = 0,
        error_kind: str | None = None,
        error_message: str | None = None,
        http_code: int | None = None,
    ) -> None:
        now = utcnow()
        session.add(
            FetchAttempt(
                sync_run_id=sync_run_id,
                subscription_id=candidate.subscription_id,
                provider=candidate.provider,
                source_url=candidate.url,
                status=status,
                http_code=http_code,
                latency_ms=max(int(latency_ms), 0),
                error_kind=error_kind,
                error_message=error_message,
                created_at=now,
            )
        )
        session.flush()

        health = self._get_or_create_health(session, candidate)
        if status == FETCH_STATUS_SUCCESS:
            health.consecutive_failures = 0
            health.state = HEALTH_STATE_CLOSED
            health.cooldown_until = None
            health.last_ok_at = now
            health.last_error = None
        elif status == FETCH_STATUS_FAILED:
            health.consecutive_failures += 1
            health.last_error = error_message
            if health.consecutive_failures >= self.fail_threshold:
                health.state = HEALTH_STATE_OPEN
                health.cooldown_until = now + timedelta(minutes=self.cooldown_minutes)
            elif health.state == HEALTH_STATE_OPEN:
                health.state = HEALTH_STATE_HALF_OPEN
        health.updated_at = now

        self._refresh_metrics(session, health, now=now)
        session.flush()

    def _refresh_metrics(self, session: Session, health: SourceHealth, now: datetime) -> None:
        lower = now - timedelta(hours=24)
        rows = session.execute(
            select(FetchAttempt.status, FetchAttempt.latency_ms)
            .where(
                FetchAttempt.subscription_id == health.subscription_id,
                FetchAttempt.provider == health.provider,
                FetchAttempt.source_url == health.source_url,
                FetchAttempt.created_at >= lower,
            )
        ).all()
        if not rows:
            health.success_rate_24h = 0.0
            health.avg_latency_ms = 0.0
            health.score = float(_clamp(health.score, 0.0, 100.0))
            return

        total = len(rows)
        success = sum(1 for status, _ in rows if status == FETCH_STATUS_SUCCESS)
        latency_values = [float(latency_ms or 0) for _, latency_ms in rows]
        avg_latency = sum(latency_values) / max(len(latency_values), 1)
        success_rate = success / total

        safe_last_ok_at = _ensure_aware(health.last_ok_at)
        if safe_last_ok_at is None:
            freshness = 0.0
        else:
            age_hours = max((now - safe_last_ok_at).total_seconds() / 3600.0, 0.0)
            freshness = _clamp(1.0 - (age_hours / 24.0), 0.0, 1.0)
        latency_norm = _clamp(avg_latency / 5000.0, 0.0, 1.0)
        coverage = _clamp(total / 7.0, 0.0, 1.0)

        score = 100.0 * (
            0.45 * success_rate
            + 0.25 * (1.0 - latency_norm)
            + 0.20 * freshness
            + 0.10 * coverage
        )
        health.success_rate_24h = float(success_rate)
        health.avg_latency_ms = float(avg_latency)
        health.score = float(_clamp(score, 0.0, 100.0))

    def _get_health(self, session: Session, candidate: SourceCandidate) -> SourceHealth | None:
        return session.scalar(
            select(SourceHealth).where(
                SourceHealth.subscription_id == candidate.subscription_id,
                SourceHealth.provider == candidate.provider,
                SourceHealth.source_url == candidate.url,
            )
        )

    def _get_or_create_health(self, session: Session, candidate: SourceCandidate) -> SourceHealth:
        existing = self._get_health(session, candidate)
        if existing is not None:
            return existing
        row = SourceHealth(
            subscription_id=candidate.subscription_id,
            provider=candidate.provider,
            source_url=candidate.url,
            state=HEALTH_STATE_CLOSED,
            score=float(candidate.confidence * 100.0),
            success_rate_24h=0.0,
            avg_latency_ms=0.0,
            consecutive_failures=0,
        )
        session.add(row)
        session.flush()
        return row


class SourceGateway:
    def __init__(
        self,
        providers: list[SourceProvider],
        router: SourceRouter,
        health_service: SourceHealthService,
        max_candidates: int = 3,
        retry_backoff_ms: int = 800,
    ) -> None:
        self.providers = {provider.name: provider for provider in providers}
        self.router = router
        self.health_service = health_service
        self.max_candidates = max(max_candidates, 1)
        self.retry_backoff_ms = max(retry_backoff_ms, 0)

    def discover_candidates(self, session: Session, sub: Subscription) -> list[SourceCandidate]:
        self._demote_legacy_manual_sources(session=session, sub=sub)
        self._deactivate_weak_wechat2rss_sources(session=session, sub=sub)
        session.flush()
        now = utcnow()
        dedup: dict[tuple[str, str], SourceCandidate] = {}
        for provider in self.providers.values():
            for candidate in provider.discover(session=session, sub=sub):
                key = (candidate.provider, candidate.url)
                previous = dedup.get(key)
                if previous is None or candidate.priority < previous.priority:
                    dedup[key] = candidate
                self._upsert_subscription_source(session=session, candidate=candidate, now=now)

        stored_rows = session.scalars(
            select(SubscriptionSource).where(
                SubscriptionSource.subscription_id == sub.id,
                SubscriptionSource.is_active.is_(True),
            )
        ).all()
        for row in stored_rows:
            key = (row.provider, row.source_url)
            if key in dedup:
                continue
            dedup[key] = SourceCandidate(
                subscription_id=sub.id,
                provider=row.provider,
                url=row.source_url,
                priority=row.priority,
                is_pinned=row.is_pinned,
                confidence=float(row.confidence or 0.0),
                discovered_at=row.discovered_at,
                metadata_json=row.metadata_json,
            )

        health = self.health_service.load_health_map(session=session, subscription_id=sub.id)
        ranked = self.router.rank(sub=sub, candidates=list(dedup.values()), health=health)
        return ranked

    def fetch_with_failover(
        self,
        session: Session,
        *,
        sync_run_id: int,
        sub: Subscription,
        since: datetime,
    ) -> SourceFetchResult:
        candidates = self.discover_candidates(session=session, sub=sub)
        if not candidates:
            placeholder = SourceCandidate(
                subscription_id=sub.id,
                provider="none",
                url="",
                priority=999,
                confidence=0.0,
                discovered_at=utcnow(),
            )
            return SourceFetchResult(
                ok=False,
                candidate=placeholder,
                articles=[],
                latency_ms=0,
                error_kind="NOT_FOUND",
                error_message="未发现可用候选源",
            )

        attempts = 0
        last_error_kind = "UNKNOWN"
        last_error_message = "未知错误"
        for candidate in candidates:
            if attempts >= self.max_candidates:
                break
            attempts += 1
            provider = self.providers.get(candidate.provider)
            if provider is None:
                continue

            if self.health_service.should_skip_for_circuit(session=session, candidate=candidate):
                self.health_service.record_attempt(
                    session=session,
                    sync_run_id=sync_run_id,
                    candidate=candidate,
                    status=FETCH_STATUS_SKIPPED,
                    latency_ms=0,
                    error_kind="CIRCUIT_OPEN",
                    error_message="源处于熔断冷却期",
                )
                continue

            probe = provider.probe(candidate)
            if not probe.ok:
                last_error_kind = probe.error_kind or "UNKNOWN"
                last_error_message = probe.error_message or "源探测失败"
                self.health_service.record_attempt(
                    session=session,
                    sync_run_id=sync_run_id,
                    candidate=candidate,
                    status=FETCH_STATUS_FAILED,
                    latency_ms=probe.latency_ms,
                    error_kind=last_error_kind,
                    error_message=last_error_message,
                )
                continue

            fetch_result = self._fetch_with_retry(provider=provider, candidate=candidate, since=since)
            if fetch_result.ok:
                self.health_service.record_attempt(
                    session=session,
                    sync_run_id=sync_run_id,
                    candidate=candidate,
                    status=FETCH_STATUS_SUCCESS,
                    latency_ms=fetch_result.latency_ms,
                )
                return fetch_result

            last_error_kind = fetch_result.error_kind or "UNKNOWN"
            last_error_message = fetch_result.error_message or "抓取失败"
            self.health_service.record_attempt(
                session=session,
                sync_run_id=sync_run_id,
                candidate=candidate,
                status=FETCH_STATUS_FAILED,
                latency_ms=fetch_result.latency_ms,
                error_kind=last_error_kind,
                error_message=last_error_message,
            )

        return SourceFetchResult(
            ok=False,
            candidate=candidates[0],
            articles=[],
            latency_ms=0,
            error_kind=last_error_kind,
            error_message=last_error_message,
        )

    def _fetch_with_retry(self, provider: SourceProvider, candidate: SourceCandidate, since: datetime) -> SourceFetchResult:
        started = time.perf_counter()
        for attempt in range(2):
            try:
                articles = provider.fetch(candidate=candidate, since=since)
                latency_ms = int((time.perf_counter() - started) * 1000)
                return SourceFetchResult(
                    ok=True,
                    candidate=candidate,
                    articles=articles,
                    latency_ms=latency_ms,
                )
            except Exception as exc:  # noqa: BLE001
                error_kind, _, error_message = classify_error(exc)
                should_retry = error_kind in {"TIMEOUT", "HTTP_5XX"} and attempt == 0
                if should_retry and self.retry_backoff_ms > 0:
                    time.sleep(self.retry_backoff_ms / 1000.0)
                    continue
                latency_ms = int((time.perf_counter() - started) * 1000)
                return SourceFetchResult(
                    ok=False,
                    candidate=candidate,
                    articles=[],
                    latency_ms=latency_ms,
                    error_kind=error_kind,
                    error_message=error_message,
                )
        latency_ms = int((time.perf_counter() - started) * 1000)
        return SourceFetchResult(
            ok=False,
            candidate=candidate,
            articles=[],
            latency_ms=latency_ms,
            error_kind="UNKNOWN",
            error_message="抓取失败",
        )

    def _upsert_subscription_source(self, session: Session, candidate: SourceCandidate, now: datetime) -> None:
        existing = session.scalar(
            select(SubscriptionSource).where(
                SubscriptionSource.subscription_id == candidate.subscription_id,
                SubscriptionSource.provider == candidate.provider,
                SubscriptionSource.source_url == candidate.url,
            )
        )
        if existing is None:
            session.add(
                SubscriptionSource(
                    subscription_id=candidate.subscription_id,
                    provider=candidate.provider,
                    source_url=candidate.url,
                    priority=candidate.priority,
                    is_pinned=candidate.is_pinned,
                    is_active=True,
                    confidence=float(candidate.confidence),
                    discovered_at=candidate.discovered_at or now,
                    metadata_json=candidate.metadata_json,
                )
            )
            session.flush()
            return

        existing.priority = candidate.priority
        existing.is_active = True
        existing.confidence = float(candidate.confidence)
        if candidate.is_pinned:
            existing.is_pinned = True
        if candidate.metadata_json:
            existing.metadata_json = candidate.metadata_json
        if candidate.discovered_at is not None:
            existing.discovered_at = candidate.discovered_at

    def _demote_legacy_manual_sources(self, session: Session, sub: Subscription) -> None:
        rows = session.scalars(
            select(SubscriptionSource).where(
                SubscriptionSource.subscription_id == sub.id,
                SubscriptionSource.provider == MANUAL_PROVIDER,
            )
        ).all()
        for row in rows:
            metadata = str(row.metadata_json or "")
            if '"legacy":true' not in metadata:
                continue
            row.is_pinned = False
            row.is_active = False
            row.priority = max(int(row.priority or 0), 95)

    def _deactivate_weak_wechat2rss_sources(self, session: Session, sub: Subscription) -> None:
        rows = session.scalars(
            select(SubscriptionSource).where(
                SubscriptionSource.subscription_id == sub.id,
                SubscriptionSource.provider == WECHAT2RSS_PROVIDER,
                SubscriptionSource.is_active.is_(True),
            )
        ).all()
        for row in rows:
            score = 0
            try:
                metadata = json.loads(row.metadata_json or "{}")
                score = int(metadata.get("score") or 0)
            except Exception:
                score = 0
            if score < WECHAT2RSS_MIN_SCORE:
                row.is_active = False


def stale_hours(last_ok_at: datetime | None, now: datetime | None = None) -> int | None:
    safe_last = _ensure_aware(last_ok_at)
    if safe_last is None:
        return None
    reference = now or datetime.now(timezone.utc)
    return max(int((reference - safe_last).total_seconds() // 3600), 0)
