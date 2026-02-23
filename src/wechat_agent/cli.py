from __future__ import annotations

from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
import sys
import webbrowser

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import and_, case, delete, func, select

from .config import (
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_OPENAI_BASE_URL,
    get_default_env_file,
    get_settings,
)
from .db import init_db, session_scope
from .models import (
    Article,
    ArticleRef,
    ArticleSummary,
    AuthSessionEntry,
    DISCOVERY_STATUS_DELAYED,
    DISCOVERY_STATUS_FAILED,
    DISCOVERY_STATUS_SUCCESS,
    DiscoveryRun,
    FETCH_STATUS_FAILED,
    FetchAttempt,
    ReadState,
    RecommendationScoreEntry,
    SOURCE_STATUS_ACTIVE,
    SOURCE_STATUS_PENDING,
    Subscription,
    SYNC_ITEM_STATUS_FAILED,
    SYNC_ITEM_STATUS_SUCCESS,
    SyncRun,
    SyncRunItem,
    utcnow,
)
from .providers.search_index_provider import SearchIndexProvider
from .providers.template_feed_provider import TemplateFeedProvider
from .providers.weread_discovery_provider import WeReadDiscoveryProvider
from .schemas import ArticleViewItem, DiscoveredArticleRef
from .services.coverage_service import CoverageService
from .services.discovery_orchestrator import DiscoveryOrchestrator
from .services.fetcher import Fetcher
from .services.read_state import ReadStateService
from .services.recommender import Recommender
from .services.session_vault import SessionVault
from .services.source_gateway import (
    ManualSourceProvider,
    SourceGateway,
    SourceHealthService,
    SourceRouter,
    TemplateMirrorSourceProvider,
    Wechat2RssIndexProvider,
    stale_hours,
)
from .services.source_resolver import SourceResolver
from .services.summarizer import Summarizer
from .services.sync_service import SyncService
from .time_utils import local_day_bounds_utc
from .views.table_renderer import render_article_items

app = typer.Typer(help="微信公众号文章 CLI 聚合推荐系统", no_args_is_help=True)
sub_app = typer.Typer(help="订阅管理")
read_app = typer.Typer(help="阅读状态管理")
config_app = typer.Typer(help="配置管理")
app.add_typer(sub_app, name="sub")
app.add_typer(read_app, name="read")
app.add_typer(config_app, name="config")


class ViewMode(str, Enum):
    source = "source"
    time = "time"
    recommend = "recommend"


class ReadStateValue(str, Enum):
    read = "read"
    unread = "unread"


def _parse_date(raw: str | None) -> date:
    if not raw:
        return datetime.now().date()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise typer.BadParameter("日期格式必须是 YYYY-MM-DD") from exc


def _day_bounds(target_date: date) -> tuple[datetime, datetime]:
    return local_day_bounds_utc(target_date)


def _build_runtime():
    settings = get_settings()
    provider = TemplateFeedProvider(
        timeout_seconds=settings.http_timeout_seconds,
        midnight_shift_days=settings.midnight_shift_days,
    )
    resolver = SourceResolver(
        templates=settings.source_templates,
        provider=provider,
        wechat2rss_index_url=settings.wechat2rss_index_url,
    )
    api_key = settings.resolved_api_key()
    base_url = settings.resolved_base_url()
    chat_model = settings.resolved_chat_model()
    embed_model = settings.resolved_embed_model()

    fetcher = Fetcher(provider=provider)
    summarizer = Summarizer(
        api_key=api_key,
        base_url=base_url,
        chat_model=chat_model,
        fetch_timeout_seconds=settings.article_fetch_timeout_seconds,
        source_char_limit=settings.summary_source_char_limit,
    )
    recommender = Recommender(
        api_key=api_key,
        base_url=base_url,
        embed_model=embed_model,
    )
    discovery_orchestrator: DiscoveryOrchestrator | None = None
    source_gateway: SourceGateway | None = None
    closers: list[object] = [provider]
    if settings.discovery_v2_enabled:
        session_vault = SessionVault(backend=settings.session_backend)
        discovery_orchestrator = DiscoveryOrchestrator(
            providers=[
                WeReadDiscoveryProvider(timeout_seconds=settings.http_timeout_seconds),
                SearchIndexProvider(timeout_seconds=settings.http_timeout_seconds),
            ],
            session_vault=session_vault,
            session_provider=settings.session_provider,
            timeout_seconds=settings.http_timeout_seconds,
            midnight_shift_days=settings.midnight_shift_days,
        )
        closers.append(discovery_orchestrator)
    else:
        source_gateway = SourceGateway(
            providers=[
                ManualSourceProvider(feed_provider=provider),
                TemplateMirrorSourceProvider(templates=settings.source_templates, feed_provider=provider),
                Wechat2RssIndexProvider(index_url=settings.wechat2rss_index_url, feed_provider=provider),
            ],
            router=SourceRouter(),
            health_service=SourceHealthService(
                fail_threshold=settings.source_circuit_fail_threshold,
                cooldown_minutes=settings.source_cooldown_minutes,
            ),
            max_candidates=settings.source_max_candidates,
            retry_backoff_ms=settings.source_retry_backoff_ms,
        )
    sync_service = SyncService(
        resolver=resolver,
        fetcher=fetcher,
        summarizer=summarizer,
        recommender=recommender,
        sync_overlap_seconds=settings.sync_overlap_seconds,
        incremental_sync_enabled=settings.incremental_sync_enabled,
        source_gateway=source_gateway,
        discovery_orchestrator=discovery_orchestrator,
    )
    return closers, sync_service


def _ai_footer(settings) -> str:
    provider = settings.resolved_ai_provider()
    if provider in {"openai", "deepseek"}:
        summary_model = settings.resolved_chat_model()
        embed_model = settings.resolved_embed_model() or "local-hash"
        return f"AI: provider={provider} | summary={summary_model} | embedding={embed_model}"
    else:
        return "AI: provider=none | summary=fallback(no_api_key) | embedding=local-hash(no_api_key)"


def _echo_ai_footer(settings) -> None:
    typer.echo(_ai_footer(settings))


