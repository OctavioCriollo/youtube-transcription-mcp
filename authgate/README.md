# authgate — human-in-the-loop YouTube login gateway

`authgate` mints YouTube **session cookies** for the transcription MCP by letting
a human log in once, in a browser that runs **on the server**. Those cookies let
the cheap Groq tier's `yt-dlp` download survive the datacenter "Sign in to
confirm you're not a bot" wall, instead of escalating every job to the pricier
ElevenLabs tier.

It is a separate service and a separate image from the MCP — deliberately, so it
has its own lifecycle and never bloats the MCP container.

## Why a server-side browser (and not an OAuth link)

YouTube downloads are not an official API; `yt-dlp` impersonates a browser and
YouTube demands **session cookies**, which only exist inside the browser where
the login happened. There is no OAuth callback that hands cookies back to a
server (Google killed the device-code flow in 2024). So the login must occur in
a browser the server can read — this one. Cookies minted here are also born on
the server's own IP, so YouTube sees no "IP jump" and they last longer than
cookies exported from your laptop.

## Flow

```
user (Telegram): "transcribe esto"
   -> MCP transcribes; Groq blocked as bot -> result carries youtube_login_would_help
   -> agent calls request_youtube_login  --HTTP-->  authgate /internal/sessions
   -> authgate launches headed Chromium (Xvfb :99), returns a one-time token URL
   -> agent sends the link to the user over chat
   user opens link (Traefik TLS + ForwardAuth token) -> noVNC -> the remote browser
   user logs in with a DISPOSABLE Google account
   -> authgate detects the session cookie, exports Netscape cookies.txt to the
      shared volume, closes the browser
   -> agent polls get_youtube_auth_status -> authenticated
   -> agent retries: MCP uses the cookies, Groq downloads, cheap tier restored
```

## Cookie lifecycle (sliding 24h TTL)

The cookies file lives on the shared MCP workspace volume
(`.../transcription-mcp/secrets/youtube-cookies.txt`, `0600`). The MCP **touches**
its mtime on every successful cookie-backed download; authgate's janitor
**reaps** it after `AUTHGATE_COOKIE_IDLE_TTL_S` (default 24h) **without use**.
Constant activity keeps a login alive indefinitely; a full idle day drops it and
the next blocked job asks the user to log in again.

## Exposure & security (option 3: path on the existing host)

The browser bytes flow **Traefik → websockify** directly; the app stays out of
that path. Traefik routes `PathPrefix(/ytauth/s/)` on the existing OpenClaw
hostname (no new DNS record), and a **ForwardAuth** check calls this service's
`/auth` for every request — only a URL carrying a live capability token reaches
the browser.

- Capability token: random, single login window, expires with the session.
- `x11vnc` binds to localhost with no VNC password; the only door is Traefik +
  ForwardAuth.
- Use a **disposable** Google account: logging in from a datacenter IP can get an
  account flagged. Never a personal account.
- The browser only runs during an active login (bounded RAM on a small VPS).

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `AUTHGATE_HOST` / `AUTHGATE_PORT` | `0.0.0.0` / `8080` | internal API + `/auth` |
| `AUTHGATE_STATE_DIR` | `/state` | persisted session JSON (survives restarts) |
| `AUTHGATE_MANAGED_COOKIES_FILE` | `/mcp-workspace/transcription-mcp/secrets/youtube-cookies.txt` | where cookies are written; must match the MCP's `MANAGED_YT_COOKIES_FILE` |
| `AUTHGATE_COOKIE_IDLE_TTL_S` | `86400` | sliding idle TTL for the cookies file |
| `AUTHGATE_SESSION_TTL_S` | `900` | how long a login window stays open |
| `AUTHGATE_LOGIN_START_URL` | Google sign-in → YouTube | first page the browser opens |
| `AUTHGATE_AUTH_COOKIE_NAMES` | `__Secure-1PSID,__Secure-3PSID,SID` | presence == logged in |
| `AUTHGATE_EXPORT_DOMAINS` | `.youtube.com,.google.com` | domains included in the export |

On the MCP side, set `AUTHGATE_BASE_URL` (e.g. `http://yt-auth:8080`),
`AUTHGATE_PUBLIC_LOGIN_BASE` (e.g. `https://<host>/ytauth`), and
`MANAGED_YT_COOKIES_FILE` to the same path as above.

## Server verification (after deploy)

The browser/VNC layer can only be verified on the server. After the stack is up:

1. `docker ps` shows `yt-auth` healthy.
2. `curl -fsS http://yt-auth:8080/healthz` from another container on the MCP
   network returns `{"status":"ok"}`.
3. From an agent (or `curl`): `POST /internal/sessions` returns a `login_path`.
4. Open `https://<host>/ytauth/s/<token>/vnc.html?autoconnect=true&resize=remote`
   in a browser → the remote Chromium on the Google login page appears.
5. Log in with the disposable account → within a few seconds
   `GET /internal/active` reports the session gone and the cookies file exists
   at the managed path.
6. Transcribe a video that previously escalated to ElevenLabs → it now completes
   on `groq`.

## Tests

`pytest authgate/tests` covers the pure logic (Netscape serialization, sliding
TTL, session state machine, persistence) and the HTTP surface (ForwardAuth token
gating, single-flight session creation) with the browser mocked.
