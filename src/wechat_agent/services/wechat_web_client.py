from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import random
import re
import time
from urllib.parse import quote_plus, urlparse

import httpx

from ..schemas import AuthProgress, OfficialAccount, QrLoginSession, SyncBatch, WeChatSession

_UUID_RE = re.compile(r'window\.QRLogin\.uuid\s*=\s*"([^"]+)"')
_LOGIN_CODE_RE = re.compile(r"window\.code=(\d+);")
_REDIRECT_RE = re.compile(r'window\.redirect_uri="([^"]+)"')

_XML_FIELD_RE = {
    "skey": re.compile(r"<skey><!\[CDATA\[(.*?)\]\]></skey>", re.DOTALL),
    "wxsid": re.compile(r"<wxsid><!\[CDATA\[(.*?)\]\]></wxsid>", re.DOTALL),
    "wxuin": re.compile(r"<wxuin><!\[CDATA\[(.*?)\]\]></wxuin>", re.DOTALL),
    "pass_ticket": re.compile(r"<pass_ticket><!\[CDATA\[(.*?)\]\]></pass_ticket>", re.DOTALL),
}

_SYNC_RE = re.compile(r'window\.synccheck=\{retcode:"(\d+)",selector:"(\d+)"\}')

_SYNC_HOSTS = (
    "webpush.wx.qq.com",
    "webpush.weixin.qq.com",
    "webpush2.weixin.qq.com",
    "webpush2.wx.qq.com",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _device_id() -> str:
    return "e" + "".join(random.choice("0123456789") for _ in range(15))


def _parse_xml_field(name: str, text: str) -> str:
    pattern = _XML_FIELD_RE[name]
    match = pattern.search(text)
    if not match:
        return ""
    return match.group(1).strip()


def _session_to_json(sess: WeChatSession) -> str:
    payload = {
        "base_uri": sess.base_uri,
        "wxuin": sess.wxuin,
        "sid": sess.sid,
        "skey": sess.skey,
        "pass_ticket": sess.pass_ticket,
        "device_id": sess.device_id,
        "sync_key": sess.sync_key,
        "sync_host": sess.sync_host,
        "cookies": sess.cookies,
        "expires_at": sess.expires_at.isoformat(),
        "nickname": sess.nickname,
    }
    return json.dumps(payload, ensure_ascii=False)


def _session_from_json(raw: str) -> WeChatSession | None:
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        expires_at = datetime.fromisoformat(str(payload.get("expires_at")))
    except Exception:
        expires_at = _now() - timedelta(seconds=1)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return WeChatSession(
        base_uri=str(payload.get("base_uri") or ""),
        wxuin=str(payload.get("wxuin") or ""),
        sid=str(payload.get("sid") or ""),
        skey=str(payload.get("skey") or ""),
        pass_ticket=str(payload.get("pass_ticket") or ""),
        device_id=str(payload.get("device_id") or _device_id()),
        sync_key=payload.get("sync_key") if isinstance(payload.get("sync_key"), dict) else {},
        sync_host=str(payload.get("sync_host") or _SYNC_HOSTS[0]),
        cookies={str(k): str(v) for k, v in (payload.get("cookies") or {}).items()},
        expires_at=expires_at,
        nickname=(str(payload.get("nickname")) if payload.get("nickname") else None),
    )


class WeChatWebAuthClient:
    def __init__(self, base_url: str = "https://wx.qq.com", timeout_seconds: int = 15) -> None:
        self.base_url = base_url.rstrip("/")
        self.http_client = httpx.Client(timeout=timeout_seconds, follow_redirects=True)

    def close(self) -> None:
        self.http_client.close()

    def start(self) -> QrLoginSession:
        redirect_uri = quote_plus(f"{self.base_url}/cgi-bin/mmwebwx-bin/webwxnewloginpage")
        url = (
            "https://login.wx.qq.com/jslogin"
            f"?appid=wx782c26e4c19acffb&redirect_uri={redirect_uri}&fun=new&lang=zh_CN&_={int(time.time() * 1000)}"
        )
        resp = self.http_client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        match = _UUID_RE.search(resp.text)
        if not match:
            raise RuntimeError("扫码登录初始化失败：未获取到UUID")
        uuid = match.group(1).strip()
        qr_url = f"https://login.weixin.qq.com/qrcode/{uuid}"
        return QrLoginSession(uuid=uuid, qr_url=qr_url, started_at=_now())

    def poll(self, session: QrLoginSession) -> AuthProgress:
        url = (
            "https://login.wx.qq.com/cgi-bin/mmwebwx-bin/login"
            f"?tip=1&uuid={session.uuid}&r={~int(time.time())}&_={int(time.time() * 1000)}"
        )
        resp = self.http_client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        text = resp.text
        code_match = _LOGIN_CODE_RE.search(text)
        if not code_match:
            return AuthProgress(status="failed", code=-1, message="无法解析扫码状态")
        code = int(code_match.group(1))
        if code == 408:
            return AuthProgress(status="waiting", code=code, message="等待扫码")
        if code == 201:
            return AuthProgress(status="scanned", code=code, message="已扫码，请在手机确认登录")
        if code == 200:
            redirect_match = _REDIRECT_RE.search(text)
            redirect_uri = redirect_match.group(1) if redirect_match else None
            return AuthProgress(status="confirmed", code=code, redirect_uri=redirect_uri, message="登录确认成功")
        if code in {400, 500, 502}:
            return AuthProgress(status="expired", code=code, message="二维码已过期")
        return AuthProgress(status="failed", code=code, message=f"未知登录状态码: {code}")

    def finish(self, progress: AuthProgress) -> WeChatSession:
        if progress.status != "confirmed" or not progress.redirect_uri:
            raise RuntimeError("登录未确认，无法完成会话初始化")
        redirect = progress.redirect_uri
        if "fun=" not in redirect:
            redirect = f"{redirect}&fun=new&version=v2"
        resp = self.http_client.get(redirect, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        body = resp.text
        skey = _parse_xml_field("skey", body)
        sid = _parse_xml_field("wxsid", body)
        wxuin = _parse_xml_field("wxuin", body)
        pass_ticket = _parse_xml_field("pass_ticket", body)
        if not all([skey, sid, wxuin, pass_ticket]):
            raise RuntimeError("登录成功但会话字段不完整")

        parsed = urlparse(progress.redirect_uri)
        base_uri = f"{parsed.scheme}://{parsed.netloc}"
        cookies = {str(k): str(v) for k, v in resp.cookies.items()}
        device_id = _device_id()
        base_request = {
            "Uin": int(wxuin),
            "Sid": sid,
            "Skey": skey,
            "DeviceID": device_id,
        }
        init_resp = self.http_client.post(
            f"{base_uri}/cgi-bin/mmwebwx-bin/webwxinit",
            params={"r": ~int(time.time()), "lang": "zh_CN", "pass_ticket": pass_ticket},
            json={"BaseRequest": base_request},
            headers={"Content-Type": "application/json;charset=UTF-8", "User-Agent": "Mozilla/5.0"},
            cookies=cookies,
        )
        init_resp.raise_for_status()
        payload = init_resp.json()
        ret = int(payload.get("BaseResponse", {}).get("Ret", -1))
        if ret != 0:
            raise RuntimeError(f"webwxinit失败: ret={ret}")
        sync_key = payload.get("SyncKey") if isinstance(payload.get("SyncKey"), dict) else {}
        user = payload.get("User") if isinstance(payload.get("User"), dict) else {}
        nickname = str(user.get("NickName") or "").strip() or None

        return WeChatSession(
            base_uri=base_uri,
            wxuin=wxuin,
            sid=sid,
            skey=skey,
            pass_ticket=pass_ticket,
            device_id=device_id,
            sync_key=sync_key,
            sync_host=_SYNC_HOSTS[0],
            cookies=cookies,
            expires_at=_now() + timedelta(days=2),
            nickname=nickname,
        )

    @staticmethod
    def session_fingerprint(raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def serialize_session(sess: WeChatSession) -> str:
        return _session_to_json(sess)

    @staticmethod
    def parse_session(raw: str) -> WeChatSession | None:
        return _session_from_json(raw)


class WeChatWebSyncClient:
    def __init__(self, timeout_seconds: int = 15) -> None:
        self.http_client = httpx.Client(timeout=timeout_seconds, follow_redirects=True)

    def close(self) -> None:
        self.http_client.close()

    def refresh_contacts(self, sess: WeChatSession) -> list[OfficialAccount]:
        resp = self.http_client.get(
            f"{sess.base_uri}/cgi-bin/mmwebwx-bin/webwxgetcontact",
            params={"lang": "zh_CN", "pass_ticket": sess.pass_ticket, "skey": sess.skey, "r": int(time.time()), "seq": 0},
            headers={"User-Agent": "Mozilla/5.0"},
            cookies=sess.cookies,
        )
        resp.raise_for_status()
        payload = resp.json()
        ret = int(payload.get("BaseResponse", {}).get("Ret", -1))
        if ret != 0:
            raise RuntimeError(f"webwxgetcontact失败: ret={ret}")
        result: list[OfficialAccount] = []
        members = payload.get("MemberList") if isinstance(payload.get("MemberList"), list) else []
        for item in members:
            if not isinstance(item, dict):
                continue
            user_name = str(item.get("UserName") or "")
            verify_flag = int(item.get("VerifyFlag") or 0)
            nick_name = str(item.get("NickName") or "").strip()
            if user_name.startswith("gh_") or verify_flag > 0:
                result.append(
                    OfficialAccount(
                        user_name=user_name,
                        nick_name=nick_name or user_name,
                        verify_flag=verify_flag,
                    )
                )
        return result

    def sync(self, sess: WeChatSession) -> SyncBatch:
        retcode, selector, sync_host = self._synccheck(sess)
        if retcode != "0":
            if retcode in {"1100", "1101"}:
                raise RuntimeError("AUTH_REQUIRED: 微信登录态失效")
            raise RuntimeError(f"SYNC_RET_ERROR: retcode={retcode}")
        if selector == "0":
            return SyncBatch(
                retcode=retcode,
                selector=selector,
                messages=[],
                sync_key=sess.sync_key,
                next_sync_host=sync_host,
                created_at=_now(),
            )

        base_request = {
            "Uin": int(sess.wxuin),
            "Sid": sess.sid,
            "Skey": sess.skey,
            "DeviceID": sess.device_id,
        }
        resp = self.http_client.post(
            f"{sess.base_uri}/cgi-bin/mmwebwx-bin/webwxsync",
            params={"sid": sess.sid, "skey": sess.skey, "lang": "zh_CN", "pass_ticket": sess.pass_ticket},
            json={"BaseRequest": base_request, "SyncKey": sess.sync_key, "rr": ~int(time.time())},
            headers={"Content-Type": "application/json;charset=UTF-8", "User-Agent": "Mozilla/5.0"},
            cookies=sess.cookies,
        )
        resp.raise_for_status()
        payload = resp.json()
        ret = str(payload.get("BaseResponse", {}).get("Ret", "-1"))
        if ret != "0":
            raise RuntimeError(f"SYNC_RET_ERROR: webwxsync ret={ret}")
        sync_key = payload.get("SyncKey") if isinstance(payload.get("SyncKey"), dict) else sess.sync_key
        messages = payload.get("AddMsgList") if isinstance(payload.get("AddMsgList"), list) else []
        sess.sync_key = sync_key
        sess.sync_host = sync_host
        return SyncBatch(
            retcode=retcode,
            selector=selector,
            messages=messages,
            sync_key=sync_key,
            next_sync_host=sync_host,
            created_at=_now(),
        )

    def _synccheck(self, sess: WeChatSession) -> tuple[str, str, str]:
        sync_hosts = [sess.sync_host] + [item for item in _SYNC_HOSTS if item != sess.sync_host]
        for host in sync_hosts:
            try:
                params = {
                    "r": int(time.time() * 1000),
                    "skey": sess.skey,
                    "sid": sess.sid,
                    "uin": sess.wxuin,
                    "deviceid": sess.device_id,
                    "synckey": self._sync_key_to_str(sess.sync_key),
                    "_": int(time.time() * 1000),
                }
                resp = self.http_client.get(
                    f"https://{host}/cgi-bin/mmwebwx-bin/synccheck",
                    params=params,
                    headers={"User-Agent": "Mozilla/5.0"},
                    cookies=sess.cookies,
                )
                resp.raise_for_status()
                match = _SYNC_RE.search(resp.text)
                if not match:
                    continue
                return match.group(1), match.group(2), host
            except Exception:
                continue
        raise RuntimeError("SYNC_RET_ERROR: synccheck不可用")

    def _sync_key_to_str(self, sync_key: dict) -> str:
        lst = sync_key.get("List") if isinstance(sync_key, dict) else None
        if not isinstance(lst, list):
            return ""
        items: list[str] = []
        for item in lst:
            if not isinstance(item, dict):
                continue
            key = item.get("Key")
            val = item.get("Val")
            if key is None or val is None:
                continue
            items.append(f"{key}_{val}")
        return "|".join(items)