def _round_robin_by_source(items: list[ArticleViewItem]) -> list[ArticleViewItem]:
    buckets: dict[str, deque[ArticleViewItem]] = defaultdict(deque)
    for item in items:
        buckets[item.source_name].append(item)

    result: list[ArticleViewItem] = []
    source_names = sorted(buckets.keys())
    while True:
        progress = False
        for source_name in source_names:
            bucket = buckets[source_name]
            if not bucket:
                continue
            result.append(bucket.popleft())
            progress = True
        if not progress:
            break
    return result


def _query_article_items(
    session,
    target_date: date,
    mode: str,
    allowed_sources: set[str] | None = None,
) -> list[ArticleViewItem]:
    day_start, day_end = _day_bounds(target_date)
    day_id_by_article_pk, _ = _build_day_id_maps(session=session, target_date=target_date)

    stmt = (
        select(
            Article.id,
            Subscription.name,
            Article.published_at,
            Article.title,
            Article.url,
            ArticleSummary.summary_text,
            ReadState.is_read,
            RecommendationScoreEntry.score,
        )
        .join(Subscription, Subscription.id == Article.subscription_id)
        .outerjoin(ArticleSummary, ArticleSummary.article_id == Article.id)
        .outerjoin(ReadState, ReadState.article_id == Article.id)
        .outerjoin(RecommendationScoreEntry, RecommendationScoreEntry.article_id == Article.id)
        .where(and_(Article.published_at >= day_start, Article.published_at < day_end))
    )

    if mode == "source":
        stmt = stmt.order_by(Subscription.name.asc(), Article.published_at.desc())
    elif mode == "time":
        stmt = stmt.order_by(Article.published_at.desc())
    else:
        read_rank = case((ReadState.is_read.is_(True), 1), else_=0)
        stmt = stmt.order_by(read_rank.asc(), RecommendationScoreEntry.score.desc().nullslast(), Article.published_at.desc())

    tag_re = re.compile(r"<[^>]+>")

    def truncate_summary(text: str, limit: int = 50) -> str:
        if len(text) <= limit:
            return text
        for sep in ("。", "！", "？", ".", "!", "?", "；", ";", "，", ",", "、"):
            idx = text.rfind(sep, 0, limit + 1)
            if idx >= int(limit * 0.6):
                return text[: idx + 1].strip()
        clipped = text[: max(limit - 1, 1)].rstrip("，,、；;：:")
        return f"{clipped}…"

    def normalize_with_title(title: str) -> str:
        cleaned = tag_re.sub(" ", title or "")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            cleaned = "正文抓取失败，建议打开原文查看。"
        return truncate_summary(cleaned, 50)

    def clean_summary(raw: str | None, title: str) -> str:
        if not raw:
            return normalize_with_title(title)
        no_tag = tag_re.sub(" ", raw)
        compact = re.sub(r"\s+", " ", no_tag).strip()
        compact = re.sub(r'^[\"\'“”‘’]+|[\"\'“”‘’]+$', "", compact).strip()
        compact = re.sub(r"^(摘要|总结|内容摘要|摘要如下)\s*[:：]\s*", "", compact).strip()
        if not compact:
            return normalize_with_title(title)
        return truncate_summary(compact, 50)

    rows = session.execute(stmt).all()
    items = [
        ArticleViewItem(
            day_id=day_id_by_article_pk.get(int(row[0]), 0),
            article_pk=row[0],
            source_name=row[1],
            published_at=row[2],
            title=row[3],
            url=row[4] or "-",
            summary=clean_summary(row[5], row[3]),
            is_read=bool(row[6]) if row[6] is not None else False,
            score=float(row[7]) if row[7] is not None else None,
        )
        for row in rows
    ]

    if allowed_sources is not None:
        items = [item for item in items if item.source_name in allowed_sources]

    if mode == "recommend":
        read_count = session.scalar(select(func.count()).select_from(ReadState).where(ReadState.is_read.is_(True))) or 0
        if read_count == 0:
            items = sorted(items, key=lambda x: x.published_at, reverse=True)
            items = _round_robin_by_source(items)

    return items


def _all_subscription_names(session) -> list[str]:
    rows = session.execute(select(Subscription.name).order_by(Subscription.name.asc())).all()
    return [str(name) for (name,) in rows]


def _sync_run_new_stats(session, run_id: int) -> tuple[int, list[str]]:
    rows = session.execute(
        select(Subscription.name, SyncRunItem.status, SyncRunItem.new_count)
        .join(Subscription, Subscription.id == SyncRunItem.subscription_id)
        .where(SyncRunItem.sync_run_id == run_id)
        .order_by(Subscription.name.asc())
    ).all()
    new_total = 0
    no_new_sources: list[str] = []
    for source_name, status, new_count in rows:
        count = int(new_count or 0)
        new_total += count
        if status == SYNC_ITEM_STATUS_SUCCESS and count == 0:
            no_new_sources.append(str(source_name))
    return new_total, no_new_sources


def _source_last_ok_by_subscription(session) -> dict[int, datetime]:
    rows = session.execute(
        select(Article.subscription_id, func.max(Article.published_at)).group_by(Article.subscription_id)
    ).all()
    return {int(sub_id): last_ok for sub_id, last_ok in rows if last_ok is not None}


