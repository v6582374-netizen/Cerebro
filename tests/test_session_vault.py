from __future__ import annotations

from pathlib import Path

from wechat_agent.services.session_vault import SessionVault


def test_session_vault_file_backend_roundtrip(tmp_path: Path, monkeypatch):
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    vault = SessionVault(backend="file")

    assert vault.get("weread") is None
    vault.set("weread", "cookie=abc; token=1")
    assert vault.get("weread") == "cookie=abc; token=1"
    vault.delete("weread")
    assert vault.get("weread") is None
