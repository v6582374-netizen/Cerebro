from __future__ import annotations

import html
import re
from urllib.parse import urlparse

import httpx

from openai import OpenAI

from ..schemas import RawArticle, SummaryResult

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_WECHAT_CONTENT_RE = re.compile(r'<div[^>]+id=["\']js_content["\'][^>]*>(.*?)</div>', re.IGNORECASE | re.DOTALL)
_ARTICLE_RE = re.compile(r"<article[^>]*>(.*?)</article>", re.IGNORECASE | re.DOTALL)
_BODY_RE = re.compile(r"<body[^>]*>(.*?)</body>", re.IGNORECASE | re.DOTALL)
_DATE_RE = re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:\s*\d{1,2}:\d{2})?\b")
_NOISE_PATTERNS = [
    re.compile(r"关注前沿科技"),
    re.compile(r"\b原创\b"),
    re.compile(r"发布于"),
    re.compile(r"发表于"),
    re.compile(r"作者[:：]\S+"),
    re.compile(r"编辑[:：]\S+"),
]


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
                "请基于提供的文章正文内容，输出30-50字中文摘要，仅输出摘要本身，不要作者信息或时间戳。\n"
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
            if full_text and len(full_text) >= 200:
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
        candidate = ""

        wechat_match = _WECHAT_CONTENT_RE.search(text)
        if wechat_match:
            candidate = wechat_match.group(1)
        else:
            article_match = _ARTICLE_RE.search(text)
            if article_match:
                candidate = article_match.group(1)
            else:
                body_match = _BODY_RE.search(text)
                if body_match:
                    candidate = body_match.group(1)
                else:
                    candidate = text

        return self._clean_text(candidate)

    def _clean_text(self, raw: str) -> str:
        unescaped = html.unescape(raw or "")
        no_tag = _TAG_RE.sub(" ", unescaped)
        compact = re.sub(r"\s+", " ", no_tag).strip()
        compact = _DATE_RE.sub(" ", compact)
        for pattern in _NOISE_PATTERNS:
            compact = pattern.sub(" ", compact)
        compact = re.sub(r"\s+", " ", compact).strip()
        return compact

    def _normalize_summary(self, text: str, article: RawArticle) -> str:
        clean_text = _TAG_RE.sub(" ", (text or ""))
        cleaned = re.sub(r"\s+", "", clean_text.strip())
        if not cleaned:
            clean_title = _TAG_RE.sub(" ", article.title.strip())
            cleaned = re.sub(r"\s+", "", clean_title)

        if len(cleaned) > 50:
            return cleaned[:50]

        if len(cleaned) >= 30:
            return cleaned

        supplement = "建议阅读全文了解细节"
        merged = cleaned
        while len(merged) < 30:
            merged = f"{merged}{supplement}"
        return merged[:50]
