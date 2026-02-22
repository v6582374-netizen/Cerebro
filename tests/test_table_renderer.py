from __future__ import annotations

from wechat_agent.views.table_renderer import _title_cell


def test_title_cell_contains_original_url():
    url = (
        "https://mp.weixin.qq.com/s?__biz=MzIzNjc1NzUzMw==&mid=2247870129"
        "&idx=2&sn=ed29c580fce5c207716b5a797ed1fa36"
    )
    title = _title_cell("测试标题", url)
    assert hasattr(title, "spans")
    assert getattr(title, "style", "") and url in str(getattr(title, "style", ""))
