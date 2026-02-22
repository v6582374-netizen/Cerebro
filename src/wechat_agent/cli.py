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
    ReadState,
    RecommendationScoreEntry,
    SOURCE_STATUS_ACTIVE,
    SOURCE_STATUS_PENDING,
    Subscription,
    SYNC_ITEM_STATUS_FAILED,
    SyncRun,
    SyncRunItem,
)
from .providers.template_feed_provider import TemplateFeedProvider
from .schemas import ArticleViewItem
from .services.fetcher import Fetcher
from .services.read_state import ReadStateService
from .services.recommender import Recommender
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
    sync_service = SyncService(
        resolver=resolver,
        fetcher=fetcher,
        summarizer=summarizer,
        recommender=recommender,
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


def _query_article_items(session, target_date: date, mode: str) -> list[ArticleViewItem]:
    day_start, day_end = _day_bounds(target_date)

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

    def normalize_with_title(title: str) -> str:
        cleaned = tag_re.sub(" ", title or "")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            cleaned = "正文抓取失败，建议打开原文查看。"
        if len(cleaned) > 50:
            return f"{cleaned[:49].rstrip('，,、；;：:')}…"
        return cleaned

    def clean_summary(raw: str | None, title: str) -> str:
        if not raw:
            return normalize_with_title(title)
        no_tag = tag_re.sub(" ", raw)
        compact = re.sub(r"\s+", " ", no_tag).strip()
        compact = re.sub(r'^[\"\'“”‘’]+|[\"\'“”‘’]+$', "", compact).strip()
        compact = re.sub(r"^(摘要|总结|内容摘要|摘要如下)\s*[:：]\s*", "", compact).strip()
        if not compact:
            return normalize_with_title(title)
        if len(compact) > 50:
            return f"{compact[:49].rstrip('，,、；;：:')}…"
        return compact

    rows = session.execute(stmt).all()
    items = [
        ArticleViewItem(
            article_id=row[0],
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

    if mode == "recommend":
        read_count = session.scalar(select(func.count()).select_from(ReadState).where(ReadState.is_read.is_(True))) or 0
        if read_count == 0:
            items = sorted(items, key=lambda x: x.published_at, reverse=True)
            items = _round_robin_by_source(items)

    return items


def _render_subscription_table(subscriptions: list[Subscription]) -> str:
    console = Console(record=True, force_terminal=False, color_system=None, width=140)
    table = Table(show_header=True, header_style="bold")
    table.add_column("公众号")
    table.add_column("微信号")
    table.add_column("状态")
    table.add_column("源URL")
    table.add_column("错误信息")

    for sub in subscriptions:
        table.add_row(
            sub.name,
            sub.wechat_id,
            sub.source_status,
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


def _bulk_mark_read(ids_raw: str, is_read: bool) -> None:
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
        for article_id in ids:
            article = session.get(Article, article_id)
            if article is None:
                typer.echo(f"文章不存在: {article_id}")
                continue
            service.mark(session=session, article_id=article_id, is_read=is_read)
            changed += 1
        if changed > 0:
            session.commit()

    typer.echo(f"已批量更新 {changed} 篇文章状态为: {'read' if is_read else 'unread'}")
    _echo_ai_footer(settings)


def _interactive_read_loop(
    session,
    target_date: date,
    mode_value: str,
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
            typer.echo(render_article_items(items=items, mode=mode_value), nl=False)
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
            ids = _parse_id_list(payload)
        except ValueError as exc:
            typer.echo(str(exc))
            continue

        if op == "o":
            article_id = ids[0]
            article = session.get(Article, article_id)
            if article is None:
                typer.echo(f"文章不存在: {article_id}")
                continue
            ok = webbrowser.open(article.url, new=2)
            typer.echo("已尝试打开浏览器。" if ok else "浏览器打开请求已发送（终端可能限制反馈）。")
            continue

        changed = 0
        for article_id in ids:
            article = session.get(Article, article_id)
            if article is None:
                typer.echo(f"文章不存在: {article_id}")
                continue

            if op == "r":
                is_read = True
            elif op == "u":
                is_read = False
            else:
                existing = session.get(ReadState, article_id)
                is_read = not (existing.is_read if existing else False)

            service.mark(session=session, article_id=article_id, is_read=is_read)
            changed += 1

        if changed == 0:
            continue

        session.commit()
        items = _query_article_items(session=session, target_date=target_date, mode=mode_value)
        typer.echo(f"已更新 {changed} 篇文章状态。")
        typer.echo(render_article_items(items=items, mode=mode_value), nl=False)


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

        sub.source_url = url
        sub.source_status = SOURCE_STATUS_ACTIVE
        sub.last_error = None
        session.commit()
        typer.echo(f"已更新源: {wechat_id}")
    _echo_ai_footer(settings)


@app.command("view")
def view(
    mode: ViewMode | None = typer.Option(None, "--mode", help="source/time/recommend"),
    date_text: str | None = typer.Option(None, "--date", help="YYYY-MM-DD，默认当天"),
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

            items = _query_article_items(session=session, target_date=target_date, mode=mode_value)
            typer.echo(f"同步完成: success={run.success_count}, fail={run.fail_count}")
            rendered = render_article_items(items=items, mode=mode_value)
            typer.echo(rendered, nl=False)
            if interactive_enabled and items:
                _interactive_read_loop(
                    session=session,
                    target_date=target_date,
                    mode_value=mode_value,
                )
    finally:
        provider.close()
    _echo_ai_footer(settings)


@read_app.command("mark")
def read_mark(
    article_id: int = typer.Option(..., "--article-id"),
    state: ReadStateValue = typer.Option(..., "--state"),
) -> None:
    """标记文章已读/未读。"""

    settings = get_settings()
    init_db(settings)

    is_read = state.value == "read"
    service = ReadStateService()

    with session_scope(settings) as session:
        article = session.get(Article, article_id)
        if article is None:
            typer.echo(f"文章不存在: {article_id}")
            _echo_ai_footer(settings)
            return

        service.mark(session=session, article_id=article_id, is_read=is_read)
        session.commit()
        typer.echo(f"已更新文章 {article_id} 状态为: {'read' if is_read else 'unread'}")
    _echo_ai_footer(settings)


@app.command("open")
def open_article(
    article_id: int = typer.Option(..., "--article-id", "-i"),
) -> None:
    """直接在系统浏览器中打开原文。"""

    settings = get_settings()
    init_db(settings)
    with session_scope(settings) as session:
        article = session.get(Article, article_id)
        if article is None:
            typer.echo(f"文章不存在: {article_id}")
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


@app.command("source")
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
    interactive: bool | None = typer.Option(None, "--interactive/--no-interactive"),
) -> None:
    """快捷命令：查看文章（会先同步）。"""

    view(
        mode=mode,
        date_text=date_text,
        interactive=interactive,
    )


@app.command("done")
def quick_done(
    ids: str = typer.Option(..., "--ids", "-i", help="逗号分隔，如 1,2,3"),
) -> None:
    """快捷命令：批量标记已读。"""

    _bulk_mark_read(ids_raw=ids, is_read=True)


@app.command("todo")
def quick_todo(
    ids: str = typer.Option(..., "--ids", "-i", help="逗号分隔，如 1,2,3"),
) -> None:
    """快捷命令：批量标记未读。"""

    _bulk_mark_read(ids_raw=ids, is_read=False)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
