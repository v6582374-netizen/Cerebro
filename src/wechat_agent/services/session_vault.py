from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess


def _default_session_store() -> Path:
    xdg_root = os.getenv("XDG_CONFIG_HOME", "").strip()
    if xdg_root:
        return Path(xdg_root).expanduser() / "wechat-agent" / "sessions.json"
    return Path.home() / ".config" / "wechat-agent" / "sessions.json"


class SessionVault:
    def __init__(self, backend: str = "auto", service_name: str = "wechat-agent") -> None:
        self.backend = backend.strip().lower() or "auto"
        self.service_name = service_name

    def set(self, provider: str, secret: str) -> None:
        if self._use_keychain():
            self._set_keychain(provider=provider, secret=secret)
            return
        self._set_file(provider=provider, secret=secret)

    def get(self, provider: str) -> str | None:
        if self._use_keychain():
            return self._get_keychain(provider=provider)
        return self._get_file(provider=provider)

    def delete(self, provider: str) -> None:
        if self._use_keychain():
            self._delete_keychain(provider=provider)
            return
        self._delete_file(provider=provider)

    def _use_keychain(self) -> bool:
        if self.backend == "keychain":
            return True
        if self.backend == "file":
            return False
        return os.uname().sysname.lower() == "darwin"

    def _set_keychain(self, provider: str, secret: str) -> None:
        account = f"{self.service_name}:{provider}"
        subprocess.run(
            [
                "security",
                "add-generic-password",
                "-a",
                account,
                "-s",
                self.service_name,
                "-w",
                secret,
                "-U",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def _get_keychain(self, provider: str) -> str | None:
        account = f"{self.service_name}:{provider}"
        proc = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                account,
                "-s",
                self.service_name,
                "-w",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return None
        value = (proc.stdout or "").strip()
        return value or None

    def _delete_keychain(self, provider: str) -> None:
        account = f"{self.service_name}:{provider}"
        subprocess.run(
            [
                "security",
                "delete-generic-password",
                "-a",
                account,
                "-s",
                self.service_name,
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    def _set_file(self, provider: str, secret: str) -> None:
        path = _default_session_store()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._load_file(path)
        payload[provider] = secret
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def _get_file(self, provider: str) -> str | None:
        path = _default_session_store()
        payload = self._load_file(path)
        value = str(payload.get(provider) or "").strip()
        return value or None

    def _delete_file(self, provider: str) -> None:
        path = _default_session_store()
        payload = self._load_file(path)
        if provider in payload:
            payload.pop(provider, None)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def _load_file(self, path: Path) -> dict[str, str]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            return {}
        return {}