def _sync_run_live_metrics(
    session,
    *,
    run_id: int,
    target_date: date,
    strict_live: bool = False,
) -> tuple[int, int, int, dict[str, str]]:
    discovery_rows = session.execute(
        select(Subscription.id, Subscription.name, DiscoveryRun.status)
        .join(Subscription, Subscription.id == DiscoveryRun.subscription_id)
        .where(DiscoveryRun.sync_run_id == run_id)
        .order_by(Subscription.name.asc())
    ).all()
    if discovery_rows:
        last_ok_by_sub = _source_last_ok_by_subscription(session)
        discover_ok = 0
        discover_failed = 0
        discover_delayed = 0
        source_status_lines: dict[str, str] = {}
        for sub_id, source_name, status in discovery_rows:
            if status == DISCOVERY_STATUS_SUCCESS:
                discover_ok += 1
                source_status_lines[str(source_name)] = "实时成功"
                continue
            if status == DISCOVERY_STATUS_DELAYED and not strict_live:
                discover_delayed += 1
                lag_hours = stale_hours(last_ok_by_sub.get(int(sub_id)), now=utcnow())
                if lag_hours is None:
                    source_status_lines[str(source_name)] = "使用缓存(延迟未知)"
                else:
                    source_status_lines[str(source_name)] = f"使用缓存(延迟{lag_hours}小时)"
                continue
            discover_failed += 1
            source_status_lines[str(source_name)] = "完全失败(待修复)"
        return discover_ok, discover_failed, discover_delayed, source_status_lines

    rows = session.execute(
        select(Subscription.id, Subscription.name, SyncRunItem.status)
        .join(Subscription, Subscription.id == SyncRunItem.subscription_id)
        .where(SyncRunItem.sync_run_id == run_id)
        .order_by(Subscription.name.asc())
    ).all()
    day_start, day_end = _day_bounds(target_date)
    last_ok_by_sub = _source_last_ok_by_subscription(session)

    live_ok = 0
    live_failed = 0
    stale_used = 0
    source_status_lines: dict[str, str] = {}
    for sub_id, source_name, status in rows:
        if status == SYNC_ITEM_STATUS_SUCCESS:
            live_ok += 1
            source_status_lines[str(source_name)] = "实时成功"
            continue

        live_failed += 1
        has_cached = bool(
            session.scalar(
                select(func.count())
                .select_from(Article)
                .where(
                    Article.subscription_id == sub_id,
                    Article.published_at >= day_start,
                    Article.published_at < day_end,
                )
            )
            or 0
        )
        if has_cached and not strict_live:
            stale_used += 1
            lag_hours = stale_hours(last_ok_by_sub.get(int(sub_id)), now=utcnow())
            if lag_hours is None:
                source_status_lines[str(source_name)] = "使用缓存(延迟未知)"
            else:
                source_status_lines[str(source_name)] = f"使用缓存(延迟{lag_hours}小时)"
        else:
            source_status_lines[str(source_name)] = "完全失败(待修复)"
    return live_ok, live_failed, stale_used, source_status_lines


def _live_success_source_names(session, run_id: int) -> set[str]:
    discovery_rows = session.execute(
        select(Subscription.name)
        .join(DiscoveryRun, DiscoveryRun.subscription_id == Subscription.id)
        .where(
            DiscoveryRun.sync_run_id == run_id,
            DiscoveryRun.status == DISCOVERY_STATUS_SUCCESS,
        )
    ).all()
    if discovery_rows:
        return {str(name) for (name,) in discovery_rows}

    rows = session.execute(
        select(Subscription.name)
        .join(SyncRunItem, SyncRunItem.subscription_id == Subscription.id)
        .where(
            SyncRunItem.sync_run_id == run_id,
            SyncRunItem.status == SYNC_ITEM_STATUS_SUCCESS,
        )
    ).all()
    return {str(name) for (name,) in rows}


def _build_day_id_maps(session, target_date: date) -> tuple[dict[int, int], dict[int, int]]:
    day_start, day_end = _day_bounds(target_date)
    rows = session.execute(
        select(Article.id)
        .where(and_(Article.published_at >= day_start, Article.published_at < day_end))
        .order_by(Article.published_at.desc(), Article.id.asc())
    ).all()
    by_article_pk: dict[int, int] = {}
    by_day_id: dict[int, int] = {}
    for idx, (article_id,) in enumerate(rows, start=1):
        article_pk = int(article_id)
        by_article_pk[article_pk] = idx
        by_day_id[idx] = article_pk
    return by_article_pk, by_day_id


def _resolve_article_pk_by_day_id(session, target_date: date, day_id: int) -> int | None:
    if day_id <= 0:
        return None
    _, by_day_id = _build_day_id_maps(session=session, target_date=target_date)
    return by_day_id.get(day_id)


def _resolve_article_pks_by_day_ids(
    session,
    target_date: date,
    day_ids: list[int],
) -> dict[int, int]:
    _, by_day_id = _build_day_id_maps(session=session, target_date=target_date)
    resolved: dict[int, int] = {}
    for day_id in day_ids:
        article_pk = by_day_id.get(day_id)
        if article_pk is not None:
            resolved[day_id] = article_pk
    return resolved


def _render_subscription_table(subscriptions: list[Subscription]) -> str:
    console = Console(record=True, force_terminal=False, color_system=None, width=140)
    table = Table(show_header=True, header_style="bold")
    table.add_column("公众号")
    table.add_column("订阅ID")
    table.add_column("发现状态")
    table.add_column("同步状态")
    table.add_column("错误信息")

    for sub in subscriptions:
        table.add_row(
            sub.name,
            sub.wechat_id,
            sub.discovery_status or SOURCE_STATUS_PENDING,
            sub.source_status,
            sub.last_error or "-",
        )

    with console.capture() as capture:
        console.print(table)
    return capture.get()


def _parse_id_list(raw_ids: str) -> list[int]:
    result: list[int] = []
    for part in raw_ids.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        if not candidate.isdigit():
            raise ValueError(f"非法文章ID: {candidate}")
        result.append(int(candidate))
    if not result:
        raise ValueError("缺少文章ID")
    return result


_ENV_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _resolve_env_path(custom_path: str | None) -> Path:
    if custom_path:
        return Path(custom_path).expanduser()
    return get_default_env_file()


