from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import ReadState, utcnow


class ReadStateService:
    def mark(self, session: Session, article_id: int, is_read: bool) -> None:
        state = session.get(ReadState, article_id)
        if state is None:
            session.add(
                ReadState(
                    article_id=article_id,
                    is_read=is_read,
                    read_at=utcnow() if is_read else None,
                )
            )
            return

        state.is_read = is_read
        state.read_at = utcnow() if is_read else None
