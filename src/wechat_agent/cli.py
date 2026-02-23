from __future__ import annotations

from collections import defaultdict, deque
from datetime import date, datetime
from enum import Enum
from pathlib import Path
import re
import sys
import webbrowser

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import and_, case, func, select

from .config import (
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_OPENAI_BASE_URL,
    get_default_env_file,
    get_settings,
)
from .db import init_db, session_scope
from .models import (
    Article,
    ArticleSummary,
    FETCH_STATUS_FAILED,
    FetchAttempt,
    HEALTH_STATE_OPEN,
    ReadState,
    RecommendationScoreEntry,
    SOURCE_STATUS_ACTIVE,
    SOURCE_MODE_AUTO,
    SOURCE_MODE_MANUAL,
    SOURCE_STATUS_PENDING,
    SourceHealth,
    Subscription,
    SubscriptionSource,
    SYNC_ITEM_STATUS_FAILED,
    SYNC_ITEM_STATUS_SUCCESS,
    SyncRun,
    SyncRunItem,
    utcnow,
)
from .providers.template_feed_provider import TemplateFeedProvider
from .schemas import ArticleViewItem
from .services.fetcher import Fetcher
from .services.read_state import ReadStateService
from .services.recommender import Recommender
from .services.source_gateway import (
    MANUAL_PROVIDER,
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
source_app = typer.Typer(help="源路由与诊断")
app.add_typer(sub_app, name="sub")
app.add_typer(read_app, name="read")
app.add_typer(config_app, name="config")
app.add_typer(source_app, name="source")


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
    sync_service = SyncService(
        resolver=resolver,
        fetcher=fetcher,
        summarizer=summarizer,
        recommender=recommender,
        sync_overlap_seconds=settings.sync_overlap_seconds,
        incremental_sync_enabled=settings.incremental_sync_enabled,
        source_gateway=SourceGateway(
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
        ),
    )
    return provider, sync_service


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
        select(SourceHealth.subscription_id, func.max(SourceHealth.last_ok_at)).group_by(SourceHealth.subscription_id)
    ).all()
    return {int(sub_id): last_ok for sub_id, last_ok in rows if last_ok is not None}


def _sync_run_live_metrics(
    session,
    *,
    run_id: int,
    target_date: date,
    strict_live: bool = False,
) -> tuple[int, int, int, dict[str, str]]:
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
    table.add_column("微信号")
    table.add_column("状态")
    table.add_column("优先Provider")
    table.add_column("源URL")
    table.add_column("错误信息")

    for sub in subscriptions:
        table.add_row(
            sub.name,
            sub.wechat_id,
            sub.source_status,
            sub.preferred_provider or "-",
            sub.source_url or "-",
            sub.last_error or "-",
        )

    console.print(table)
    return console.export_text()


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


def _pin_subscription_source(
    session,
    *,
    sub: Subscription,
    provider: str,
    url: str,
) -> None:
    current_sources = session.scalars(
        select(SubscriptionSource).where(SubscriptionSource.subscription_id == sub.id)
    ).all()
    for row in current_sources:
        row.is_pinned = False

    source = session.scalar(
        select(SubscriptionSource).where(
            SubscriptionSource.subscription_id == sub.id,
            SubscriptionSource.provider == provider,
            SubscriptionSource.source_url == url,
        )
    )
    if source is None:
        source = SubscriptionSource(
            subscription_id=sub.id,
            provider=provider,
            source_url=url,
            priority=0,
            is_pinned=True,
            is_active=True,
            confidence=1.0 if provider == MANUAL_PROVIDER else 0.8,
            metadata_json='{"pinned_by":"user"}',
        )
        session.add(source)
    else:
        source.priority = 0
        source.is_pinned = True
        source.is_active = True
        source.confidence = max(float(source.confidence or 0.0), 0.8)

    sub.source_url = url
    sub.preferred_provider = provider
    sub.source_mode = SOURCE_MODE_MANUAL if provider == MANUAL_PROVIDER else SOURCE_MODE_AUTO
    sub.source_status = SOURCE_STATUS_ACTIVE
    sub.last_error = None


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
    _echo_ai_footer(settings)