def _read_env_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_LINE_RE.match(line)
        if not match:
            continue
        key = match.group(1)
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def _serialize_env_value(value: str) -> str:
    if value == "":
        return ""
    if any(ch.isspace() for ch in value) or any(ch in value for ch in ['"', "'", "#"]):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _upsert_env_values(path: Path, updates: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(updates)
    out_lines: list[str] = []

    for line in raw_lines:
        match = _ENV_LINE_RE.match(line)
        if not match:
            out_lines.append(line)
            continue
        key = match.group(1)
        if key in pending:
            out_lines.append(f"{key}={_serialize_env_value(pending.pop(key))}")
        else:
            out_lines.append(line)

    if not raw_lines:
        out_lines.append("# WeChat Agent configuration")

    if pending:
        if out_lines and out_lines[-1].strip() != "":
            out_lines.append("")
        for key, value in pending.items():
            out_lines.append(f"{key}={_serialize_env_value(value)}")

    path.write_text("\n".join(out_lines).rstrip() + "\n", encoding="utf-8")


def _mask_secret(value: str | None) -> str:
    if not value:
        return "(未配置)"
    if len(value) <= 8:
        return "********"
    return f"{value[:4]}...{value[-4:]}"


def _candidate_label(ref: DiscoveredArticleRef) -> str:
    title = (ref.title_hint or "（无标题）").strip()
    title = re.sub(r"\s+", " ", title)
    if len(title) > 48:
        title = f"{title[:48]}..."
    return f"[{ref.channel}|{ref.confidence:.2f}] {title}"


def _select_discovery_candidate(refs: list[DiscoveredArticleRef]) -> DiscoveredArticleRef | None:
    if not refs:
        return None
    top = refs[:5]
    if len(top) == 1:
        return top[0]
    if top[0].confidence - top[1].confidence >= 0.2:
        return top[0]

    typer.echo("发现多个可能匹配的候选文章，请选择最接近该公众号的一项：")
    for idx, ref in enumerate(top, start=1):
        typer.echo(f"{idx}. {_candidate_label(ref)}")
        typer.echo(f"   {ref.url}")

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        typer.echo("当前非交互终端，默认选择候选 1。")
        return top[0]

    raw = typer.prompt("输入候选编号(1-5，0为暂不绑定)", default="1").strip()
    try:
        picked = int(raw)
    except ValueError:
        picked = 1
    if picked <= 0 or picked > len(top):
        return None
    return top[picked - 1]


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _upsert_auth_session(
    session,
    *,
    provider: str,
    secret: str,
    expires_at: datetime | None,
) -> None:
    existing = session.get(AuthSessionEntry, provider)
    digest = _hash_secret(secret)
    if existing is None:
        session.add(
            AuthSessionEntry(
                provider=provider,
                encrypted_blob=digest,
                expires_at=expires_at,
            )
        )
        return
    existing.encrypted_blob = digest
    existing.expires_at = expires_at


def _session_state(session, settings) -> str:
    provider = settings.session_provider
    row = session.get(AuthSessionEntry, provider)
    if row is None:
        return "missing"
    now = utcnow()
    if row.expires_at is not None:
        expires_at = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=now.tzinfo)
        if expires_at < now:
            return "expired"
    vault = SessionVault(backend=settings.session_backend)
    secret = vault.get(provider)
    if not secret:
        return "missing"
    return "valid"


def _bulk_mark_read(ids_raw: str, is_read: bool, target_date: date) -> None:
    settings = get_settings()
    init_db(settings)
    service = ReadStateService()

    try:
        ids = _parse_id_list(ids_raw)
    except ValueError as exc:
        typer.echo(str(exc))
        _echo_ai_footer(settings)
        return

    changed = 0
    with session_scope(settings) as session:
        resolved_map = _resolve_article_pks_by_day_ids(
            session=session,
            target_date=target_date,
            day_ids=ids,
        )
        for day_id in ids:
            article_pk = resolved_map.get(day_id)
            if article_pk is None:
                typer.echo(f"文章不存在: day_id={day_id}, date={target_date.isoformat()}")
                continue
            article = session.get(Article, article_pk)
            if article is None:
                typer.echo(f"文章不存在: day_id={day_id}, date={target_date.isoformat()}")
                continue
            service.mark(session=session, article_id=article_pk, is_read=is_read)
            changed += 1
        if changed > 0:
            session.commit()

    typer.echo(f"已批量更新 {changed} 篇文章状态为: {'read' if is_read else 'unread'}")
    _echo_ai_footer(settings)


def _interactive_read_loop(
    session,
    target_date: date,
    mode_value: str,
    source_names: list[str] | None = None,
    source_status_lines: dict[str, str] | None = None,
) -> None:
    service = ReadStateService()

    typer.echo("进入交互已读模式: r/u/t <ids> | o <id> 打开原文 | p 重绘 | q 退出")
    while True:
        try:
            raw = typer.prompt("read>").strip()
        except (EOFError, typer.Abort):
            typer.echo("退出交互已读模式。")
            return

        if not raw:
            continue
        if raw.lower() in {"q", "quit", "exit"}:
            typer.echo("退出交互已读模式。")
            return
        if raw.lower() in {"p", "print"}:
            items = _query_article_items(session=session, target_date=target_date, mode=mode_value)
            typer.echo(
                render_article_items(
                    items=items,
                    mode=mode_value,
                    source_names=source_names,
                    source_status_lines=source_status_lines,
                ),
                nl=False,
            )
            continue

        pieces = raw.split(maxsplit=1)
        if len(pieces) != 2:
            typer.echo("用法: r <ids> | u <ids> | t <ids> | o <id> | p | q")
            continue

        op, payload = pieces[0].lower(), pieces[1]
        if op not in {"r", "u", "t", "o"}:
            typer.echo("未知操作，只支持 r/u/t/o/p/q")
            continue

        try:
            day_ids = _parse_id_list(payload)
        except ValueError as exc:
            typer.echo(str(exc))
            continue

        resolved_map = _resolve_article_pks_by_day_ids(
            session=session,
            target_date=target_date,
            day_ids=day_ids,
        )

        if op == "o":
            day_id = day_ids[0]
            article_pk = resolved_map.get(day_id)
            if article_pk is None:
                typer.echo(f"文章不存在: day_id={day_id}, date={target_date.isoformat()}")
                continue
            article = session.get(Article, article_pk)
            if article is None:
                typer.echo(f"文章不存在: day_id={day_id}, date={target_date.isoformat()}")
                continue
            ok = webbrowser.open(article.url, new=2)
            typer.echo("已尝试打开浏览器。" if ok else "浏览器打开请求已发送（终端可能限制反馈）。")
            continue

        changed = 0
        for day_id in day_ids:
            article_pk = resolved_map.get(day_id)
            if article_pk is None:
                typer.echo(f"文章不存在: day_id={day_id}, date={target_date.isoformat()}")
                continue

            article = session.get(Article, article_pk)
            if article is None:
                typer.echo(f"文章不存在: day_id={day_id}, date={target_date.isoformat()}")
                continue

            if op == "r":
                is_read = True
            elif op == "u":
                is_read = False
            else:
                existing = session.get(ReadState, article_pk)
                is_read = not (existing.is_read if existing else False)

            service.mark(session=session, article_id=article_pk, is_read=is_read)
            changed += 1

        if changed == 0:
            continue

        session.commit()
        items = _query_article_items(session=session, target_date=target_date, mode=mode_value)
        typer.echo(f"已更新 {changed} 篇文章状态。")
        typer.echo(
            render_article_items(
                items=items,
                mode=mode_value,
                source_names=source_names,
                source_status_lines=source_status_lines,
            ),
            nl=False,
        )


