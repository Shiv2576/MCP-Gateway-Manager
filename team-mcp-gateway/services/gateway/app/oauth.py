"""OAuth for /mcp: the gateway is its own authorization server, so an MCP
client (e.g. a Claude custom connector) can connect with a browser login
instead of an API key.

The SDK serves /register, /authorize, /token, /revoke and the metadata
documents and does the protocol checks (PKCE, redirect URI, expiry). This
module stores clients, codes and tokens, and serves the login page. Tokens
belong to a gateway user, so that user's server grants apply as with API keys.
Only hashes of codes and tokens are stored.
"""

from __future__ import annotations

import html
import json
import secrets
import time
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from . import api, db as store

LOGIN_TTL_S = 600
CODE_TTL_S = 300
ACCESS_TTL_S = 3600
REFRESH_TTL_S = 30 * 24 * 3600
LOOPBACK = ("localhost", "127.0.0.1", "::1")


def user_for_oauth_token(conn: store.Db, token: str, resource: str) -> dict | None:
    """The user an access token belongs to, if it is live and was issued for this gateway."""
    return conn.execute(
        "SELECT u.* FROM oauth_tokens t JOIN users u ON u.id = t.user_id"
        " WHERE t.token_hash = ? AND t.kind = 'access' AND t.expires_at > ?"
        " AND (t.resource IS NULL OR rtrim(t.resource, '/') = ?)",
        (store.sha256(token), time.time(), resource.rstrip("/")),
    ).fetchone()


class Provider:
    """OAuthAuthorizationServerProvider backed by the gateway's database."""

    def __init__(self, conn: store.Db, public_url: str, redirect_uris: tuple[str, ...]):
        self.conn, self.public_url, self.redirect_uris = conn, public_url.rstrip("/"), redirect_uris

    # ------------------------------------------------------------ clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = self.conn.execute("SELECT info FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row["info"]) if row else None

    def redirect_allowed(self, uri: str) -> bool:
        parts = urlsplit(uri)
        # Loopback is for local clients (Claude Code, MCP Inspector): only this machine can receive it.
        return uri in self.redirect_uris or (parts.scheme == "http" and parts.hostname in LOOPBACK)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # Anyone can register, so where codes may be sent is what keeps them safe.
        for uri in client_info.redirect_uris or []:
            if not self.redirect_allowed(str(uri)):
                raise RegistrationError("invalid_redirect_uri", f"{uri} is not an allowed redirect URI")
        # shortcut: registrations are never purged; add a cleanup of clients
        # with no tokens if the table ever grows large.
        self.conn.execute(
            "INSERT INTO oauth_clients (client_id, info, created_at) VALUES (?, ?, ?)",
            (client_info.client_id, client_info.model_dump_json(), time.time()),
        )

    # -------------------------------------------------------------- codes

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Park the request and send the browser to the login page."""
        login_id = secrets.token_urlsafe(32)
        now = time.time()
        self.conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (now,))
        self.conn.execute(
            "INSERT INTO oauth_codes (code_hash, client_id, params, expires_at) VALUES (?, ?, ?, ?)",
            (store.sha256(login_id), client.client_id, params.model_dump_json(), now + LOGIN_TTL_S),
        )
        return f"{self.public_url}/oauth/login?id={login_id}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        row = self.conn.execute(
            "SELECT * FROM oauth_codes WHERE code_hash = ? AND client_id = ? AND user_id IS NOT NULL",
            (store.sha256(authorization_code), client.client_id),
        ).fetchone()
        if row is None:
            return None
        params = AuthorizationParams.model_validate_json(row["params"])
        return AuthorizationCode(
            code=authorization_code,
            scopes=params.scopes or [],
            expires_at=row["expires_at"],
            client_id=row["client_id"],
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=str(row["user_id"]),
        )

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        cur = self.conn.execute("DELETE FROM oauth_codes WHERE code_hash = ?", (store.sha256(authorization_code.code),))
        if not cur.rowcount:  # already used by a concurrent request
            raise TokenError("invalid_grant", "authorization code was already used")
        return self._issue(int(authorization_code.subject), client.client_id,
                           authorization_code.scopes, authorization_code.resource)

    # ------------------------------------------------------------- tokens

    def _issue(self, user_id: int, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh = "mcpo_" + secrets.token_urlsafe(32), "mcpr_" + secrets.token_urlsafe(32)
        now = time.time()
        self.conn.execute("DELETE FROM oauth_tokens WHERE expires_at < ?", (now,))
        self.conn.executemany(
            "INSERT INTO oauth_tokens (token_hash, kind, user_id, client_id, scopes, resource, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (store.sha256(access), "access", user_id, client_id, " ".join(scopes), resource, now, now + ACCESS_TTL_S),
                (store.sha256(refresh), "refresh", user_id, client_id, " ".join(scopes), resource, now, now + REFRESH_TTL_S),
            ],
        )
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL_S, refresh_token=refresh,
                          scope=" ".join(scopes) or None)

    def _token(self, token: str, kind: str):
        return self.conn.execute(
            "SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = ? AND expires_at > ?",
            (store.sha256(token), kind, time.time()),
        ).fetchone()

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        row = self._token(refresh_token, "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"], scopes=row["scopes"].split(),
                            expires_at=int(row["expires_at"]), resource=row["resource"], subject=str(row["user_id"]))

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        # Rotate: the old refresh token works once.
        cur = self.conn.execute("DELETE FROM oauth_tokens WHERE token_hash = ?", (store.sha256(refresh_token.token),))
        if not cur.rowcount:
            raise TokenError("invalid_grant", "refresh token was already used")
        return self._issue(int(refresh_token.subject), client.client_id, scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self._token(token, "access")
        if row is None:
            return None
        return AccessToken(token=token, client_id=row["client_id"], scopes=row["scopes"].split(),
                           expires_at=int(row["expires_at"]), resource=row["resource"], subject=str(row["user_id"]))

    async def revoke_token(self, token) -> None:
        """Ends this app's connection for this user: every token, access and refresh."""
        self.conn.execute("DELETE FROM oauth_tokens WHERE user_id = ? AND client_id = ?",
                          (int(token.subject), token.client_id))


