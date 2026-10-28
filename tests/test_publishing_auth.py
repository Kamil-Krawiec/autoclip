"""OAuth file handling, refresh, consent redirect, and clear error hints."""

from __future__ import annotations

import json
import socket
import stat
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from autoclip import paths
from autoclip.publishing import auth


@pytest.fixture
def client_secret_file(autoclip_home: Path) -> Path:
    paths.ensure_layout()
    path = auth.client_secrets_path()
    path.write_text("{}", encoding="utf-8")
    return path


class FakeCredentials:
    valid = False
    expired = True
    refresh_token = "refresh-token"

    def __init__(self) -> None:
        self.refreshed = False

    @classmethod
    def from_authorized_user_file(cls, path: str, scopes: list[str]):
        instance = cls()
        instance.path = path
        instance.scopes = scopes
        return instance

    def refresh(self, request) -> None:
        self.refreshed = True
        self.valid = True
        self.expired = False

    def to_json(self) -> str:
        return json.dumps({"token": "new", "refresh_token": self.refresh_token})


class ExpiredFakeCredentials(FakeCredentials):
    def refresh(self, request) -> None:
        raise Exception("invalid_grant: Token has been expired or revoked.")  # noqa: B017


class TestBuildClient:
    def test_missing_client_secret_names_the_exact_path(self, autoclip_home: Path) -> None:
        with pytest.raises(auth.PublishingAuthError) as caught:
            auth.build_client()

        assert str(auth.client_secrets_path()) in str(caught.value)
        assert "Google Cloud" in caught.value.hint

    def test_missing_token_says_run_auth(self, client_secret_file: Path) -> None:
        with pytest.raises(auth.PublishingAuthError) as caught:
            auth.build_client()

        assert "autoclip publish auth" in caught.value.hint

    def test_refresh_writes_a_private_token_atomically(
        self, client_secret_file: Path, monkeypatch
    ) -> None:
        token = auth.token_path()
        token.write_text('{"old": true}', encoding="utf-8")
        monkeypatch.setattr(auth, "_credentials_class", lambda: FakeCredentials)
        monkeypatch.setattr(auth, "_request_class", lambda: object)
        monkeypatch.setattr(auth, "_build_youtube", lambda credentials: "youtube-client")

        client = auth.build_client()

        assert client == "youtube-client"
        assert json.loads(token.read_text(encoding="utf-8"))["token"] == "new"
        assert stat.S_IMODE(token.stat().st_mode) == 0o600
        assert not token.with_suffix(".json.tmp").exists()

    def test_invalid_grant_explains_seven_day_testing_tokens(
        self, client_secret_file: Path, monkeypatch
    ) -> None:
        auth.token_path().write_text("{}", encoding="utf-8")
        monkeypatch.setattr(auth, "_credentials_class", lambda: ExpiredFakeCredentials)
        monkeypatch.setattr(auth, "_request_class", lambda: object)

        with pytest.raises(auth.PublishingAuthError) as caught:
            auth.build_client()

        assert "7 days" in caught.value.hint
        assert "autoclip publish auth" in caught.value.hint

    def test_unreadable_token_is_reported(self, client_secret_file: Path, monkeypatch) -> None:
        auth.token_path().write_text("not json", encoding="utf-8")

        class Broken:
            @classmethod
            def from_authorized_user_file(cls, path, scopes):
                raise ValueError("bad json")

        monkeypatch.setattr(auth, "_credentials_class", lambda: Broken)

        with pytest.raises(auth.PublishingAuthError, match="unreadable"):
            auth.build_client()

    def test_check_token_reports_ok_without_uploading(
        self, client_secret_file: Path, monkeypatch
    ) -> None:
        auth.token_path().write_text("{}", encoding="utf-8")
        creds = FakeCredentials()
        creds.valid = True
        monkeypatch.setattr(
            auth,
            "_credentials_class",
            lambda: type(
                "C", (), {"from_authorized_user_file": classmethod(lambda cls, p, s: creds)}
            ),
        )
        monkeypatch.setattr(auth, "_build_youtube", lambda credentials: object())

        assert auth.check_token() == "YouTube token OK"


class TestPaths:
    def test_env_overrides(self, autoclip_home: Path, monkeypatch) -> None:
        monkeypatch.setenv(auth.ENV_CLIENT_SECRETS, "/tmp/client.json")
        monkeypatch.setenv(auth.ENV_TOKEN, "/tmp/token.json")

        assert auth.client_secrets_path() == Path("/tmp/client.json")
        assert auth.token_path() == Path("/tmp/token.json")

    def test_defaults_live_under_secrets(self, autoclip_home: Path) -> None:
        assert auth.client_secrets_path() == paths.secrets_dir() / auth.CLIENT_SECRETS_NAME
        assert auth.token_path() == paths.secrets_dir() / auth.TOKEN_NAME

    def test_redirect_uri_is_localhost_not_wildcard(self) -> None:
        assert auth.redirect_uri_for(8765) == "http://localhost:8765/"
        assert "0.0.0.0" not in auth.redirect_uri_for(8765)


class TestConsentCallback:
    @staticmethod
    def _wait_until_listening(port: int) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    return
            time.sleep(0.02)
        raise AssertionError("the consent server never started listening")

    def test_captures_the_code_from_a_real_http_request(self) -> None:
        port = auth._free_port()
        result: dict[str, str] = {}

        def run() -> None:
            result["code"] = auth._wait_for_code(port, timeout_s=5)

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self._wait_until_listening(port)
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/?code=abc123", timeout=5
            ) as response:
                assert response.status == 200
        finally:
            thread.join(timeout=5)

        assert result["code"] == "abc123"

    def test_google_error_is_reported(self) -> None:
        port = auth._free_port()
        result: dict[str, object] = {}

        def run() -> None:
            try:
                auth._wait_for_code(port, timeout_s=5)
            except auth.PublishingAuthError as exc:
                result["error"] = exc

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self._wait_until_listening(port)
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(f"http://127.0.0.1:{port}/?error=access_denied", timeout=5)
        finally:
            thread.join(timeout=5)

        assert isinstance(result.get("error"), auth.PublishingAuthError)


class TestAuthorizeDockerRedirect:
    def test_binds_wildcard_but_redirects_to_localhost(
        self, client_secret_file: Path, monkeypatch, tmp_path: Path
    ) -> None:
        captured: dict[str, object] = {}

        class FakeFlow:
            def __init__(self) -> None:
                self.redirect_uri = ""
                self.credentials = FakeCredentials()

            @classmethod
            def from_client_secrets_file(cls, path: str, scopes: list[str]):
                return cls()

            def authorization_url(self, **kwargs):
                captured["redirect_uri"] = self.redirect_uri
                captured["kwargs"] = kwargs
                return ("https://consent.invalid", "state")

            def fetch_token(self, code: str) -> None:
                captured["code"] = code

        monkeypatch.setattr(auth, "_flow_class", lambda: FakeFlow)
        monkeypatch.setattr(auth, "_wait_for_code", lambda port, timeout: "the-code")

        written = auth.authorize(host="0.0.0.0", port=8765, open_browser=False)

        assert captured["redirect_uri"] == "http://localhost:8765/"
        assert captured["kwargs"] == {
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
        assert captured["code"] == "the-code"
        assert written == auth.token_path()
        assert written.exists()
