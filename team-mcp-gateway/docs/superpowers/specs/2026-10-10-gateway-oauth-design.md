# OAuth on the gateway: design

Date: 2026-10-10. Status: draft for review.

Let MCP clients connect to the gateway's `/mcp` with a browser login (the
OAuth flow from the MCP authorization spec), so team members can add the
gateway to Claude as a custom connector. API keys keep working.

---

## 0. Agreed requirements

From the conversation:

- The gateway is **its own OAuth authorization server** (approach 1). No
  external identity provider.
- Logins use the **existing gateway users** (username + password). The OAuth
  token belongs to that user, so per-user server grants, the rate limit and the
  audit log apply unchanged.
- **API keys stay.** `/mcp` accepts either an API key or an OAuth access token.

Assumptions (not stated, chosen here):

- OAuth is **off unless `PUBLIC_URL` is set** (e.g. `https://gateway.example.com`).
  Local dev and the existing tests run as today. Claude reaches the gateway
  from Anthropic's servers, so a public HTTPS URL (real ingress with TLS, or a
  cloudflared/ngrok tunnel for testing) is a hard prerequisite.
- **No scopes.** A token grants what its user may do; grants already limit that.
- Lifetimes: auth code 5 min, access token 1 h, refresh token 30 days,
  rotated on each use.
- Single gateway replica (SQLite already requires this), so OAuth state lives in
  the same SQLite file.

Out of scope: SSO/IdP login, per-tool scopes, OAuth to upstream servers,
Client ID Metadata Documents (only if Claude requires them; see §7).

---

## 1. What the SDK already gives us

