from __future__ import annotations

from difflib import SequenceMatcher
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BIND_STATUS_BOUND, OfficialAccountEntry, Subscription, SubscriptionBinding
from ..schemas import BindResult


def _norm(text: str) -> str:
    value = (text or "").strip().lower()
    return re.sub(r"[\W_]+", "", value)


class SubscriptionBinder:
    def find_candidates(self, session: Session, subscription_name: str) -> list[tuple[str, str, float]]:
        target = _norm(subscription_name)
        if not target:
            return []
        rows = session.scalars(select(OfficialAccountEntry).order_by(OfficialAccountEntry.nick_name.asc())).all()
        candidates: list[tuple[str, str, float]] = []
        for row in rows:
            candidate = _norm(row.nick_name)
            if not candidate:
                continue
            if candidate == target:
                score = 1.0
            elif target in candidate or candidate in target:
                score = 0.90
            else:
                ratio = SequenceMatcher(None, target, candidate).ratio()
                if ratio < 0.50:
                    continue
                score = 0.50 + ratio * 0.4
            candidates.append((row.user_name, row.nick_name, score))
        candidates.sort(key=lambda item: item[2], reverse=True)
        return candidates

    def auto_bind(self, session: Session, sub: Subscription) -> BindResult:
        candidates = self.find_candidates(session=session, subscription_name=sub.name)
        if not candidates:
            return BindResult(ok=False, official_user_name=None, confidence=0.0, reason="NO_CANDIDATE")
        top = candidates[0]
        if len(candidates) > 1 and top[2] - candidates[1][2] < 0.1:
            return BindResult(ok=False, official_user_name=None, confidence=top[2], reason="AMBIGUOUS")
        self.bind(session=session, sub=sub, official_user_name=top[0], confidence=top[2])
        return BindResult(ok=True, official_user_name=top[0], confidence=top[2], reason="AUTO_BOUND")

    def bind(self, session: Session, sub: Subscription, official_user_name: str, confidence: float = 1.0) -> None:
        row = session.get(SubscriptionBinding, sub.id)
        if row is None:
            session.add(
                SubscriptionBinding(
                    subscription_id=sub.id,
                    official_user_name=official_user_name,
                    bind_status=BIND_STATUS_BOUND,
                    confidence=float(confidence),
                )
            )
            return
        row.official_user_name = official_user_name
        row.bind_status = BIND_STATUS_BOUND
        row.confidence = float(confidence)

    def bound_user_name(self, session: Session, sub_id: int) -> str | None:
        row = session.get(SubscriptionBinding, sub_id)
        if row is None:
            return None
        return str(row.official_user_name or "") or None
