from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    InboundMessageEntry,
    OfficialAccountEntry,
    Subscription,
    WeChatAccount,
    WeChatSyncState,
    utcnow,
)
from ..schemas import DiscoveredArticleRef, ExtractedArticleRef
from ..services.message_extractor import MessageExtractor
from ..services.subscription_binder import SubscriptionBinder
from ..services.wechat_web_client import WeChatWebAuthClient, WeChatWebSyncClient


class WeChatWebDiscoveryProvider:
    name = "wechat_web"

    def __init__(
        self,
        session_vault,
        session_provider: str = "wechat_web",
        timeout_seconds: int = 15,
    ) -> None:
        self.session_vault = session_vault
        self.session_provider = session_provider
        self.auth_client = WeChatWebAuthClient(timeout_seconds=timeout_seconds)
        self.sync_client = WeChatWebSyncClient(timeout_seconds=timeout_seconds)
        self.extractor = MessageExtractor()
        self.binder = SubscriptionBinder()
        self._cache_date: date | None = None
        self._refs_by_official: dict[str, list[DiscoveredArticleRef]] = {}
        self._last_metrics = {
            "sync_batches": 0,
            "official_msgs": 0,
            "article_refs_extracted": 0,
            "blocked_by_auth": 0,
        }

    def close(self) -> None:
        self.auth_client.close()
        self.sync_client.close()

    def get_last_metrics(self) -> dict[str, int]:
        return {k: int(v) for k, v in self._last_metrics.items()}

    def search_for_subscription(self, db: Session, sub: Subscription, target_date: date) -> list[DiscoveredArticleRef]:
        self._ensure_synced(db=db, target_date=target_date)
        bound_user = self.binder.bound_user_name(session=db, sub_id=sub.id)
        if bound_user:
            return list(self._refs_by_official.get(bound_user, []))
        bind_result = self.binder.auto_bind(session=db, sub=sub)
        if bind_result.ok and bind_result.official_user_name:
            return list(self._refs_by_official.get(bind_result.official_user_name, []))
        return []

    def _ensure_synced(self, db: Session, target_date: date) -> None:
        if self._cache_date == target_date:
            return
        self._cache_date = target_date
        self._refs_by_official = {}
        self._last_metrics = {
            "sync_batches": 0,
            "official_msgs": 0,
            "article_refs_extracted": 0,
            "blocked_by_auth": 0,
        }

        raw = self.session_vault.get(self.session_provider)
        if not raw:
            self._last_metrics["blocked_by_auth"] = 1
            raise RuntimeError("AUTH_REQUIRED: 请先执行 wechat-agent login 完成扫码登录")

        sess = self.auth_client.parse_session(raw)
        if sess is None or sess.expires_at <= datetime.now(timezone.utc):
            self._last_metrics["blocked_by_auth"] = 1
            raise RuntimeError("AUTH_REQUIRED: 登录态已失效，请重新执行 wechat-agent login")

        contacts = self.sync_client.refresh_contacts(sess)
        self._upsert_account_and_contacts(db=db, sess=sess, contacts=contacts)

        batch = self.sync_client.sync(sess)
        official_names = {item.user_name for item in contacts}
        inbound, refs = self.extractor.extract(messages=batch.messages, official_user_names=official_names)
        self._upsert_inbound(db=db, sess=sess, inbound=inbound)
        self._index_refs(refs=refs)
        account = db.scalar(select(WeChatAccount).where(WeChatAccount.wxuin == sess.wxuin))
        if account is not None:
            state = db.get(WeChatSyncState, account.id)
            if state is not None:
                state.sync_key_json = json.dumps(batch.sync_key, ensure_ascii=False)
                state.sync_host = batch.next_sync_host
                state.last_selector = batch.selector

        serialized = self.auth_client.serialize_session(sess)
        self.session_vault.set(self.session_provider, serialized)

        self._last_metrics["sync_batches"] = 1
        self._last_metrics["official_msgs"] = len(inbound)
        self._last_metrics["article_refs_extracted"] = len(refs)
        self._last_metrics["blocked_by_auth"] = 0

    def _upsert_account_and_contacts(self, db: Session, sess, contacts) -> None:
        account = db.scalar(select(WeChatAccount).where(WeChatAccount.wxuin == sess.wxuin))
        if account is None:
            account = WeChatAccount(wxuin=sess.wxuin, nickname=sess.nickname, status="ACTIVE", last_login_at=utcnow(), last_sync_at=utcnow())
            db.add(account)
            db.flush()
        else:
            account.nickname = sess.nickname or account.nickname
            account.status = "ACTIVE"
            account.last_login_at = utcnow()
            account.last_sync_at = utcnow()

        sync_state = db.get(WeChatSyncState, account.id)
        payload = json.dumps(sess.sync_key, ensure_ascii=False)
        if sync_state is None:
            db.add(
                WeChatSyncState(
                    account_id=account.id,
                    sync_key_json=payload,
                    sync_host=sess.sync_host,
                    last_selector=None,
                )
            )
        else:
            sync_state.sync_key_json = payload
            sync_state.sync_host = sess.sync_host

        for item in contacts:
            row = db.scalar(
                select(OfficialAccountEntry).where(
                    OfficialAccountEntry.account_id == account.id,
                    OfficialAccountEntry.user_name == item.user_name,
                )
            )
            if row is None:
                db.add(
                    OfficialAccountEntry(
                        account_id=account.id,
                        user_name=item.user_name,
                        nick_name=item.nick_name,
                        verify_flag=item.verify_flag,
                    )
                )
            else:
                row.nick_name = item.nick_name
                row.verify_flag = int(item.verify_flag)

    def _upsert_inbound(self, db: Session, sess, inbound) -> None:
        account = db.scalar(select(WeChatAccount).where(WeChatAccount.wxuin == sess.wxuin))
        if account is None:
            return
        for item in inbound:
            row = db.scalar(
                select(InboundMessageEntry).where(
                    InboundMessageEntry.account_id == account.id,
                    InboundMessageEntry.msg_id == item.msg_id,
                )
            )
            digest = hashlib.sha256(item.content.encode("utf-8")).hexdigest()
            if row is None:
                db.add(
                    InboundMessageEntry(
                        account_id=account.id,
                        msg_id=item.msg_id,
                        from_user_name=item.from_user_name,
                        msg_type=item.msg_type,
                        app_msg_type=item.app_msg_type,
                        create_time=item.create_time,
                        content_hash=digest,
                    )
                )

    def _index_refs(self, refs: list[ExtractedArticleRef]) -> None:
        for item in refs:
            ref = DiscoveredArticleRef(
                url=item.url,
                title_hint=item.title_hint,
                published_at_hint=item.published_at_hint,
                channel=self.name,
                confidence=0.95,
            )
            bucket = self._refs_by_official.setdefault(item.from_user_name, [])
            bucket.append(ref)
