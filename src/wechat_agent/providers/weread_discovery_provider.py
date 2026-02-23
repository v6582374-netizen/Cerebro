from __future__ import annotations

from datetime import date
import json
from urllib.parse import quote

import httpx

from ..schemas import DiscoveredArticleRef


class WeReadDiscoveryProvider:
    name = "weread"

    def __init__(self, timeout_seconds: int = 15, client: httpx.Client | None = None) -> None:
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=True)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def search(
        self,
        subscription_name: str,
        target_date: date,
        session_token: str | None,
        limit: int = 6,
    ) -> list[DiscoveredArticleRef]:
        if not session_token:
            raise RuntimeError("AUTH_EXPIRED: 缺少微信读书登录态")

        encoded = quote(subscription_name)
        url = f"https://weread.qq.com/web/search/global?keyword={encoded}"
        response = self.client.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2_1) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json,text/plain,*/*",
                "Cookie": session_token,
                "Referer": "https://weread.qq.com/",
            },
        )
        response.raise_for_status()
        payload = response.json()
        refs = self._extract_mp_refs(payload, target_date=target_date, limit=limit)
        return refs

    def _extract_mp_refs(self, payload: object, *, target_date: date, limit: int) -> list[DiscoveredArticleRef]:
        refs: list[DiscoveredArticleRef] = []
        seen: set[str] = set()

        def walk(obj: object) -> None:
            if len(refs) >= limit:
                return
            if isinstance(obj, dict):
                for key, value in obj.items():
                    lowered = str(key).lower()
                    if lowered in {"url", "link", "href"} and isinstance(value, str):
                        if "mp.weixin.qq.com/s" in value and value not in seen:
                            seen.add(value)
                            refs.append(
                                DiscoveredArticleRef(
                                    url=value,
                                    title_hint=None,
                                    published_at_hint=None,
                                    channel=self.name,
                                    confidence=0.85,
                                )
                            )
                    else:
                        walk(value)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item)
            elif isinstance(obj, str):
                if "mp.weixin.qq.com/s" in obj and obj not in seen:
                    seen.add(obj)
                    refs.append(
                        DiscoveredArticleRef(
                            url=obj,
                            title_hint=None,
                            published_at_hint=None,
                            channel=self.name,
                            confidence=0.75,
                        )
                    )

        walk(payload)
        return refs

    @staticmethod
    def parse_token_from_input(raw: str) -> str:
        text = (raw or "").strip()
        if not text:
            return ""
        if text.startswith("{") and text.endswith("}"):
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    cookie = str(data.get("cookie") or "").strip()
                    if cookie:
                        return cookie
            except Exception:
                pass
        return text