`mcp` 2.3.0's `Server.streamable_http_app(auth=AuthSettings(...),
auth_server_provider=provider)` mounts, with no code of ours:

| Path | Role |
|---|---|
| `/.well-known/oauth-authorization-server` | RFC 8414 metadata |
| `/.well-known/oauth-protected-resource/mcp` | RFC 9728 metadata, points at the issuer |
| `/register` | RFC 7591 dynamic client registration |
| `/authorize` | validates the request (PKCE S256 required), then calls `provider.authorize()` |
| `/token` | code and refresh exchanges (calls the provider) |
| `/revoke` | token revocation |

We pass **no `token_verifier`**, so the SDK does not wrap `/mcp`; our existing
guard keeps doing that (§3). `validate_token_resource=False` is set
explicitly; the guard checks the resource itself.

---

## 2. Components

### `db.py`: three tables

```sql
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id  TEXT PRIMARY KEY,
    info       TEXT NOT NULL,      -- OAuthClientInformationFull as JSON
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_codes (     -- pending logins and issued codes
    code_hash  TEXT PRIMARY KEY,
    client_id  TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE, -- NULL until login
    params     TEXT NOT NULL,      -- redirect_uri, code_challenge, state, resource
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id  TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    resource   TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
```

Only hashes are stored (`store.sha256`), like sessions and API keys.
Deleting a user deletes their tokens via the cascade.

### New `app/oauth.py`

- `GatewayOAuthProvider(conn, settings)`, implementing
  `OAuthAuthorizationServerProvider`:
  - `register_client`: rejects any redirect URI that is not in
    `OAUTH_REDIRECT_URIS` and not a loopback `http://localhost|127.0.0.1:*`
    URI. Default `OAUTH_REDIRECT_URIS`: Claude's callback URL(s) (§7).
  - `authorize`: stores a pending row in `oauth_codes` (random id,
    `user_id` NULL) and returns `PUBLIC_URL/oauth/login?req=<id>`.
  - `load_authorization_code` / `exchange_authorization_code`: only rows
    with a `user_id`, single use (row deleted on exchange); issues an access +
    refresh token pair. `subject` = user id.
  - `load_refresh_token` / `exchange_refresh_token`: rotates (old refresh
    token deleted, new pair issued).
  - `load_access_token`: lookup by hash, not expired.
  - `revoke_token`: deletes all tokens of that user+client pair.
- `GET/POST /oauth/login`: a small server-rendered HTML page on the gateway
  host (the dashboard lives on another host, so its session cookie is not
  usable here). Shows "**<client_name>** wants to use the gateway's tools as you.
  It will return to **<redirect host>**." plus username/password. On success:
  fills `user_id` on the pending row, redirects to
  `redirect_uri?code=…&state=…`. The `req` id is random and single-use, so it
  doubles as the CSRF token. Wrong password: same page with an error, using
  the same password check as `/api/login`. `X-Frame-Options: DENY`.
- `user_for_oauth_token(conn, token)` → user row or None (checks kind,
  expiry and that `resource`, when present, is `PUBLIC_URL/mcp`).

### `main.py`

- `Settings`: add `public_url` (`PUBLIC_URL`) and `oauth_redirect_uris`.
- When `public_url` is set: pass `auth=AuthSettings(issuer_url=public_url,
  resource_server_url=public_url + "/mcp", client_registration_options=
  ClientRegistrationOptions(enabled=True), revocation_options=
  RevocationOptions(enabled=True), validate_token_resource=False)` and the
  provider to `streamable_http_app`, plus the `/oauth/login` routes.
- `ApiKeyGuard` → accepts `api.user_for_api_key(...) or
  oauth.user_for_oauth_token(...)`. Its 401 adds
  `resource_metadata="<PUBLIC_URL>/.well-known/oauth-protected-resource/mcp"`
  to `WWW-Authenticate` when OAuth is on; that header is how clients discover
  the login.

### Dashboard: Keys tab

A "Connected apps" section under the API keys: one row per (user, client)
with an active refresh token: client name, user (admins see everyone, as
with keys), connected at, **Disconnect** button. Backed by
`GET /api/oauth/connections` and `DELETE /api/oauth/connections/{client_id}`
(own connections only for members, any for admins).

### Helm

`charts/gateway/values.yaml`: `publicUrl: ""` and `oauthRedirectUris`,
passed as env. The existing ingress already routes `/` (so `/.well-known/*`,
`/register`, `/oauth/*`) to the gateway; TLS is the deployer's concern.

---

## 3. Data flow (Claude custom connector)

1. Member adds `https://<PUBLIC_URL>/mcp` in Claude.
2. Claude calls `/mcp` → 401 with `resource_metadata` → reads both metadata
   documents → `POST /register` (redirect URI checked against the allowlist).
3. Browser opens `/authorize` (PKCE) → `/oauth/login?req=…` → member logs in.
4. Redirect to Claude's callback with the code → Claude calls `/token` → gets
   access + refresh tokens.
5. Claude calls `/mcp` with `Bearer <access token>` → guard resolves the user →
   `tools/list` shows only that user's granted servers.
6. After 1 h Claude refreshes. Disconnect in the dashboard or deleting the
   user ends access on the next call.

---

## 4. Error handling

- Unknown/expired/used code, bad PKCE verifier, unknown client → SDK returns
  the RFC error (`invalid_grant`, `invalid_client`); provider methods return
  None or raise `TokenError`/`AuthorizeError`.
- Disallowed redirect URI at registration → `RegistrationError
  ("invalid_redirect_uri")`.
- Expired or unknown pending `req` on the login page → "This sign-in link has
  expired, start again from your app." No redirect (we cannot trust where to).
- Expired rows are purged opportunistically on each insert, like sessions.

---

## 5. Testing

New `services/gateway/tests/test_oauth.py`, against the ASGI app with
`PUBLIC_URL` set, driving the real HTTP flow:

- full flow: register → authorize → login POST → code → token (PKCE) →
  `/mcp` `tools/list` returns only granted servers;
- refresh rotates (old refresh token rejected afterwards);
- revoke / dashboard disconnect → `/mcp` 401;
- wrong password, reused code, wrong PKCE verifier all fail;
- non-allowlisted redirect URI rejected at `/register`;
- API key still works; 401 carries `resource_metadata`;
- with `PUBLIC_URL` unset, `/register` is 404 and API keys work as today.

Manual check: tunnel to the k3d gateway, add it as a custom connector in
Claude, log in as `bob`, confirm only bob's servers appear.

---

## 6. Files touched

`services/gateway/app/{db.py, main.py, api.py, oauth.py (new)}`,
`services/gateway/tests/test_oauth.py (new)`,
`services/dashboard/src/Admin.jsx` (Keys tab section),
`charts/gateway/{values.yaml, templates/deployment.yaml}`,
`deploy/gateway-values-local.yaml`, `README.md`.

---

## 7. To verify before building

- Claude's exact OAuth callback URL(s) for custom connectors (expected
  `https://claude.ai/api/mcp/auth_callback`, possibly also a `claude.com`
  one) → default `OAUTH_REDIRECT_URIS`.
- That Claude still accepts dynamic client registration (vs. requiring Client
  ID Metadata Documents, which the SDK may not serve). If CIMD is required,
  this spec gets a section for it.
