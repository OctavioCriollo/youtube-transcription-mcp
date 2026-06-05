# Deploying transcription-mcp into OpenClaw

This guide assumes you already have OpenClaw running and the `openclaw`
CLI accessible on the host machine.

## Deployment models

| Model | When to pick it |
| --- | --- |
| **P. Containerized streamable-http in a separate stack** (production, recommended) | What this project actually runs in production with OpenClaw on a VPS. A prebuilt GHCR image runs as its own Docker Compose stack alongside OpenClaw, sharing a private network and an artifacts volume. Adding/removing an MCP never touches the OpenClaw stack. See [Production deployment](#production-deployment-containerized-recommended) below. |
| **A. uvx from your GitHub repo** | Simple local/dev pattern. OpenClaw launches the server as a child process via `uvx`. No container, but the OpenClaw host must have `uv` + `ffmpeg`, and config (incl. API keys) lives in `openclaw.json`. |
| **B. uvx from PyPI** | Same as A but the package is published publicly to PyPI. |
| **C. Remote HTTP over Tailscale** | When you need yt-dlp to run from a residential IP — host this MCP on your home PC and reach it over Tailscale. |

> **Why P (containerized) is the production choice and A (uvx) was demoted:**
> the audio path needs `ffmpeg`/`yt-dlp`, which are not in OpenClaw's gateway image;
> baking them in (or installing at runtime) is fragile and lost on container
> recreate. The uvx model also stored provider API keys inside `openclaw.json`.
> The containerized model ships its own dependencies in a self-contained image, keeps
> keys in the MCP stack's `.env`, and scales: a new MCP is just another service in the
> MCP stack, registered once with `openclaw mcp set`.

The **full, operator-grade procedure** for Model P (compose files, shared network +
volume ownership, registration, verification, troubleshooting) lives in the OpenClaw
deployment repo: `OpenClaw v1.0/mcp-servers/README.md` and
`OpenClaw v1.0/doc/configuracion_openclaw_paso_a_paso.md` (section 24). The summary
below is the MCP-side view.

## Production deployment (containerized, recommended)

The MCP runs as a container from the published GHCR image
(`ghcr.io/octaviocriollo/youtube-transcription-mcp:latest`) in a **separate** Docker
Compose stack (`mcp-servers/openclaw-mcp-servers.yml` in the OpenClaw repo). It shares
two resources that the **OpenClaw stack owns and creates** (fixed name, not external):

| Resource | Purpose |
| --- | --- |
| network `openclaw-mcp-network` | Private gateway ↔ MCP traffic. Not exposed to the internet, no Traefik labels. |
| volume `openclaw_mcp_workspace` | Shared artifacts. Mounted **read-only** on the gateway at `/home/node/.openclaw/mcp-workspace`; **read-write** on the MCP at `/mcp-workspace`. Each MCP writes under its own subdirectory. |

Key environment for the container (set in the MCP stack `.env`):

```bash
MCP_TRANSPORT=streamable-http
WORKSPACE_DIR=/mcp-workspace/transcription-mcp                 # per-MCP subdir of the shared volume
OPENCLAW_WORKSPACE_DIR=/home/node/.openclaw/mcp-workspace/transcription-mcp  # how the gateway sees it (ro)
TRANSCRIPTION_JOB_STALE_SECONDS=180
TRANSCRIPTION_JOB_TIMEOUT_SECONDS=3600
GROQ_API_KEY=...
ELEVENLABS_API_KEY=...
```

Bring up OpenClaw first (it creates the network + volume), then the MCP stack, then
register once:

```bash
docker exec <gateway-container> \
  openclaw mcp set youtube-transcription \
  '{"url":"http://transcription-mcp:8000/mcp","transport":"streamable-http"}'
```

Delivery: when the agent needs to send a file, it calls `create_transcription_bundle`
and sends `bundle_path_for_openclaw` (the gateway reads it through the read-only
mount). The image declares **no** `VOLUME`; persistence is the shared volume.

Health: the Docker `HEALTHCHECK` hits `GET /health`, so a hung-but-listening MCP is
restarted. Verify reachability from the gateway:

```bash
docker exec <gateway-container> sh -lc \
  'node -e "fetch(\"http://transcription-mcp:8000/health\").then(async r=>console.log(r.status, await r.text()))"'
# expected: 200 {"status":"ok","transport":"streamable-http","workspace_dir":"/mcp-workspace/transcription-mcp","active_jobs":0}
```

---

## Alternative: uvx child-process (Model A, local/dev)

The rest of this guide documents **Model A** (`uvx`), useful for a quick local setup
where OpenClaw runs natively (not in a container) and has `uv` + `ffmpeg` available.
For production on a VPS, prefer Model P above.

## 1. Install `uv` on the OpenClaw host (one time)

`uvx` is the launcher; `uv` is its umbrella tool. Install once:

```bash
# Linux/macOS
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Verify:

```bash
uvx --version
```

## 2. Get API keys for the providers you want

The MCP runs a 3-level chain (Groq → ElevenLabs → captions). Each
level is optional; missing keys simply skip that level.

| Provider | Why have it | Where to get a key |
| --- | --- | --- |
| **Groq** (~$0.04/hr) | Cheapest level. Used first when yt-dlp can reach YouTube (residential IP, etc.). Free tier available. | [console.groq.com](https://console.groq.com) → `gsk_...` |
| **ElevenLabs** (~$0.22/hr) | Cloud-safe fallback when Groq/yt-dlp gets blocked. ElevenLabs downloads YouTube URL server-side. | [elevenlabs.io](https://elevenlabs.io) → API Keys |
| **(none for captions)** | Free fallback for both. No key needed. | n/a |

**Recommended for cloud VPS:** configure both Groq and ElevenLabs.
On a residential IP host (Tailscale), Groq alone is enough — but
ElevenLabs as a safety net adds no cost when Groq succeeds.

## 3. Ensure ffmpeg on the host (only for audio fallback)

```bash
# Ubuntu/Debian
sudo apt-get update && sudo apt-get install -y ffmpeg

# macOS
brew install ffmpeg
```

If you only ever use the captions path, you can skip this — but it's
cheap and harmless to install.

## 4. Register the MCP with OpenClaw

```bash
openclaw mcp set transcripcion '{
  "command": "uvx",
  "args": [
    "--from",
    "git+https://github.com/OctavioCriollo/youtube-transcription-mcp.git",
    "youtube-transcription-mcp"
  ],
  "env": {
    "GROQ_API_KEY": "gsk_...",
    "ELEVENLABS_API_KEY": "...",
    "YT_COOKIES_FILE": "/run/secrets/youtube-cookies.txt",
    "YT_PROXY": "http://proxy-host:8080",
    "MCP_CACHE_TTL_HOURS": "24",
    "MCP_MAX_CONCURRENT_JOBS": "2",
    "MCP_JOB_TTL_HOURS": "168"
  }
}'
```

`YT_COOKIES_FILE` and `YT_PROXY` are optional. Leave them out unless the host
needs help with the cheaper Groq + yt-dlp path.

For a private repo, the host must be able to clone it. Options:

- SSH: replace the URL with `git+ssh://git@github.com/<user>/transcription-mcp.git`
  and have an SSH key configured for the OS user running OpenClaw.
