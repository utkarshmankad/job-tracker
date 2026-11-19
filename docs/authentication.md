# Authentication

Job Tracker has one intended user: the owner. Every API endpoint that returns or changes
tracker data requires a signed-in session. Only `GET /api/v1/health` (which returns
`{"status": "ok"}` and nothing else) and the sign-in endpoints under `/api/v1/auth/` can be
called without one.

This document never contains real credentials. Each value below is configured in the place
named, never committed to the repository, and never pasted into chat or issue trackers.

## How it works

1. The login screen calls `GET /api/v1/auth/config` for the public Google OAuth client ID
   and a single-use login nonce.
2. Google Identity Services ("Sign in with Google") returns a Google **ID token** to the
   browser.
3. The browser posts the token to `POST /api/v1/auth/google`. The backend verifies it
   server-side: Google's signature, issuer, audience (`AUTH_GOOGLE_CLIENT_ID`), expiry, and
   the nonce. It then requires `email_verified` and an email equal to
   `AUTH_ALLOWED_EMAIL`. Any other Google account gets `403 account_not_allowed`.
4. The backend sets its own session cookie: HMAC-signed, `HttpOnly`, `SameSite=Lax`,
   `Secure` with the `__Host-` prefix in production, and valid for
   `AUTH_SESSION_TTL_SECONDS`. It also returns a CSRF token that the frontend keeps only in
   memory.
5. Every protected request carries the cookie. State-changing requests (`POST`, `PATCH`,
   `DELETE`) must also send `X-CSRF-Token`.
6. Signing out (`POST /api/v1/auth/logout`) revokes the session server-side and clears the
   cookie. When a session expires or is revoked, the next request returns `401` and the app
   goes back to the login screen with an explanation.

The frontend bundle contains no reusable secret. The Google client ID is public by design,
and the CSRF token is useless without the HttpOnly cookie.

**Same-origin in production.** The frontend and API must share an origin (for example behind one reverse proxy).
The browser therefore talks only to that origin, so the session cookie is first-party
and is not affected by third-party-cookie blocking.

### Responses

| Status | `code` | Meaning |
|---|---|---|
| 401 | `not_authenticated` | No session cookie |
| 401 | `session_expired` | Session lifetime ended |
| 401 | `session_revoked` | Signed out |
| 401 | `invalid_session` | Malformed or tampered cookie, or the allowed account changed |
| 401 | `invalid_token` | Google token failed verification, or the nonce was missing, expired or reused |
| 403 | `account_not_allowed` | Valid Google account that is not `AUTH_ALLOWED_EMAIL` |
| 403 | `csrf_failed` | Missing or wrong `X-CSRF-Token` on a state-changing request |
| 403 | `origin_not_allowed` | Sign-in posted from an origin that isn't allowed |
| 429 | `rate_limited` | Too many requests; see `Retry-After` |
| 503 | `auth_not_configured` / `identity_provider_unavailable` | Server misconfiguration, or Google's certificates are unreachable |

Every error body has the shape `{"detail": "...", "code": "..."}` and is sent with
`Cache-Control: no-store`.

### Rate limits

Limits are kept in process memory and counted per client address. The backend runs as a
single process.

- Sign-in attempts: 10 per 5 minutes (`AUTH_RATE_LIMIT_*` in `backend/config.py`).
- Sensitive operations: 30 per minute, shared across all of them. These are merge, delete,
  bulk edits, imports, export, duplicate scan, poller trigger and backfill, Gmail re-auth,
  suppress-rule changes, and diagnostics (`SENSITIVE_RATE_LIMIT_*`).

### Logging

Nothing logs tokens, cookies, authorization headers, CSRF values, nonces, or email
addresses. Failed sign-ins log only a reason code. Uvicorn access logs have query strings
replaced with `?[redacted]`, because the Gmail re-auth callback carries an OAuth code.

## Environment variables