@app.command("status")
def status() -> None:
    """查看最近一次同步结果。"""

    settings = get_settings()
    init_db(settings)

    with session_scope(settings) as session:
        run = session.scalar(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(1))
        if run is None:
            typer.echo("暂无同步记录。")
            _echo_ai_footer(settings)
            return

        typer.echo(
            f"最近同步 #{run.id} | 触发方式: {run.trigger} | 成功: {run.success_count} | 失败: {run.fail_count}"
        )
        typer.echo(f"session_state={_session_state(session=session, settings=settings)}")
        new_total, no_new_sources = _sync_run_new_stats(session=session, run_id=run.id)
        typer.echo(f"新增文章: {new_total}")
        if no_new_sources:
            typer.echo(f"成功但无新增: {'、'.join(no_new_sources)}")

        failed_items = session.execute(
            select(Subscription.name, SyncRunItem.error_message)
            .join(Subscription, Subscription.id == SyncRunItem.subscription_id)
            .where(
                SyncRunItem.sync_run_id == run.id,
                SyncRunItem.status == SYNC_ITEM_STATUS_FAILED,
            )
        ).all()

        if failed_items:
            typer.echo("失败项:")
            for name, error_message in failed_items:
                typer.echo(f"- {name}: {error_message or '未知错误'}")

        error_rows = session.execute(
            select(FetchAttempt.provider, FetchAttempt.error_kind, func.count())
            .where(
                FetchAttempt.sync_run_id == run.id,
                FetchAttempt.status == FETCH_STATUS_FAILED,
            )
            .group_by(FetchAttempt.provider, FetchAttempt.error_kind)
            .order_by(func.count().desc())
        ).all()
        if error_rows:
            typer.echo("错误聚合(provider + error_kind):")
            for provider_name, error_kind, count in error_rows:
                typer.echo(f"- {provider_name}:{error_kind or 'UNKNOWN'} -> {count}")

        discovery_error_rows = session.execute(
            select(DiscoveryRun.error_kind, func.count())
            .where(
                DiscoveryRun.sync_run_id == run.id,
                DiscoveryRun.status == DISCOVERY_STATUS_FAILED,
            )
            .group_by(DiscoveryRun.error_kind)
            .order_by(func.count().desc())
            .limit(5)
        ).all()
        if discovery_error_rows:
            typer.echo("发现错误聚合(error_kind):")
            for error_kind, count in discovery_error_rows:
                typer.echo(f"- {error_kind or 'UNKNOWN'} -> {count}")
    _echo_ai_footer(settings)


@app.command("login")
def login(
    provider: str = typer.Option("weread", "--provider"),
    token: str | None = typer.Option(None, "--token", help="登录态Cookie，可留空进入安全输入"),
    expires_days: int = typer.Option(30, "--expires-days", min=1, max=365),
) -> None:
    """保存本地登录态（仅本机，不上传）。"""

    settings = get_settings()
    init_db(settings)
    provider_name = provider.strip().lower() or settings.session_provider
    raw_token = token
    if raw_token is None:
        raw_token = typer.prompt("请输入登录态Cookie", hide_input=True)
    if provider_name == "weread":
        raw_token = WeReadDiscoveryProvider.parse_token_from_input(raw_token)
    secret = (raw_token or "").strip()
    if not secret:
        typer.echo("登录态为空，未保存。")
        _echo_ai_footer(settings)
        return

    vault = SessionVault(backend=settings.session_backend)
    try:
        vault.set(provider_name, secret)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"保存登录态失败: {exc}")
        _echo_ai_footer(settings)
        return

    expires_at = utcnow() + timedelta(days=expires_days)
    with session_scope(settings) as session:
        _upsert_auth_session(
            session=session,
            provider=provider_name,
            secret=secret,
            expires_at=expires_at,
        )
        session.commit()
    typer.echo(f"登录态已保存: provider={provider_name}, expires_at={expires_at.isoformat()}")
    _echo_ai_footer(settings)


@app.command("logout")
def logout(
    provider: str = typer.Option("weread", "--provider"),
) -> None:
    """删除本地登录态。"""

    settings = get_settings()
    init_db(settings)
    provider_name = provider.strip().lower() or settings.session_provider
    vault = SessionVault(backend=settings.session_backend)
    try:
        vault.delete(provider_name)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"删除登录态失败: {exc}")
        _echo_ai_footer(settings)
        return
    with session_scope(settings) as session:
        row = session.get(AuthSessionEntry, provider_name)
        if row is not None:
            session.delete(row)
            session.commit()
    typer.echo(f"登录态已删除: provider={provider_name}")
    _echo_ai_footer(settings)


@app.command("coverage")
def coverage(
    date_text: str = typer.Option(..., "--date", help="YYYY-MM-DD"),
) -> None:
    """输出指定日期覆盖率与分布。"""

    settings = get_settings()
    init_db(settings)
    target_date = _parse_date(date_text)
    service = CoverageService()
    with session_scope(settings) as session:
        report = service.compute(session=session, target_date=target_date)
        session.commit()
        typer.echo(
            "覆盖率报告: "
            f"date={report.date.isoformat()}, total={report.total_subs}, "
            f"success={report.success_subs}, delayed={report.delayed_subs}, "
            f"fail={report.fail_subs}, coverage_ratio={report.coverage_ratio:.3f}"
        )
        try:
            details = json.loads(report.detail_json)
        except Exception:
            details = []
        if isinstance(details, list) and details:
            error_counts: dict[str, int] = defaultdict(int)
            for row in details:
                if not isinstance(row, dict):
                    continue
                status = str(row.get("status") or "")
                error_kind = str(row.get("error_kind") or "")
                if status != DISCOVERY_STATUS_SUCCESS and error_kind:
                    error_counts[error_kind] += 1

            console = Console(record=True, force_terminal=False, color_system=None, width=140)
            table = Table(show_header=True, header_style="bold")
            table.add_column("公众号")
            table.add_column("状态", width=10)
            table.add_column("错误分类", width=16)
            for row in details:
                if not isinstance(row, dict):
                    continue
                table.add_row(
                    str(row.get("name") or "-"),
                    str(row.get("status") or "-"),
                    str(row.get("error_kind") or "-"),
                )
            with console.capture() as capture:
                console.print(table)
            typer.echo(capture.get(), nl=False)
            if error_counts:
                typer.echo("失败原因分布:")
                for error_kind, count in sorted(error_counts.items(), key=lambda item: item[1], reverse=True):
                    typer.echo(f"- {error_kind}: {count}")
        if report.coverage_ratio < settings.coverage_sla_target:
            typer.echo("告警: 覆盖率低于SLA阈值，请检查登录态与发现通道可用性。")
    _echo_ai_footer(settings)