# ---------------------------------------------------------------- login page

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in · MCP gateway</title>
<style>
  body {{ font: 15px/1.5 system-ui, sans-serif; background: #f6f7f9; color: #1c1f24; margin: 0;
         display: grid; place-items: center; min-height: 100vh; padding: 16px; box-sizing: border-box; }}
  main {{ background: #fff; border: 1px solid #dde1e6; border-radius: 10px; padding: 24px; width: 100%; max-width: 360px; }}
  h1 {{ font-size: 18px; margin: 0 0 8px; }}
  label {{ display: block; margin-top: 12px; font-weight: 500; }}
  input {{ width: 100%; box-sizing: border-box; padding: 8px; margin-top: 4px; font: inherit;
          border: 1px solid #c4c9d0; border-radius: 6px; }}
  button {{ margin-top: 16px; width: 100%; padding: 9px; font: inherit; font-weight: 600; border: 0;
           border-radius: 6px; background: #2f5bea; color: #fff; cursor: pointer; }}
  .muted {{ color: #5c6470; font-size: 13px; }} .error {{ color: #b42318; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #16181c; color: #e6e8eb; }} main {{ background: #1f2226; border-color: #33373d; }}
    input {{ background: #16181c; color: inherit; border-color: #3d424a; }} .muted {{ color: #9aa2ad; }}
    .error {{ color: #f97066; }}
  }}
</style></head>
<body><main>{body}</main></body></html>"""

HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(PAGE.format(body=body), status_code=status, headers=HEADERS)


def _pending(conn: store.Db, login_id: str):
    return conn.execute(
        "SELECT c.*, k.info FROM oauth_codes c JOIN oauth_clients k ON k.client_id = c.client_id"
        " WHERE c.code_hash = ? AND c.user_id IS NULL AND c.expires_at > ?",
        (store.sha256(login_id), time.time()),
    ).fetchone()


async def login(request: Request) -> Response:
    conn = request.app.state.db
    form = await request.form() if request.method == "POST" else {}
    login_id = str(form.get("id") or request.query_params.get("id", ""))
    row = _pending(conn, login_id) if login_id else None
    if row is None:
        # No redirect: without a valid request there is no trusted place to send the browser.
        return _page("<h1>Sign-in link expired</h1><p>Start again from the app you were connecting.</p>", 400)
    client = OAuthClientInformationFull.model_validate_json(row["info"])
    params = AuthorizationParams.model_validate_json(row["params"])

    error, status = "", 200
    if request.method == "POST":
        try:
            user = api.check_password(request, str(form.get("username", "")).strip(), str(form.get("password", "")))
        except api.HTTPError as e:
            error, status = f'<p class="error" role="alert">{html.escape(e.message.capitalize())}.</p>', e.status
        else:
            code = secrets.token_urlsafe(32)
            cur = conn.execute(
                "UPDATE oauth_codes SET code_hash = ?, user_id = ?, expires_at = ?"
                " WHERE code_hash = ? AND user_id IS NULL",
                (store.sha256(code), user["id"], time.time() + CODE_TTL_S, store.sha256(login_id)),
            )
            if not cur.rowcount:  # the same form was submitted twice
                return _page("<h1>Sign-in link expired</h1><p>Start again from the app you were connecting.</p>", 400)
            target = construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)
            return RedirectResponse(target, status_code=303, headers=HEADERS)

    name = html.escape(client.client_name or "An app")
    host = html.escape(urlsplit(str(params.redirect_uri)).netloc)
    return _page(
        f"""<h1>Sign in to the MCP gateway</h1>
<p><strong>{name}</strong> wants to use the gateway's tools as you.
After you sign in it gets access to the servers you are allowed to use.</p>
<p class="muted">You will be sent back to <strong>{host}</strong>.</p>
{error}
<form method="post" action="/oauth/login">
  <input type="hidden" name="id" value="{html.escape(login_id)}">
  <label>Username <input name="username" autocomplete="username" required autofocus></label>
  <label>Password <input name="password" type="password" autocomplete="current-password" required></label>
  <button>Sign in and allow</button>
</form>""",
        status,
    )


routes = [Route("/oauth/login", login, methods=["GET", "POST"])]
