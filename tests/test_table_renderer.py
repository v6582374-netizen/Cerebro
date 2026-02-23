from __future__ import annotations

from datetime import datetime, timezone

from wechat_agent.views.table_renderer import _title_cell
from wechat_agent.views.table_renderer import render_article_items
from wechat_agent.schemas import ArticleViewItem


def test_title_cell_contains_original_url():
    url = (
        "https://mp.weixin.qq.com/s?__biz=MzIzNjc1NzUzMw==&mid=2247870129"
        "&idx=2&sn=ed29c580fce5c207716b5a797ed1fa36"
    )
    title = _title_cell("测试标题", url)
    assert hasattr(title, "spans")
    assert getattr(title, "style", "") and url in str(getattr(title, "style", ""))


def test_source_render_includes_status_line():
    now = datetime.now(timezone.utc)
    rendered = render_article_items(
        items=[
            ArticleViewItem(
                day_id=1,
                article_pk=1,
                source_name="号A",
                published_at=now,
                title="标题",
                url="https://example.com/a",
                summary="摘要",
                is_read=False,
            )
        ],
        mode="source",
        source_names=["号A", "号B"],
        source_status_lines={"号A": "实时成功", "号B": "使用缓存(延迟1小时)"},
    )
    assert "状态: 实时成功" in rendered
    assert "状态: 使用缓存(延迟1小时)" in rendered