@sub_app.command("add")
def sub_add(name: str = typer.Option(..., "--name"), wechat_id: str = typer.Option(..., "--wechat-id")) -> None:
    """新增订阅。"""

    settings = get_settings()
    init_db(settings)

    with session_scope(settings) as session:
        existing = session.scalar(select(Subscription).where(Subscription.wechat_id == wechat_id))
        if existing:
            typer.echo(f"已存在订阅: {wechat_id}")
            _echo_ai_footer(settings)
            return

        session.add(
            Subscription(
                name=name,
                wechat_id=wechat_id,
                source_status=SOURCE_STATUS_PENDING,
            )
        )
        session.commit()
        typer.echo(f"已新增订阅: {name} ({wechat_id})")
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


@sub_app.command("set-source")
def sub_set_source(
    wechat_id: str = typer.Option(..., "--wechat-id"),
    url: str = typer.Option(..., "--url"),
) -> None:
    """手动设置订阅源 URL。"""

    settings = get_settings()
    init_db(settings)

    with session_scope(settings) as session:
        sub = session.scalar(select(Subscription).where(Subscription.wechat_id == wechat_id))
        if sub is None:
            typer.echo(f"未找到订阅: {wechat_id}")
            _echo_ai_footer(settings)
            return

        _pin_subscription_source(
            session=session,
            sub=sub,
            provider=MANUAL_PROVIDER,
            url=url,
        )
        session.commit()
        typer.echo(f"已更新源: {wechat_id}")
    _echo_ai_footer(settings)


@source_app.command("list")
def source_list(
    wechat_id: str = typer.Option(..., "--wechat-id"),
) -> None:
    """查看某个订阅号的候选源列表与健康状态。"""

    settings = get_settings()
    init_db(settings)
    with session_scope(settings) as session:
        sub = session.scalar(select(Subscription).where(Subscription.wechat_id == wechat_id))
        if sub is None:
            typer.echo(f"未找到订阅: {wechat_id}")
            _echo_ai_footer(settings)
            return

        rows = session.execute(
            select(
                SubscriptionSource.provider,
                SubscriptionSource.source_url,
                SubscriptionSource.is_pinned,
                SubscriptionSource.is_active,
                SubscriptionSource.priority,
                SourceHealth.state,
                SourceHealth.score,
                SourceHealth.last_error,
            )
            .outerjoin(
                SourceHealth,
                and_(
                    SourceHealth.subscription_id == SubscriptionSource.subscription_id,
                    SourceHealth.provider == SubscriptionSource.provider,
                    SourceHealth.source_url == SubscriptionSource.source_url,
                ),
            )
            .where(SubscriptionSource.subscription_id == sub.id)
            .order_by(SubscriptionSource.is_pinned.desc(), SubscriptionSource.priority.asc())
        ).all()
        if not rows:
            typer.echo("暂无候选源记录。")
            _echo_ai_footer(settings)
            return

        console = Console(record=True, force_terminal=False, color_system=None, width=180)
        table = Table(show_header=True, header_style="bold")
        table.add_column("Provider", width=16)
        table.add_column("Pinned", width=8)
        table.add_column("Active", width=8)
        table.add_column("Priority", width=8)
        table.add_column("Health", width=10)
        table.add_column("Score", width=8)
        table.add_column("URL", overflow="fold")
        table.add_column("LastError", overflow="fold")

        for row in rows:
            table.add_row(
                str(row[0]),
                "yes" if bool(row[2]) else "no",
                "yes" if bool(row[3]) else "no",
                str(row[4]),
                str(row[5] or "-"),
                f"{float(row[6] or 0.0):.1f}",
                str(row[1]),
                str(row[7] or "-"),
            )
        with console.capture() as capture:
            console.print(table)
        typer.echo(capture.get(), nl=False)
    _echo_ai_footer(settings)


@source_app.command("pin")
def source_pin(
    wechat_id: str = typer.Option(..., "--wechat-id"),
    provider: str = typer.Option(..., "--provider"),
    url: str = typer.Option(..., "--url"),
) -> None:
    """手工指定并置顶一个候选源。"""

    settings = get_settings()
    init_db(settings)
    with session_scope(settings) as session:
        sub = session.scalar(select(Subscription).where(Subscription.wechat_id == wechat_id))
        if sub is None:
            typer.echo(f"未找到订阅: {wechat_id}")
            _echo_ai_footer(settings)
            return
        _pin_subscription_source(
            session=session,
            sub=sub,
            provider=provider.strip() or MANUAL_PROVIDER,
            url=url.strip(),
        )
        session.commit()
        typer.echo(f"已置顶源: {wechat_id} -> {provider}")
    _echo_ai_footer(settings)