- HTTPS + token: `git+https://<token>@github.com/<user>/transcription-mcp.git`.

To pin a specific commit or tag:

```text
git+https://github.com/<user>/transcription-mcp.git@v0.1.0
git+https://github.com/<user>/transcription-mcp.git@<commit-sha>
```

## 5. Verify

```bash
openclaw mcp list
openclaw mcp show transcripcion
```

OpenClaw stores the entry but does NOT validate connectivity at this
step (per their docs). The MCP is spawned lazily when the agent first
calls a tool.

## 6. Smoke test from your channel

In any OpenClaw-connected chat (Telegram, Discord, WebChat), use a video you own
or have permission to process:

> Transcribe this video from my channel: <YOUR_YOUTUBE_VIDEO_URL>

The agent should call `transcribe_youtube` and reply with the text.

For long videos, production agents should prefer the async tools:
`start_youtube_transcription`, `get_transcription_status`, and
`get_transcription_result`. The MCP returns `user_visible_message`,
`recommended_next_tool`, and `recommended_poll_seconds` so the agent can keep
the user informed instead of appearing stuck. Clients with prompt support can
also use the MCP prompt `transcribe_with_progress`.

## Troubleshooting

### `uvx: command not found` in OpenClaw logs

Install `uv` on the OpenClaw host (step 1). If OpenClaw runs inside a
container, `uvx` must be inside that container or accessible via PATH.

### `Failed to fetch git+https://...`

OpenClaw cannot clone your repo. Confirm:

- The URL is reachable from the host: `git clone <url> /tmp/test-clone`.
- For private repos, credentials are configured for the OS user that runs
  OpenClaw (not your interactive shell).

### `GROQ_API_KEY env var is required` only when audio fallback is hit

The captions path does not need Groq. If you see this, your captions
path failed (video had no captions) AND Groq isn't configured.

### `yt-dlp failed: HTTP Error 403` (audio fallback)

YouTube blocked the host IP. Options, in order:

- configure `YT_COOKIES_FILE` if you have a valid cookies.txt for the host;
- configure `YT_PROXY` if you operate a trusted proxy;
- let the fallback chain use ElevenLabs `source_url`, which does not depend on
  the MCP host IP.

### Server seems to start but tool is invisible to the agent

Restart OpenClaw if it does not hot-reload MCP changes:

```bash
docker compose restart openclaw
# or however your deployment restarts the service
```

Then check OpenClaw's logs for MCP discovery messages.

## Updating

OpenClaw re-clones on the next spawn (when the agent calls a tool).
If you want to force a refresh, you can bump the pinned revision in the
mcp set command:

```bash
openclaw mcp set transcripcion '{... "args": [..., "git+https://.../repo.git@v0.2.0", ...] ...}'
```

Or simply call `openclaw mcp unset transcripcion` then `set` again with
the same config.

## Alternative: HTTP mode for residential IP

If yt-dlp keeps getting blocked on your cloud VPS and you need audio
transcription, host this MCP on your home machine instead:

```bash
# On your home PC
git clone https://github.com/<you>/transcription-mcp.git
cd transcription-mcp
uv sync --frozen
export GROQ_API_KEY=gsk_...
MCP_TRANSPORT=streamable-http uv run --frozen youtube-transcription-mcp
```

Then on the OpenClaw host, with Tailscale connecting both:

```bash
openclaw mcp set transcripcion '{
  "url": "http://<tailscale-hostname>:8000/mcp",
  "transport": "streamable-http"
}'
```

Same package, different transport, different host.
