"""Google OAuth 2.0 for a desktop app, implemented with the standard library.

This deliberately avoids ``google-auth`` / ``google-api-python-client``. The only
third-party dependency is ``requests``, which Anki already bundles.

The flow is the one Google documents for *Desktop app* clients
(https://developers.google.com/identity/protocols/oauth2/native-app):

1. Generate a PKCE ``code_verifier`` and its S256 ``code_challenge``.
2. Prepare the consent URL with ``access_type=offline`` so that a refresh
   token is issued, and a random ``state`` for CSRF protection.
3. Catch the redirect on a temporary ``http://127.0.0.1:<port>`` loopback
   server, validate ``state``, and stop.
4. Exchange the returned code for tokens.
5. Later, silently exchange the refresh token when the access token expires.

Opening the consent URL is deliberately *not* done here. :class:`ConsentSession`
only prepares the URL and starts listening; the UI shows it and lets the user
open it in a browser or copy it, so nothing navigates without being asked.

Only the refresh token is written to disk. The access token is short-lived and
is kept in memory for the lifetime of the Anki session.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import string
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .config import SCOPES, clear_tokens, load_tokens, save_tokens

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

#: Unreserved characters permitted in a PKCE verifier (RFC 7636 section 4.1).
_VERIFIER_ALPHABET = string.ascii_letters + string.digits + "-._~"

#: Refresh this many seconds before actual expiry so a request is never sent
#: with a token that dies in flight.
_EXPIRY_SKEW_SECONDS = 60


class AuthError(RuntimeError):
    """Raised when authentication fails or the user declines consent."""


class AuthCancelled(AuthError):
    """Raised when the user cancels sign-in before it completes.

    Distinct from :class:`AuthError` so the caller can stay silent instead of
    showing an error dialog for a deliberate, user-initiated cancellation.
    """


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def generate_code_verifier(length: int = 64) -> str:
    """Return a high-entropy PKCE code verifier.

    RFC 7636 requires 43 to 128 characters drawn from the unreserved set.
    """
    if not 43 <= length <= 128:
        raise ValueError("code verifier length must be between 43 and 128")
    return "".join(secrets.choice(_VERIFIER_ALPHABET) for _ in range(length))


def code_challenge_from_verifier(verifier: str) -> str:
    """Return the BASE64URL(SHA256(verifier)) challenge, without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_auth_url(
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
) -> str:
    """Build the consent URL the user's browser is sent to."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        # Ensure a refresh token is returned even if an old grant exists.
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Loopback redirect receiver
# ---------------------------------------------------------------------------

_SUCCESS_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Anki Sheets Sync</title></head>
<body style="font-family: sans-serif; padding: 3rem; text-align: center;">
<h2>Connected</h2>
<p>You can close this tab and return to Anki.</p>
</body></html>"""

_ERROR_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Anki Sheets Sync</title></head>
<body style="font-family: sans-serif; padding: 3rem; text-align: center;">
<h2>Not connected</h2>
<p>{error}</p>
<p>You can close this tab and return to Anki.</p>
</body></html>"""


class _CallbackHandler(BaseHTTPRequestHandler):
    """Captures a single OAuth redirect, then the caller stops the server.

    The handler validates ``state`` itself rather than leaving it to
    :func:`wait_for_callback`, because it is the only place that knows what to
    tell the user. A request whose state does not match must render “Not
    connected”, not “Connected” — otherwise a forged or stale redirect would
    show the user a success page for a sign-in that is about to be rejected.
    """

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        query = parse_qs(urlparse(self.path).query)
        code = query.get("code", [""])[0]
        error = query.get("error", [""])[0]
        state = query.get("state", [""])[0]

        if error:
            self.server.captured = {"error": error}  # type: ignore[attr-defined]
            body: str = _ERROR_HTML.format(error=error)
            status = 400
        elif not code:
            self.server.captured = {"error": "no authorization code was returned"}  # type: ignore[attr-defined]
            body = _ERROR_HTML.format(error="no authorization code was returned")
            status = 400
        elif state != getattr(self.server, "expected_state", ""):
            self.server.captured = {"error": "OAuth state mismatch; the response was rejected."}  # type: ignore[attr-defined]
            body = _ERROR_HTML.format(
                error="This sign-in link did not match the request Anki made. "
                "Please start the connection again from Anki."
            )
            status = 400
        else:
            self.server.captured = {"code": code, "state": state}  # type: ignore[attr-defined]
            body = _SUCCESS_HTML
            status = 200

        payload = body.encode("utf8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress the default stderr access log."""