@config_app.command("api")
def config_api() -> None:
    """交互式配置 AI Provider 与 API Key。"""

    env_path = _resolve_env_path(custom_path=None)
    current = _read_env_values(env_path)
    typer.echo(f"配置文件: {env_path}")

    allowed = {"auto", "openai", "deepseek"}
    provider_default = current.get("AI_PROVIDER", "auto").strip().lower() or "auto"
    provider = typer.prompt("AI_PROVIDER (auto/openai/deepseek)", default=provider_default).strip().lower()
    while provider not in allowed:
        typer.echo("仅支持 auto/openai/deepseek，请重新输入。")
        provider = typer.prompt("AI_PROVIDER (auto/openai/deepseek)", default="auto").strip().lower()

    updates: dict[str, str] = {"AI_PROVIDER": provider}

    if provider in {"auto", "openai"}:
        openai_base = current.get("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL).strip() or DEFAULT_OPENAI_BASE_URL
        updates["OPENAI_BASE_URL"] = typer.prompt("OPENAI_BASE_URL", default=openai_base).strip() or DEFAULT_OPENAI_BASE_URL

        existing_openai_key = current.get("OPENAI_API_KEY", "").strip()
        if existing_openai_key:
            if typer.confirm("检测到已配置 OPENAI_API_KEY，是否更新？", default=False):
                updates["OPENAI_API_KEY"] = typer.prompt(
                    "OPENAI_API_KEY", hide_input=True, confirmation_prompt=True
                ).strip()
        else:
            updates["OPENAI_API_KEY"] = typer.prompt(
                "OPENAI_API_KEY (可留空)", default="", show_default=False, hide_input=True
            ).strip()

    if provider in {"auto", "deepseek"}:
        deepseek_base = current.get("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL).strip() or DEFAULT_DEEPSEEK_BASE_URL
        updates["DEEPSEEK_BASE_URL"] = (
            typer.prompt("DEEPSEEK_BASE_URL", default=deepseek_base).strip() or DEFAULT_DEEPSEEK_BASE_URL
        )

        existing_deepseek_key = current.get("DEEPSEEK_API_KEY", "").strip()
        if existing_deepseek_key:
            if typer.confirm("检测到已配置 DEEPSEEK_API_KEY，是否更新？", default=False):
                updates["DEEPSEEK_API_KEY"] = typer.prompt(
                    "DEEPSEEK_API_KEY", hide_input=True, confirmation_prompt=True
                ).strip()
        else:
            updates["DEEPSEEK_API_KEY"] = typer.prompt(
                "DEEPSEEK_API_KEY (可留空)", default="", show_default=False, hide_input=True
            ).strip()

    _upsert_env_values(path=env_path, updates=updates)
    get_settings.cache_clear()
    refreshed = get_settings()
    typer.echo("配置已保存。")
    typer.echo(f"当前 provider: {refreshed.ai_provider}")
    _echo_ai_footer(refreshed)


@config_app.command("show")
def config_show() -> None:
    """显示当前配置文件与关键配置（敏感字段脱敏）。"""

    env_path = _resolve_env_path(custom_path=None)
    current = _read_env_values(env_path)
    get_settings.cache_clear()
    settings = get_settings()

    typer.echo(f"配置文件: {env_path}")
    if not env_path.exists():
        typer.echo("配置文件不存在，可执行 `wechat-agent config api` 生成。")
        _echo_ai_footer(settings)
        return

    provider = current.get("AI_PROVIDER", settings.ai_provider)
    openai_base = current.get("OPENAI_BASE_URL", settings.openai_base_url or DEFAULT_OPENAI_BASE_URL)
    deepseek_base = current.get("DEEPSEEK_BASE_URL", settings.deepseek_base_url)

    typer.echo(f"AI_PROVIDER={provider}")
    typer.echo(f"OPENAI_BASE_URL={openai_base}")
    typer.echo(f"OPENAI_API_KEY={_mask_secret(current.get('OPENAI_API_KEY'))}")
    typer.echo(f"DEEPSEEK_BASE_URL={deepseek_base}")
    typer.echo(f"DEEPSEEK_API_KEY={_mask_secret(current.get('DEEPSEEK_API_KEY'))}")
    typer.echo(f"DISCOVERY_V2_ENABLED={settings.discovery_v2_enabled}")
    typer.echo(f"SESSION_PROVIDER={settings.session_provider}")
    typer.echo(f"SESSION_BACKEND={settings.session_backend}")
    typer.echo(f"COVERAGE_SLA_TARGET={settings.coverage_sla_target:.2f}")
    _echo_ai_footer(settings)


@sub_app.command("add")
def sub_add(
    name: str = typer.Option(..., "--name"),
    wechat_id: str | None = typer.Option(None, "--wechat-id"),
) -> None:
    """新增订阅（V2: 仅需公众号名，系统自动发现）。"""

    settings = get_settings()
    init_db(settings)
    candidate_wechat_id = (wechat_id or "").strip()
    if not candidate_wechat_id:
        slug = re.sub(r"[^0-9a-zA-Z]+", "", name).lower()[:24] or "sub"
        candidate_wechat_id = f"auto_{slug}_{hashlib.sha1(name.encode('utf-8')).hexdigest()[:8]}"
        typer.echo("未提供 wechat_id，已自动生成订阅标识。")
    else:
        typer.echo("提示: --wechat-id 将在后续版本弃用，建议仅传 --name。")

    with session_scope(settings) as session:
        existing = session.scalar(select(Subscription).where(Subscription.wechat_id == candidate_wechat_id))
        if existing:
            typer.echo(f"已存在订阅: {candidate_wechat_id}")
            _echo_ai_footer(settings)
            return

        sub = Subscription(
            name=name,
            wechat_id=candidate_wechat_id,
            source_status=SOURCE_STATUS_PENDING,
            discovery_status=SOURCE_STATUS_PENDING,
        )
        session.add(sub)
        session.flush()

        discovery_note = ""
        if settings.discovery_v2_enabled:
            orchestrator = DiscoveryOrchestrator(
                providers=[
                    WeReadDiscoveryProvider(timeout_seconds=settings.http_timeout_seconds),
                    SearchIndexProvider(timeout_seconds=settings.http_timeout_seconds),
                ],
                session_vault=SessionVault(backend=settings.session_backend),
                session_provider=settings.session_provider,
                timeout_seconds=settings.http_timeout_seconds,
                midnight_shift_days=settings.midnight_shift_days,
            )
            try:
                target_date = datetime.now().date()
                day_start, _ = _day_bounds(target_date)
                discovery_result = orchestrator.discover(
                    session=session,
                    sub=sub,
                    target_date=target_date,
                    since=day_start,
                )
            except Exception as exc:  # noqa: BLE001
                discovery_result = None
                sub.discovery_status = SOURCE_STATUS_PENDING
                sub.last_error = f"PENDING_DISCOVERY: {exc}"
                discovery_note = "首次自动发现异常，已标记待发现，后续 view 会自动重试。"
            finally:
                orchestrator.close()

            if discovery_result is not None:
                if not discovery_result.ok or not discovery_result.refs:
                    sub.discovery_status = SOURCE_STATUS_PENDING
                    sub.last_error = f"PENDING_DISCOVERY: {discovery_result.error_kind or 'SEARCH_EMPTY'}"
                    discovery_note = "首次自动发现未命中，已标记待发现，后续 view 会自动重试。"
                else:
                    selected = _select_discovery_candidate(discovery_result.refs)
                    if selected is None:
                        sub.discovery_status = SOURCE_STATUS_PENDING
                        sub.last_error = "PENDING_DISCOVERY: 候选未确认"
                        discovery_note = "已创建订阅，暂未确认候选，后续 view 会继续自动发现。"
                    else:
                        session.execute(
                            delete(ArticleRef).where(
                                ArticleRef.subscription_id == sub.id,
                                ArticleRef.url != selected.url,
                            )
                        )
                        chosen = session.scalar(
                            select(ArticleRef).where(
                                ArticleRef.subscription_id == sub.id,
                                ArticleRef.url == selected.url,
                            )
                        )
                        if chosen is not None:
                            chosen.confidence = max(float(chosen.confidence or 0.0), 1.0)
                        sub.discovery_status = DISCOVERY_STATUS_SUCCESS
                        sub.source_status = SOURCE_STATUS_ACTIVE
                        sub.last_error = None
                        discovery_note = f"已自动绑定候选: {selected.url}"

        session.commit()
        typer.echo(f"已新增订阅: {name} ({candidate_wechat_id})")
        if discovery_note:
            typer.echo(discovery_note)
    _echo_ai_footer(settings)


@sub_app.command("list")
def sub_list() -> None:
    """查看订阅列表。"""

    settings = get_settings()
    init_db(settings)

    with session_scope(settings) as session:
        rows = session.scalars(select(Subscription).order_by(Subscription.created_at.asc())).all()
        if not rows:
            typer.echo("当前没有订阅。")
            _echo_ai_footer(settings)
            return

        rendered = _render_subscription_table(rows)
        typer.echo(rendered, nl=False)
    _echo_ai_footer(settings)


@sub_app.command("remove")
def sub_remove(wechat_id: str = typer.Option(..., "--wechat-id")) -> None:
    """删除订阅。"""

    settings = get_settings()
    init_db(settings)

    with session_scope(settings) as session:
        sub = session.scalar(select(Subscription).where(Subscription.wechat_id == wechat_id))
        if sub is None:
            typer.echo(f"未找到订阅: {wechat_id}")
            _echo_ai_footer(settings)
            return

        session.delete(sub)
        session.commit()
        typer.echo(f"已删除订阅: {wechat_id}")
    _echo_ai_footer(settings)


@app.command("view")
def view(
    mode: ViewMode | None = typer.Option(None, "--mode", help="source/time/recommend"),
    date_text: str | None = typer.Option(None, "--date", help="YYYY-MM-DD，默认当天"),
    strict_live: bool = typer.Option(False, "--strict-live", help="只展示本次实时抓取成功的订阅数据"),
    interactive: bool | None = typer.Option(
        None,
        "--interactive/--no-interactive",
        help="view后进入已读交互模式。默认在TTY环境自动开启。",
    ),
) -> None:
    """先同步后展示文章列表。"""

    settings = get_settings()
    init_db(settings)

    mode_value = (mode.value if mode else settings.default_view_mode).lower()
    if mode_value not in {"source", "time", "recommend"}:
        raise typer.BadParameter("mode 必须是 source/time/recommend")

    target_date = _parse_date(date_text)

    resources, sync_service = _build_runtime()
    interactive_enabled = interactive
    if interactive_enabled is None:
        interactive_enabled = sys.stdin.isatty() and sys.stdout.isatty()

    try:
        with session_scope(settings) as session:
            run = sync_service.sync(session=session, target_date=target_date, trigger="view")
            session.commit()

            allowed_sources = _live_success_source_names(session=session, run_id=run.id) if strict_live else None
            items = _query_article_items(
                session=session,
                target_date=target_date,
                mode=mode_value,
                allowed_sources=allowed_sources,
            )
            source_names = _all_subscription_names(session) if mode_value == "source" else None
            new_total, no_new_sources = _sync_run_new_stats(session=session, run_id=run.id)
            discover_ok, discover_failed, discover_delayed, source_status_lines = _sync_run_live_metrics(
                session=session,
                run_id=run.id,
                target_date=target_date,
                strict_live=strict_live,
            )
            total_subs = max(len(source_names or []), 1)
            coverage_ratio = (discover_ok + discover_delayed) / total_subs
            typer.echo(
                "同步完成: "
                f"success={run.success_count}, fail={run.fail_count}, new={new_total}, "
                f"discover_ok={discover_ok}, discover_delayed={discover_delayed}, "
                f"discover_failed={discover_failed}, coverage_ratio={coverage_ratio:.3f}"
            )
            if no_new_sources:
                typer.echo(f"本轮无新增: {'、'.join(no_new_sources)}")
            rendered = render_article_items(
                items=items,
                mode=mode_value,
                source_names=source_names,
                source_status_lines=source_status_lines if mode_value == "source" else None,
            )
            typer.echo(rendered, nl=False)
            if interactive_enabled and items:
                _interactive_read_loop(
                    session=session,
                    target_date=target_date,
                    mode_value=mode_value,
                    source_names=source_names,
                    source_status_lines=source_status_lines if mode_value == "source" else None,
                )
    finally:
        for resource in resources:
            close_fn = getattr(resource, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass
    _echo_ai_footer(settings)


@app.command("history")
def history(
    date_text: str = typer.Option(..., "--date", help="YYYY-MM-DD（必填）"),
    mode: ViewMode | None = typer.Option(None, "--mode", help="source/time/recommend"),
    interactive: bool | None = typer.Option(
        None,
        "--interactive/--no-interactive",
        help="history后进入已读交互模式。默认在TTY环境自动开启。",
    ),
) -> None:
    """按日期查询历史文章（只查库，不触发抓取）。"""

    settings = get_settings()
    init_db(settings)

    mode_value = (mode.value if mode else settings.default_view_mode).lower()
    if mode_value not in {"source", "time", "recommend"}:
        raise typer.BadParameter("mode 必须是 source/time/recommend")

    target_date = _parse_date(date_text)
    interactive_enabled = interactive
    if interactive_enabled is None:
        interactive_enabled = sys.stdin.isatty() and sys.stdout.isatty()

    with session_scope(settings) as session:
        items = _query_article_items(session=session, target_date=target_date, mode=mode_value)
        source_names = _all_subscription_names(session) if mode_value == "source" else None
        rendered = render_article_items(
            items=items,
            mode=mode_value,
            source_names=source_names,
        )
        typer.echo(f"历史查询: date={target_date.isoformat()}, mode={mode_value}")
        typer.echo(rendered, nl=False)
        if interactive_enabled and items:
            _interactive_read_loop(
                session=session,
                target_date=target_date,
                mode_value=mode_value,
                source_names=source_names,
            )
    _echo_ai_footer(settings)


@read_app.command("mark")
def read_mark(
    day_id: int = typer.Option(..., "--id", "-i"),
    date_text: str | None = typer.Option(None, "--date", help="YYYY-MM-DD，默认当天"),
    state: ReadStateValue = typer.Option(..., "--state"),
) -> None:
    """按日内ID标记文章已读/未读。"""

    settings = get_settings()
    init_db(settings)
    target_date = _parse_date(date_text)

    is_read = state.value == "read"
    service = ReadStateService()

    with session_scope(settings) as session:
        article_pk = _resolve_article_pk_by_day_id(
            session=session,
            target_date=target_date,
            day_id=day_id,
        )
        if article_pk is None:
            typer.echo(f"文章不存在: day_id={day_id}, date={target_date.isoformat()}")
            _echo_ai_footer(settings)
            return

        article = session.get(Article, article_pk)
        if article is None:
            typer.echo(f"文章不存在: day_id={day_id}, date={target_date.isoformat()}")
            _echo_ai_footer(settings)
            return

        service.mark(session=session, article_id=article_pk, is_read=is_read)
        session.commit()
        typer.echo(
            f"已更新文章状态: date={target_date.isoformat()}, id={day_id}, state={'read' if is_read else 'unread'}"
        )
    _echo_ai_footer(settings)


@app.command("open")
def open_article(
    day_id: int = typer.Option(..., "--id", "-i"),
    date_text: str | None = typer.Option(None, "--date", help="YYYY-MM-DD，默认当天"),
) -> None:
    """按日内ID在系统浏览器中打开原文。"""

    settings = get_settings()
    init_db(settings)
    target_date = _parse_date(date_text)
    with session_scope(settings) as session:
        article_pk = _resolve_article_pk_by_day_id(
            session=session,
            target_date=target_date,
            day_id=day_id,
        )
        if article_pk is None:
            typer.echo(f"文章不存在: day_id={day_id}, date={target_date.isoformat()}")
            _echo_ai_footer(settings)
            return
        article = session.get(Article, article_pk)
        if article is None:
            typer.echo(f"文章不存在: day_id={day_id}, date={target_date.isoformat()}")
            _echo_ai_footer(settings)
            return
        ok = webbrowser.open(article.url, new=2)
        typer.echo(f"已尝试打开文章: {article.url}")
        if not ok:
            typer.echo("浏览器打开请求已发送，但当前终端可能限制可见反馈。")
    _echo_ai_footer(settings)


@app.command("add")
def quick_add(
    name: str = typer.Option(..., "--name", "-n"),
    wechat_id: str | None = typer.Option(None, "--id", "-i"),
) -> None:
    """快捷命令：新增订阅。"""

    sub_add(name=name, wechat_id=wechat_id)


@app.command("list")
def quick_list() -> None:
    """快捷命令：列出订阅。"""

    sub_list()


@app.command("remove")
def quick_remove(
    wechat_id: str = typer.Option(..., "--id", "-i"),
) -> None:
    """快捷命令：删除订阅。"""

    sub_remove(wechat_id=wechat_id)


@app.command("show")
def quick_show(
    mode: ViewMode | None = typer.Option(None, "--mode", "-m"),
    date_text: str | None = typer.Option(None, "--date", "-d"),
    strict_live: bool = typer.Option(False, "--strict-live"),
    interactive: bool | None = typer.Option(None, "--interactive/--no-interactive"),
) -> None:
    """快捷命令：查看文章（会先同步）。"""

    view(
        mode=mode,
        date_text=date_text,
        strict_live=strict_live,
        interactive=interactive,
    )


@app.command("done")
def quick_done(
    ids: str = typer.Option(..., "--ids", "-i", help="逗号分隔，如 1,2,3"),
    date_text: str | None = typer.Option(None, "--date", "-d", help="YYYY-MM-DD，默认当天"),
) -> None:
    """快捷命令：批量标记已读。"""

    _bulk_mark_read(ids_raw=ids, is_read=True, target_date=_parse_date(date_text))


@app.command("todo")
def quick_todo(
    ids: str = typer.Option(..., "--ids", "-i", help="逗号分隔，如 1,2,3"),
    date_text: str | None = typer.Option(None, "--date", "-d", help="YYYY-MM-DD，默认当天"),
) -> None:
    """快捷命令：批量标记未读。"""

    _bulk_mark_read(ids_raw=ids, is_read=False, target_date=_parse_date(date_text))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