| Name | Where | Required | Notes |
|---|---|---|---|
| `APP_ENV` | environment (set in the Docker image) | yes in prod | `production` makes the API refuse to start if anything below is missing |
| `AUTH_MODE` | backend | no | `google` (default) or `local`. `local` is refused in production |
| `AUTH_ALLOWED_EMAIL` | environment / local `.env` | yes (google) | The only Google account allowed to sign in |
| `AUTH_GOOGLE_CLIENT_ID` | environment / local `.env` | yes (google) | Web-application OAuth client ID used for Sign in with Google |
| `AUTH_SESSION_SECRET` | environment / local `.env` | yes (google, prod) | At least 32 random characters. Signs sessions, CSRF tokens and nonces |
| `AUTH_SESSION_TTL_SECONDS` | backend | no | Session lifetime. Default 43200 (12 h); allowed range 300–604800 |
| `FRONTEND_ORIGIN` | environment | yes in prod | `https://` origin of the frontend. Used for CORS and the sign-in origin check |
| `PUBLIC_BASE_URL` | environment | for Gmail re-auth | Must be the frontend origin so the session cookie reaches `/poller/reauth/callback` |
| `VITE_API_BASE` | frontend build | **must be unset** | Leave unset so production calls `/api` on the same origin. Only set it for local setups that point somewhere unusual |

**Handling the session secret.** Generate it and pipe it straight into its destination, so
it never appears on screen, on a command line, or in shell history:

```bash
# Local .env (git-ignored)
python3 -c "import secrets; print('AUTH_SESSION_SECRET=' + secrets.token_urlsafe(48))" >> .env
```

Never write the value into a command line; shell history would keep it.

## Google Cloud setup (one time)

1. Google Cloud Console → APIs & Services → Credentials → **Create OAuth client ID** →
   *Web application*. This can be the same project as the Gmail ingestion client, but it
   should be a separate **Web** client.
2. **Authorized JavaScript origins**:
   - your production Vercel origin, e.g. `https://<your-app>.vercel.app`
   - `http://jobtracker.localhost:5173` for local development with Google mode
3. Authorized redirect URIs aren't needed for sign-in (the popup flow is used).
4. Copy the client ID into `AUTH_GOOGLE_CLIENT_ID` in the places listed below. The client
   *secret* of this web client isn't used by Job Tracker.

For the Gmail re-auth web flow, the Gmail ingestion OAuth client
(`client_secret.json` on the server) must list
`<PUBLIC_BASE_URL>/api/v1/poller/reauth/callback` as an authorized redirect URI. In
production, `PUBLIC_BASE_URL` is the Vercel origin.

## Local development

Pick one mode explicitly. Authentication is never silently disabled.

**Local developer mode** (no Google setup needed). In the repository `.env`:

```dotenv
AUTH_MODE=local
# optional: AUTH_ALLOWED_EMAIL=you@example.com  (shown as the signed-in account)
# optional: AUTH_SESSION_SECRET=...              (otherwise sessions end when the API restarts)
```

Start the backend and frontend as usual. The login screen shows **Continue as local
developer**, which only works from `127.0.0.1`/`::1` and is rejected whenever
`APP_ENV=production`.

**Google mode locally.** In `.env`, set `AUTH_MODE=google`, `AUTH_ALLOWED_EMAIL`,
`AUTH_GOOGLE_CLIENT_ID`, and `AUTH_SESSION_SECRET`. Then add
`http://jobtracker.localhost:5173` to the client's JavaScript origins.

If `AUTH_MODE=google` and settings are missing outside production, the API still starts, but
the login screen reports "Sign-in isn't configured" and every protected endpoint returns 401.

**Tests.** Backend route tests opt in to the explicit `test_auth_user` fixture
(`tests/conftest.py`). It overrides the `require_user` dependency with a fixed owner.
`tests/integration/test_auth.py` exercises the real authentication path without that
override. E2E tests run the backend with `AUTH_MODE=local` and sign in through the UI.

## Rotating configuration

| What | How | Effect |
|---|---|---|
| Session secret | Generate a new value (see *Handling the session secret*) and update `.env` | Restart the backend. Every existing session, CSRF token and pending nonce becomes invalid, so you sign in again |
| Allowed account | Update `AUTH_ALLOWED_EMAIL` in `.env` | Sessions issued to the previous account are rejected (`invalid_session`) |
| Google client ID | Create a new web client with the same JavaScript origins, set `AUTH_GOOGLE_CLIENT_ID`, then delete the old client in Google Cloud | New sign-ins use the new client. Existing sessions continue until they expire |
| Session lifetime | Update `AUTH_SESSION_TTL_SECONDS` in `.env` | Applies to new sessions |
| Suspected compromise | Rotate the session secret first (ends all sessions), then review the Google account's security events | Immediate global sign-out |

For local `.env` changes, restart the backend.