def start_callback_server(expected_state: str) -> tuple[HTTPServer, str]:
    """Start the loopback server on an ephemeral port.

    Returns the server and the redirect URI to send to Google. Attach the
    authorization code to ``server.captured`` once the browser redirects back.
    The caller owns shutting the server down.

    ``expected_state`` is handed to the request handler so it can reject a
    redirect whose ``state`` does not match and render the correct page.
    """
    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.captured = {}  # type: ignore[attr-defined]
    server.expected_state = expected_state  # type: ignore[attr-defined]
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


def wait_for_callback(
    server: HTTPServer,
    expected_state: str,
    timeout: float = 300.0,
    cancelled: threading.Event | None = None,
) -> str:
    """Block until the browser redirects back, then return the authorization code.

    Validates ``state`` to guard against cross-site request forgery. Must not be
    called on the Qt main thread.

    If ``cancelled`` is supplied, setting it aborts the wait promptly and raises
    :class:`AuthCancelled`. The HTTP server is always shut down on the way out.
    """
    deadline = time.monotonic() + timeout
    try:
        while True:
            captured: dict[str, str] = getattr(server, "captured", {})
            if captured:
                break
            if cancelled is not None and cancelled.is_set():
                raise AuthCancelled()
            if time.monotonic() >= deadline:
                raise AuthError(
                    "Timed out waiting for the browser to complete sign-in."
                )
            time.sleep(0.1)

        if "error" in captured:
            raise AuthError(f"Google returned an error: {captured['error']}")
        if not captured.get("code"):
            raise AuthError("Google did not return an authorization code.")
        if captured.get("state") != expected_state:
            raise AuthError("OAuth state mismatch; the response was rejected.")
        return captured["code"]
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Token endpoints
# ---------------------------------------------------------------------------