@source_app.command("unpin")
def source_unpin(
    wechat_id: str = typer.Option(..., "--wechat-id"),
) -> None:
    """取消一个订阅号的 pinned 源。"""

    settings = get_settings()
    init_db(settings)
    with session_scope(settings) as session:
        sub = session.scalar(select(Subscription).where(Subscription.wechat_id == wechat_id))
        if sub is None:
            typer.echo(f"未找到订阅: {wechat_id}")
            _echo_ai_footer(settings)
            return
        rows = session.scalars(
            select(SubscriptionSource).where(
                SubscriptionSource.subscription_id == sub.id,
                SubscriptionSource.is_pinned.is_(True),
            )
        ).all()
        for row in rows:
            row.is_pinned = False
        sub.source_mode = SOURCE_MODE_AUTO
        if sub.preferred_provider == MANUAL_PROVIDER:
            sub.preferred_provider = None
        session.commit()
        typer.echo(f"已取消置顶源: {wechat_id}")
    _echo_ai_footer(settings)


@source_app.command("doctor")
def source_doctor(
    wechat_id: str | None = typer.Option(None, "--wechat-id"),
) -> None:
    """诊断源状态并给出修复建议。"""

    settings = get_settings()
    init_db(settings)
    with session_scope(settings) as session:
        stmt = select(Subscription)
        if wechat_id:
            stmt = stmt.where(Subscription.wechat_id == wechat_id)
        subscriptions = session.scalars(stmt.order_by(Subscription.name.asc())).all()
        if not subscriptions:
            typer.echo("未找到可诊断的订阅。")
            _echo_ai_footer(settings)
            return

        latest_run_id = session.scalar(select(SyncRun.id).order_by(SyncRun.started_at.desc()).limit(1))
        latest_errors: dict[int, str] = {}
        if latest_run_id is not None:
            for sub_id, error in session.execute(
                select(SyncRunItem.subscription_id, SyncRunItem.error_message).where(
                    SyncRunItem.sync_run_id == latest_run_id,
                    SyncRunItem.status == SYNC_ITEM_STATUS_FAILED,
                )
            ).all():
                latest_errors[int(sub_id)] = str(error or "")

        console = Console(record=True, force_terminal=False, color_system=None, width=180)
        table = Table(show_header=True, header_style="bold")
        table.add_column("公众号", width=18)
        table.add_column("状态", width=12)
        table.add_column("健康分", width=8)
        table.add_column("建议", overflow="fold")
        table.add_column("最近错误", overflow="fold")

        for sub in subscriptions:
            top_health = session.scalar(
                select(SourceHealth)
                .where(SourceHealth.subscription_id == sub.id)
                .order_by(SourceHealth.score.desc())
                .limit(1)
            )
            score = float(top_health.score) if top_health else 0.0
            state = str(top_health.state) if top_health else "UNKNOWN"
            last_error = latest_errors.get(sub.id) or sub.last_error or "-"
            if state == HEALTH_STATE_OPEN:
                advice = "源熔断中，稍后自动半开重试；可先手工 source pin。"
            elif score < 40:
                advice = "健康分偏低，建议 source pin 绑定稳定源。"
            elif sub.source_status != SOURCE_STATUS_ACTIVE:
                advice = "当前未激活，建议执行 view 触发自动修复。"
            else:
                advice = "状态正常，可继续观察。"
            table.add_row(sub.name, state, f"{score:.1f}", advice, last_error)

        with console.capture() as capture:
            console.print(table)
        typer.echo(capture.get(), nl=False)
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

    provider, sync_service = _build_runtime()
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
            live_ok, live_failed, stale_used, source_status_lines = _sync_run_live_metrics(
                session=session,
                run_id=run.id,
                target_date=target_date,
                strict_live=strict_live,
            )
            typer.echo(
                "同步完成: "
                f"success={run.success_count}, fail={run.fail_count}, new={new_total}, "
                f"live_sources_ok={live_ok}, live_sources_failed={live_failed}, stale_sources_used={stale_used}"
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
        provider.close()
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
    wechat_id: str = typer.Option(..., "--id", "-i"),
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


@app.command("set-source")
def quick_source(
    wechat_id: str = typer.Option(..., "--id", "-i"),
    url: str = typer.Option(..., "--url"),
) -> None:
    """快捷命令：手动设置订阅源。"""

    sub_set_source(wechat_id=wechat_id, url=url)


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
