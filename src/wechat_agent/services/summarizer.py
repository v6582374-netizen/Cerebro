from __future__ import annotations

import html
from html.parser import HTMLParser
import re
from urllib.parse import urlparse

import httpx

from openai import OpenAI

from ..schemas import RawArticle, SummaryResult

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_DATE_RE = re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:\s*\d{1,2}:\d{2})?\b")
_NOISE_PATTERNS = [
    re.compile(r"关注前沿科技"),
    re.compile(r"\b原创\b"),
    re.compile(r"发布于"),
    re.compile(r"发表于"),
    re.compile(r"作者[:：]\S+"),
    re.compile(r"编辑[:：]\S+"),
]


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
            self.chunks.append(data)

    def text(self) -> str:
        return " ".join(self.chunks).strip()


class Summarizer:
    def __init__(
        self,
        api_key: str | None,
        base_url: str | None,
        chat_model: str,
        fetch_timeout_seconds: int = 15,
        source_char_limit: int = 6000,
        client: OpenAI | None = None,
    ) -> None:
        self.chat_model = chat_model
        self._fallback_model = "fallback"
        self.fetch_timeout_seconds = fetch_timeout_seconds
        self.source_char_limit = source_char_limit
        self._content_cache: dict[str, str] = {}
        if client is not None:
            self.client = client
        elif api_key:
            self.client = OpenAI(api_key=api_key, base_url=base_url)
        else:
            self.client = None

    def summarize(self, article: RawArticle) -> SummaryResult:
        source_text = self._build_source_text(article)
        if self.client is None:
            fallback = self._fallback_summary(article, source_text)
            return SummaryResult(summary_text=fallback, model=self._fallback_model, used_fallback=True)

        try:
            prompt = (
                "请阅读下面的文章正文并总结为一条中文摘要。\n"
                "要求：不超过50字；完整一句话；不要换行；不要引号；不要作者/时间等元信息；仅输出摘要。\n"
                f"标题：{article.title}\n"
                f"正文：{source_text[: self.source_char_limit]}"
            )
            resp = self.client.chat.completions.create(
                model=self.chat_model,
                temperature=0.2,
                max_tokens=120,
                messages=[
                    {"role": "system", "content": "你是精炼的信息摘要助手。"},
                    {"role": "user", "content": prompt},
                ],
            )
            text = (resp.choices[0].message.content or "").strip()
            normalized = self._normalize_summary(text, article)
            return SummaryResult(summary_text=normalized, model=self.chat_model, used_fallback=False)
        except Exception:  # noqa: BLE001
            fallback = self._fallback_summary(article, source_text)
            return SummaryResult(summary_text=fallback, model=self._fallback_model, used_fallback=True)

    def _fallback_summary(self, article: RawArticle, source_text: str | None = None) -> str:
        basis = (source_text or "").strip() or article.content_excerpt.strip() or article.title.strip()
        if not basis:
            basis = "文章信息较少，建议打开原文查看完整内容。"
        return self._normalize_summary(basis, article)

    def _build_source_text(self, article: RawArticle) -> str:
        if self.client is not None:
            full_text = self._fetch_full_article_text(article.url)
            if full_text:
                return full_text[: self.source_char_limit]

        excerpt = self._clean_text(article.content_excerpt)
        title = self._clean_text(article.title)
        merged = f"{title}\n{excerpt}".strip()
        return merged[: self.source_char_limit]

    def _fetch_full_article_text(self, url: str) -> str:
        if not url:
            return ""
        cached = self._content_cache.get(url)
        if cached is not None:
            return cached

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            self._content_cache[url] = ""
            return ""

        try:
            response = httpx.get(
                url,
                timeout=self.fetch_timeout_seconds,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2_1) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            response.raise_for_status()
            extracted = self._extract_main_text(response.text)
            self._content_cache[url] = extracted
            return extracted
        except Exception:  # noqa: BLE001
            self._content_cache[url] = ""
            return ""

    def _extract_main_text(self, html_text: str) -> str:
        text = _SCRIPT_STYLE_RE.sub(" ", html_text)
        candidate = self._extract_element_text(text, target_id="js_content")

        if not candidate:
            candidate = self._extract_element_text(text, target_tag="article")
        if not candidate:
            candidate = self._extract_element_text(text, target_tag="body")
        if not candidate:
            candidate = text

        return self._clean_text(candidate)

    def _extract_element_text(
        self,
        html_text: str,
        target_id: str | None = None,
        target_tag: str | None = None,
    ) -> str:
        try:
            parser = _ElementTextParser(target_id=target_id, target_tag=target_tag)
            parser.feed(html_text)
            parser.close()
            return parser.text()
        except Exception:  # noqa: BLE001
            return ""

    def _clean_text(self, raw: str) -> str:
        unescaped = html.unescape(raw or "")
        no_tag = _TAG_RE.sub(" ", unescaped)
        compact = re.sub(r"\s+", " ", no_tag).strip()
        compact = _DATE_RE.sub(" ", compact)
        for pattern in _NOISE_PATTERNS:
            compact = pattern.sub(" ", compact)
        compact = re.sub(r"\s+", " ", compact).strip()
        return compact

    def _truncate_summary(self, text: str, limit: int = 50) -> str:
        if len(text) <= limit:
            return text

        for sep in ("。", "！", "？", ".", "!", "?", "；", ";", "，", ",", "、"):
            idx = text.rfind(sep, 0, limit + 1)
            if idx >= int(limit * 0.6):
                return text[: idx + 1].strip()

        clipped = text[: max(limit - 1, 1)].rstrip("，,、；;：:")
        return f"{clipped}…"

    def _normalize_summary(self, text: str, article: RawArticle) -> str:
        cleaned = self._clean_text(text or "")
        cleaned = re.sub(r'^[\"\'“”‘’]+|[\"\'“”‘’]+$', "", cleaned).strip()
        cleaned = re.sub(r"^(摘要|总结|内容摘要|摘要如下)\s*[:：]\s*", "", cleaned).strip()
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        if not cleaned:
            cleaned = self._clean_text(article.title)
        if not cleaned:
            cleaned = "正文抓取失败，建议打开原文查看。"

        return self._truncate_summary(cleaned, limit=50)