def _json_or_error(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        raise AuthError(
            f"Google returned a non-JSON response (HTTP {response.status_code})."
        ) from None
    if not isinstance(payload, dict):
        raise AuthError("Google returned an unexpected response shape.")
    return payload


def exchange_code_for_tokens(
    client_id: str,
    client_secret: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> dict[str, Any]:
    """Trade the authorization code for an access token and a refresh token."""
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    try:
        response = requests.post(TOKEN_ENDPOINT, data=data, timeout=30)
    except requests.RequestException as exc:
        raise AuthError(f"Could not reach Google: {exc}") from exc

    payload = _json_or_error(response)
    if "error" in payload:
        detail = payload.get("error_description", payload["error"])
        raise AuthError(f"Token exchange failed: {detail}")
    return payload


def refresh_access_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> dict[str, Any]:
    """Obtain a new short-lived access token using a refresh token."""
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    try:
        response = requests.post(TOKEN_ENDPOINT, data=data, timeout=30)
    except requests.RequestException as exc:
        raise AuthError(f"Could not reach Google: {exc}") from exc

    payload = _json_or_error(response)
    if "error" in payload:
        detail = payload.get("error_description", payload["error"])
        raise AuthError(f"Re-authentication required ({detail}).")
    return payload


def revoke(refresh_token: str) -> None:
    """Ask Google to revoke a refresh token.

    Best effort. A failure here must not block disconnecting, because the token
    is discarded locally either way.
    """
    try:
        requests.post(REVOKE_ENDPOINT, data={"token": refresh_token}, timeout=30)
    except requests.RequestException:
        pass


# ---------------------------------------------------------------------------
# Token persistence and the in-memory access token cache
# ---------------------------------------------------------------------------


def store_tokens(tokens: dict[str, Any], access_token: str) -> None:
    """Persist the refresh token and cache the access token in memory."""
    save_tokens({"refresh_token": tokens.get("refresh_token", "")})
    set_cached_access_token(access_token, int(tokens.get("expires_in", 3600)))


_cached_access_token: str = ""
_cached_expiry: float = 0.0


def set_cached_access_token(access_token: str, expires_in: int) -> None:
    """Cache an access token in memory for this Anki session."""
    global _cached_access_token, _cached_expiry
    _cached_access_token = access_token
    _cached_expiry = time.time() + expires_in


def is_connected() -> bool:
    """Whether a refresh token is stored on disk."""
    return bool(load_tokens().get("refresh_token"))


def get_access_token(client_id: str, client_secret: str) -> str:
    """Return a valid access token, refreshing silently when needed.

    Must not be called on the Qt main thread, because it may perform network
    requests.
    """
    global _cached_access_token, _cached_expiry

    if _cached_access_token and _cached_expiry - _EXPIRY_SKEW_SECONDS > time.time():
        return _cached_access_token

    refresh_token = load_tokens().get("refresh_token", "")
    if not refresh_token:
        raise AuthError("Not connected to Google. Connect first.")

    payload = refresh_access_token(client_id, client_secret, refresh_token)
    access_token = payload.get("access_token", "")
    if not access_token:
        raise AuthError("Google did not return an access token.")

    _cached_access_token = access_token
    _cached_expiry = time.time() + int(payload.get("expires_in", 3600))
    return access_token


def disconnect() -> None:
    """Revoke the token with Google and forget it locally."""
    global _cached_access_token, _cached_expiry
    _cached_access_token = ""
    _cached_expiry = 0.0

    refresh_token = load_tokens().get("refresh_token", "")
    if refresh_token:
        revoke(refresh_token)
    clear_tokens()


class ConsentSession:
    """A prepared-but-not-yet-completed OAuth consent flow.

    Splitting *preparation* from *waiting* is what lets the UI show the consent
    URL before anything is opened. The loopback server is already listening by
    the time the URL exists, so a redirect that arrives immediately is still
    captured — the user is never in a state where they can redirect before the
    server is ready.

    :meth:`run` blocks and must not be called on the Qt main thread. It may be
    called at most once. :meth:`abort` releases the loopback server and is safe
    to call from any thread, any number of times.
    """

    def __init__(self, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._verifier = generate_code_verifier()
        self._state = secrets.token_urlsafe(24)
        # Ownership of the server is handed off atomically: exactly one of
        # run()/abort() claims it, so it is shut down exactly once even if both
        # are called (e.g. abort() after run() has already taken it).
        self._lock = threading.Lock()
        # Explicitly optional: the annotation is what lets _claim_server() set
        # it to None to transfer ownership. Without it the unpacking below would
        # infer a non-optional type and reject that assignment.
        self._server: HTTPServer | None
        self._server, self._redirect_uri = start_callback_server(self._state)
        self.url = build_auth_url(
            client_id,
            self._redirect_uri,
            code_challenge_from_verifier(self._verifier),
            self._state,
        )

    def _claim_server(self) -> HTTPServer | None:
        with self._lock:
            server, self._server = self._server, None
            return server

    def run(self, cancelled: threading.Event | None = None) -> None:
        """Wait for the redirect and store the tokens.

        Raises :class:`AuthCancelled` if ``cancelled`` is set while waiting,
        otherwise :class:`AuthError` on failure. Ownership of the loopback server
        passes to :func:`wait_for_callback`, which always shuts it down.
        """
        server = self._claim_server()
        if server is None:
            raise AuthCancelled()  # already aborted; nothing left to wait on

        code = wait_for_callback(server, self._state, cancelled=cancelled)
        tokens = exchange_code_for_tokens(
            self._client_id,
            self._client_secret,
            code,
            self._verifier,
            self._redirect_uri,
        )

        if not tokens.get("refresh_token"):
            raise AuthError("Google did not issue a refresh token. Please try again.")

        store_tokens(tokens, tokens.get("access_token", ""))

    def abort(self) -> None:
        """Release the loopback server without completing the flow.

        Safe to call after :meth:`run` has finished, and safe to call from the
        Qt main thread while a background :meth:`run` is waiting on the
        redirect. In the latter case the server is already owned by
        :func:`wait_for_callback`, which shuts it down as soon as it notices
        ``cancelled`` was set.
        """
        server = self._claim_server()
        if server is None:
            return
        server.shutdown()
        server.server_close()
