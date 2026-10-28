"""YouTube OAuth: client/token files, consent flow, and a usable API client.

The OAuth files live outside the database because they are credentials, not
application state. They default to ``$AUTOCLIP_HOME/secrets`` and can be moved
with environment variables for Docker mounts and tests.

Two pitfalls are handled deliberately:

* Google's loopback redirect must be ``localhost`` even when the local server
  binds ``0.0.0.0`` for Docker port mapping. ``run_local_server(host="0.0.0.0")``
  produces a redirect URI browsers and Google reject.
* A refresh token in Google's "Testing" mode expires after seven days. The
  resulting ``invalid_grant`` becomes a clear "run autoclip publish auth" hint,
  not an endless retry.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import paths

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

ENV_CLIENT_SECRETS = "AUTOCLIP_YOUTUBE_CLIENT_SECRETS_FILE"
ENV_TOKEN = "AUTOCLIP_YOUTUBE_TOKEN_FILE"

CLIENT_SECRETS_NAME = "youtube_client_secret.json"
TOKEN_NAME = "youtube_token.json"

#: Seconds the headless consent server waits for the browser callback.
CONSENT_TIMEOUT_S = 300.0


class PublishingAuthError(RuntimeError):
    """OAuth credentials are missing, invalid, or could not be refreshed."""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base}\n\n{self.hint}" if self.hint else base


def client_secrets_path() -> Path:
    override = os.environ.get(ENV_CLIENT_SECRETS)
    return Path(override).expanduser() if override else paths.secrets_dir() / CLIENT_SECRETS_NAME


def token_path() -> Path:
    override = os.environ.get(ENV_TOKEN)
    return Path(override).expanduser() if override else paths.secrets_dir() / TOKEN_NAME


# --------------------------------------------------------------------------
# Google imports kept behind helpers so tests (and the API) can run without the
# optional Google stack installed.
# --------------------------------------------------------------------------


def _credentials_class():
    from google.oauth2.credentials import Credentials

    return Credentials


def _request_class():
    from google.auth.transport.requests import Request

    return Request


def _flow_class():
    from google_auth_oauthlib.flow import InstalledAppFlow

    return InstalledAppFlow


def _build_youtube(credentials):
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def save_token(credentials) -> Path:
    """Write ``credentials.to_json()`` atomically with owner-only permissions."""
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = credentials.to_json()
    # Accept both the real Credentials object (str) and a dict from a test fake.
    text = payload if isinstance(payload, str) else json.dumps(payload)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    _restrict(tmp)
    os.replace(tmp, path)
    return path


def _restrict(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _missing_client_secrets_error() -> PublishingAuthError:
    return PublishingAuthError(
        f"YouTube OAuth client file not found: {client_secrets_path()}",
        hint=(
            "Create a Desktop OAuth client in Google Cloud, enable the YouTube "
            "Data API v3, then save the JSON there. See the README's "
            "“Publish to YouTube” section."
        ),
    )


def _missing_token_error() -> PublishingAuthError:
    return PublishingAuthError(
        f"YouTube token not found: {token_path()}",
        hint="Run `autoclip publish auth` once on a machine with a browser.",
    )


def build_client():
    """Load the cached token, refresh if needed, and return a YouTube client."""
    secrets_path = client_secrets_path()
    if not secrets_path.is_file():
        raise _missing_client_secrets_error()

    path = token_path()
    if not path.is_file():
        raise _missing_token_error()

    Credentials = _credentials_class()
    try:
        credentials = Credentials.from_authorized_user_file(str(path), SCOPES)
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        raise PublishingAuthError(
            f"The YouTube token file is unreadable: {path}",
            hint="Delete it and run `autoclip publish auth` again.",
        ) from exc

    if not credentials.valid:
        if credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(_request_class()())
            except Exception as exc:  # noqa: BLE001 - the Google stack raises many types
                raise _refresh_error(exc) from exc
            save_token(credentials)
        else:
            raise PublishingAuthError(
                "The YouTube token cannot be refreshed.",
                hint="Run `autoclip publish auth` to authorize again.",
            )

    return _build_youtube(credentials)


def _refresh_error(exc: Exception) -> PublishingAuthError:
    message = str(exc)
    lowered = message.lower()
    if "invalid_grant" in lowered or "expired" in lowered or "revoked" in lowered:
        return PublishingAuthError(
            "The YouTube token is no longer valid.",
            hint=(
                "Google Testing-mode refresh tokens expire after 7 days. Run "
                "`autoclip publish auth` again (or publish the OAuth app for a "
                "long-lived token)."
            ),
        )
    return PublishingAuthError(
        f"Could not refresh the YouTube token: {message}",
        hint="Check your network connection, then run `autoclip publish auth --check`.",
    )


def check_token() -> str:
    """Refresh and verify the token without uploading anything."""
    build_client()
    return "YouTube token OK"


def authorize(
    host: str = "localhost",
    port: int = 0,
    *,
    open_browser: bool = True,
    timeout_s: float = CONSENT_TIMEOUT_S,
) -> Path:
    """Run the OAuth consent flow and write the token file.

    With ``host="0.0.0.0"`` the local server binds all interfaces (for Docker
    port mapping) while the redirect URI stays ``http://localhost:<port>/``.
    """
    secrets_path = client_secrets_path()
    if not secrets_path.is_file():
        raise _missing_client_secrets_error()

    flow = _flow_class().from_client_secrets_file(str(secrets_path), SCOPES)

    if host in {"localhost", "127.0.0.1", "::1"}:
        credentials = flow.run_local_server(
            host=host,
            port=port,
            open_browser=open_browser,
            authorization_prompt_message="Open this URL to authorize AutoClip: {url}",
        )
        return save_token(credentials)

    if not port:
        port = _free_port()
    redirect_uri = f"http://localhost:{port}/"
    flow.redirect_uri = redirect_uri
    url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    print(f"Open this URL in a browser on the host:\n\n{url}\n")
    if open_browser:
        with contextlib.suppress(Exception):
            webbrowser.open(url)

    code = _wait_for_code(port, timeout_s)
    flow.fetch_token(code=code)
    return save_token(flow.credentials)


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("0.0.0.0", 0))
        return int(sock.getsockname()[1])


def _wait_for_code(port: int, timeout_s: float) -> str:
    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's contract
            query = parse_qs(urlparse(self.path).query)
            if "code" in query:
                captured["code"] = query["code"][0]
                body = b"<html><body>Authorization complete. You can close this tab.</body></html>"
                self.send_response(200)
            elif "error" in query:
                captured["error"] = query["error"][0]
                body = (
                    b"<html><body>Authorization failed. Check the AutoClip terminal.</body></html>"
                )
                self.send_response(400)
            else:
                body = b"not found"
                self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # noqa: A003 - silence the default log
            pass

    server = HTTPServer(("0.0.0.0", port), Handler)
    server.timeout = 1.0
    deadline = time.monotonic() + timeout_s
    try:
        while not captured and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if "error" in captured:
        raise PublishingAuthError(
            f"Google returned an authorization error: {captured['error']}",
            hint="Check the OAuth consent screen configuration and try again.",
        )
    if "code" not in captured:
        raise PublishingAuthError(
            "Timed out waiting for the Google authorization callback.",
            hint=(
                f"Make sure the browser can reach {redirect_uri_for(port)} and "
                "that port is published in Docker."
            ),
        )
    return captured["code"]


def redirect_uri_for(port: int) -> str:
    """The loopback redirect URI, never ``0.0.0.0``."""
    return f"http://localhost:{port}/"
