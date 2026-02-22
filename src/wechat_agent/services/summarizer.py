from __future__ import annotations

import re

from openai import OpenAI

from ..schemas import RawArticle, SummaryResult


class Summarizer:
    def __init__(
        self,
        api_key: str | None,
        base_url: str | None,
        chat_model: str,
        client: OpenAI | None = None,
    ) -> None:
        self.chat_model = chat_model
        self._fallback_model = "fallback"
        if client is not None:
            self.client = client
        elif api_key:
            self.client = OpenAI(api_key=api_key, base_url=base_url)
        else:
            self.client = None

    def summarize(self, article: RawArticle) -> SummaryResult:
        if self.client is None:
            fallback = self._fallback_summary(article)
            return SummaryResult(summary_text=fallback, model=self._fallback_model, used_fallback=True)

        try:
            prompt = (
                "请将以下文章信息总结为30-50字中文短摘要，仅输出摘要本身。\n"
                f"标题：{article.title}\n"
                f"正文片段：{article.content_excerpt[:1200]}"
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
            fallback = self._fallback_summary(article)
            return SummaryResult(summary_text=fallback, model=self._fallback_model, used_fallback=True)

    def _fallback_summary(self, article: RawArticle) -> str:
        basis = article.content_excerpt.strip() or article.title.strip()
        if not basis:
            basis = "文章信息较少，建议打开原文查看完整内容。"
        return self._normalize_summary(basis, article)

    def _normalize_summary(self, text: str, article: RawArticle) -> str:
        clean_text = re.sub(r"<[^>]+>", " ", (text or ""))
        cleaned = re.sub(r"\s+", "", clean_text.strip())
        if not cleaned:
            clean_title = re.sub(r"<[^>]+>", " ", article.title.strip())
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
