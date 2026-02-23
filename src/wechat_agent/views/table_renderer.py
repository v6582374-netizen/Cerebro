from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
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
        expand=True,
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

    row = [
        str(item.day_id),
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


def render_article_items(
    items: list[ArticleViewItem],
    mode: str,
    source_names: list[str] | None = None,
    source_status_lines: dict[str, str] | None = None,
) -> str:
    console = Console(
        force_terminal=True,
        color_system="standard",
        markup=False,
        highlight=False,
    )
    with console.capture() as capture:
        if mode == "source":
            grouped: dict[str, list[ArticleViewItem]] = defaultdict(list)
            for item in items:
                grouped[item.source_name].append(item)

            names = sorted(set(source_names or grouped.keys()))
            if not names:
                console.print("当天没有可展示的文章。")
            for index, source_name in enumerate(names):
                if index > 0:
                    console.print()
                console.print(source_name)
                if source_status_lines and source_name in source_status_lines:
                    console.print(f"状态: {source_status_lines[source_name]}")
                source_items = grouped.get(source_name, [])
                if not source_items:
                    console.print("当天无更新。")
                    continue
                table = _build_table(include_source=False, include_score=False)
                for item in source_items:
                    _add_item(table, item, include_source=False, include_score=False)
                console.print(table)
        else:
            if not items:
                console.print("当天没有可展示的文章。")
                return capture.get()
            include_score = mode == "recommend"
            table = _build_table(include_source=True, include_score=include_score)
            for item in items:
                _add_item(table, item, include_source=True, include_score=include_score)
            console.print(table)
    return capture.get()
