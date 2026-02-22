from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from io import StringIO
import re

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from ..schemas import ArticleViewItem


def _format_time(dt: datetime) -> str:
    value = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


def _read_flag(is_read: bool) -> str:
    return "[x]" if is_read else "[ ]"


def _build_table(include_source: bool, include_score: bool) -> Table:
    table = Table(
        show_header=True,
        header_style="bold cyan",
        box=box.SQUARE,
        show_lines=True,
        pad_edge=True,
    )
    table.add_column("ID", justify="right", width=4, no_wrap=True)
    table.add_column("已读", width=5, no_wrap=True)
    if include_source:
        table.add_column("公众号", width=12, overflow="fold")
    table.add_column("更新时间", width=16, no_wrap=True)
    table.add_column("标题(可点击)", ratio=3, overflow="fold")
    table.add_column("AI摘要", ratio=4, overflow="fold")
    if include_score:
        table.add_column("推荐分", justify="right", width=8)
    return table


def _title_cell(title: str, url: str) -> Text | str:
    if not url:
        return title
    # Keep the exact original URL as click target to avoid parameter loss.
    return Text(title, style=f"link {url}")


def _add_item(table: Table, item: ArticleViewItem, include_source: bool, include_score: bool) -> None:
    summary = re.sub(r"\s+", " ", item.summary or "").strip()
    if len(summary) > 50:
        summary = f"{summary[:49].rstrip('，,、；;：:')}…"

    row = [
        str(item.article_id),
        _read_flag(item.is_read),
    ]
    if include_source:
        row.append(item.source_name)
    row.extend(
        [
            _format_time(item.published_at),
            _title_cell(item.title, item.url),
            summary,
        ]
    )
    if include_score:
        row.append(f"{(item.score or 0.0):.3f}")
    table.add_row(*row)


def render_article_items(items: list[ArticleViewItem], mode: str) -> str:
    console = Console(
        record=True,
        force_terminal=True,
        color_system=None,
        width=180,
        markup=False,
        file=StringIO(),
    )

    if not items:
        console.print("当天没有可展示的文章。")
        return console.export_text()

    if mode == "source":
        grouped: dict[str, list[ArticleViewItem]] = defaultdict(list)
        for item in items:
            grouped[item.source_name].append(item)

        for index, source_name in enumerate(sorted(grouped.keys())):
            if index > 0:
                console.print()
            console.print(source_name)
            table = _build_table(include_source=False, include_score=False)
            for item in grouped[source_name]:
                _add_item(table, item, include_source=False, include_score=False)
            console.print(table)
        return console.export_text(styles=True)

    include_score = mode == "recommend"
    table = _build_table(include_source=True, include_score=include_score)
    for item in items:
        _add_item(table, item, include_source=True, include_score=include_score)
    console.print(table)
    return console.export_text(styles=True)
