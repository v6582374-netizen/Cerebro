from __future__ import annotations

from datetime import datetime, timezone
import html
import re

from ..schemas import ExtractedArticleRef, InboundMessagePayload

_URL_RE = re.compile(r"https?://mp\.weixin\.qq\.com/s\?[^\s\"'<>]+", re.IGNORECASE)
_CDATA_URL_RE = re.compile(r"<url><!\[CDATA\[(.*?)\]\]></url>", re.IGNORECASE | re.DOTALL)
_CDATA_TITLE_RE = re.compile(r"<title><!\[CDATA\[(.*?)\]\]></title>", re.IGNORECASE | re.DOTALL)


def _to_dt(ts: int | str | None) -> datetime:
    try:
        value = int(ts or 0)
    except Exception:
        value = 0
    if value <= 0:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(value, tz=timezone.utc)


class MessageExtractor:
    def extract(
        self,
        messages: list[dict],
        official_user_names: set[str],
    ) -> tuple[list[InboundMessagePayload], list[ExtractedArticleRef]]:
        inbound: list[InboundMessagePayload] = []
        refs: list[ExtractedArticleRef] = []
        seen_refs: set[tuple[str, str]] = set()

        for item in messages:
            if not isinstance(item, dict):
                continue
            msg_id = str(item.get("MsgId") or "")
            from_user_name = str(item.get("FromUserName") or "")
            msg_type = int(item.get("MsgType") or 0)
            app_msg_type = int(item.get("AppMsgType") or 0)
            content = html.unescape(str(item.get("Content") or ""))
            create_time = _to_dt(item.get("CreateTime"))

            if not msg_id:
                continue
            if not (from_user_name.startswith("gh_") or from_user_name in official_user_names):
                continue

            inbound.append(
                InboundMessagePayload(
                    msg_id=msg_id,
                    from_user_name=from_user_name,
                    msg_type=msg_type,
                    app_msg_type=app_msg_type,
                    content=content,
                    create_time=create_time,
                )
            )
            if msg_type not in {1, 49}:
                continue

            title_hint = None
            title_match = _CDATA_TITLE_RE.search(content)
            if title_match:
                title_hint = title_match.group(1).strip() or None
            elif item.get("FileName"):
                title_hint = str(item.get("FileName")).strip() or None

            urls: list[str] = []
            for match in _URL_RE.findall(content):
                urls.append(match.replace("&amp;", "&").replace("&#38;", "&"))
            for match in _CDATA_URL_RE.findall(content):
                candidate = html.unescape(match or "").strip()
                if candidate:
                    urls.append(candidate.replace("&amp;", "&").replace("&#38;", "&"))

            for url in urls:
                key = (msg_id, url)
                if key in seen_refs:
                    continue
                seen_refs.add(key)
                refs.append(
                    ExtractedArticleRef(
                        url=url,
                        title_hint=title_hint,
                        published_at_hint=create_time,
                        from_user_name=from_user_name,
                        msg_id=msg_id,
                    )
                )
        return inbound, refs
