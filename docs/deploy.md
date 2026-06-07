# Deploying transcription-mcp

This guide is the MCP-side reference for deployment. It covers two patterns:

- **Production deployment** alongside OpenClaw on a VPS: the prebuilt GHCR image
  runs as its own Docker Compose stack, joined to OpenClaw via a shared private
  network and a shared artifacts volume.
- **Home-host deployment over Tailscale**: when the cloud VPS gets HTTP 403 from
  YouTube on the Groq path, host the MCP on a residential IP and let OpenClaw
  reach it over Tailscale.

Desktop-client setups (Codex, Claude Code, Claude Desktop, Cursor) are covered
in the project [README](../README.md#installation).

---

## Production deployment (OpenClaw, containerized streamable-http)

The MCP runs from `ghcr.io/octaviocriollo/youtube-transcription-mcp:latest` in a
**separate** Docker Compose stack next to OpenClaw. Keeping the stacks separate
means adding or removing an MCP never touches the OpenClaw stack: you add a
service to your MCP stack and run `openclaw mcp set` once.

The two stacks share resources **owned by the OpenClaw stack**:

| Resource | Owner | Joined here as | Purpose |
| --- | --- | --- | --- |
| `openclaw-mcp-network` (network) | OpenClaw stack | `external` | Private gateway ↔ MCP traffic. Not exposed to the internet, no Traefik labels. |
| `openclaw_mcp_workspace` (volume) | OpenClaw stack | `external`, read-write | Shared artifacts. Mounted **read-only** on the gateway at `/home/node/.openclaw/mcp-workspace`; **read-write** on this MCP at `/mcp-workspace`. Each MCP writes under its own subdirectory (e.g. `/mcp-workspace/transcription-mcp/`). |

Bring up the OpenClaw stack **first** — it creates both resources. The MCP
stack joins them as `external`.

### 1. Compose service

```yaml
services:
  transcription-mcp:
    image: ${TRANSCRIPTION_MCP_IMAGE:-ghcr.io/octaviocriollo/youtube-transcription-mcp:latest}
    pull_policy: always
    restart: unless-stopped
    init: true
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - NET_RAW
      - NET_ADMIN
    environment:
      TZ: ${TZ:-UTC}
      MCP_TRANSPORT: streamable-http
      MCP_HOST: 0.0.0.0
      MCP_PORT: ${TRANSCRIPTION_MCP_PORT:-8000}
      MCP_HTTP_PATH: ${TRANSCRIPTION_MCP_HTTP_PATH:-/mcp}
      WORKSPACE_DIR: /mcp-workspace/transcription-mcp
      OPENCLAW_WORKSPACE_DIR: ${OPENCLAW_MCP_WORKSPACE_DIR:-/home/node/.openclaw/mcp-workspace}/transcription-mcp
      TRANSCRIPTION_JOB_STALE_SECONDS: ${TRANSCRIPTION_JOB_STALE_SECONDS:-180}
      TRANSCRIPTION_JOB_TIMEOUT_SECONDS: ${TRANSCRIPTION_JOB_TIMEOUT_SECONDS:-3600}
      GROQ_API_KEY: ${GROQ_API_KEY:-}
      ELEVENLABS_API_KEY: ${ELEVENLABS_API_KEY:-}
    volumes:
      - ${MCP_WORKSPACE_VOLUME:-openclaw_mcp_workspace}:/mcp-workspace
    expose:
      - "${TRANSCRIPTION_MCP_PORT:-8000}"
    healthcheck:
      test:
        [
          "CMD",
          "python",
          "-c",
          "import os,urllib.request,sys; url='http://127.0.0.1:'+os.environ.get('MCP_PORT','8000')+'/health'; sys.exit(0 if urllib.request.urlopen(url, timeout=4).status==200 else 1)",
        ]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 40s
    networks:
      - openclaw-mcp-network

volumes:
  openclaw_mcp_workspace:
    external: true
    name: ${MCP_WORKSPACE_VOLUME:-openclaw_mcp_workspace}

networks:
  openclaw-mcp-network:
    external: true
    name: ${MCP_NETWORK:-openclaw-mcp-network}
```

### 2. Environment file template

```bash
# Shared timezone
TZ=UTC

# Shared Docker network with the OpenClaw gateway.
# Created by the OpenClaw stack; this stack joins it as external.
MCP_NETWORK=openclaw-mcp-network

# Shared Docker volume for MCP artifacts.
# Created by the OpenClaw stack; mounted read-write here, read-only in the gateway.
MCP_WORKSPACE_VOLUME=openclaw_mcp_workspace

# Path where the OpenClaw gateway mounts that volume (read-only).
# Used only so this MCP can report bundle_path_for_openclaw for file delivery.
OPENCLAW_MCP_WORKSPACE_DIR=/home/node/.openclaw/mcp-workspace

# Image. Pin a tag (e.g. :sha-... or :vX) for strict reproducibility.
TRANSCRIPTION_MCP_IMAGE=ghcr.io/octaviocriollo/youtube-transcription-mcp:latest
TRANSCRIPTION_MCP_PORT=8000
TRANSCRIPTION_MCP_HTTP_PATH=/mcp

# Job robustness (production hardening).
# STALE: seconds without a heartbeat before a running job is marked stale_failed
#        so a dead/hung worker stops blocking a concurrency slot. 0 disables.
# TIMEOUT: hard ceiling in seconds for a single job. 0 disables.
TRANSCRIPTION_JOB_STALE_SECONDS=180
TRANSCRIPTION_JOB_TIMEOUT_SECONDS=3600

# Provider keys (consumed by transcription-mcp).
# GROQ is the cheapest level; ELEVENLABS is the cloud-safe fallback.
# Leave blank to disable that provider level.
GROQ_API_KEY=
ELEVENLABS_API_KEY=

# Provider order is server policy (clients cannot choose it). Optional overrides:
# MCP_YOUTUBE_PROVIDER_ORDER=groq,elevenlabs,subtitles
# MCP_MEDIA_PROVIDER_ORDER=groq,elevenlabs
# MCP_FILE_PROVIDER_ORDER=groq,elevenlabs
# MCP_LOCK_PROVIDER_ORDER=true
```

Real `.env` files are gitignored. Convention: `.env.mcp-<proyecto>.<entorno>`
(e.g. `.env.mcp-NT.production`).

### 3. Bring up the stack

```bash
# 1. Bring up the OpenClaw stack FIRST (it creates the network and volume).

# 2. Bring up the MCP stack
docker compose -f openclaw-mcp-servers.yml --env-file .env.mcp-<env> pull
docker compose -f openclaw-mcp-servers.yml --env-file .env.mcp-<env> up -d

# 3. Verify the container is healthy
docker compose -f openclaw-mcp-servers.yml --env-file .env.mcp-<env> ps
```

### 4. Register with OpenClaw

One-time registration; OpenClaw hot-applies it (no gateway restart needed):

```bash
docker exec openclaw-openclaw-gateway-1 \
  openclaw mcp set youtube-transcription \
  '{"url":"http://transcription-mcp:8000/mcp","transport":"streamable-http"}'

docker exec openclaw-openclaw-gateway-1 openclaw mcp list
```

The MCP does **not** need provider keys inside `openclaw.json`. Keys live only
in the MCP stack's `.env`.

### 5. Verify connectivity from the gateway

```bash
docker exec openclaw-openclaw-gateway-1 sh -lc \
  'node -e "fetch(\"http://transcription-mcp:8000/health\").then(async r=>console.log(r.status, await r.text()))"'
# expected: 200 {"status":"ok","transport":"streamable-http","workspace_dir":"/mcp-workspace/transcription-mcp","active_jobs":0}
```

### 6. Smoke test from a chat channel

In any OpenClaw-connected chat (Telegram, Discord, WebChat), use a video you
own or have permission to process:

> Transcribe this video from my channel: `<YOUR_YOUTUBE_VIDEO_URL>`

The agent should call `transcribe_youtube` and reply with the text. For long
videos, production agents should prefer the async tools
(`start_youtube_transcription` + `watch_transcription` + `get_transcription_result`).
The MCP returns `user_visible_message`, `recommended_next_tool` and
`recommended_poll_seconds` so the agent can keep the user informed.

### 7. Updating

Every push to `main` republishes `ghcr.io/octaviocriollo/youtube-transcription-mcp:latest`
via the CI workflow. The **running container does not auto-update**; on the
host:

```bash
docker compose -f openclaw-mcp-servers.yml --env-file .env.mcp-<env> pull
docker compose -f openclaw-mcp-servers.yml --env-file .env.mcp-<env> up -d
```

For strict reproducibility, pin `TRANSCRIPTION_MCP_IMAGE` to a `:sha-...` or
`:vX` tag instead of `:latest`; updating then means changing the tag and
redeploying.

---

## Variant: host on a separate machine over Tailscale

When the Groq path needs a residential IP (cloud VPS gets HTTP 403 from
YouTube), run the MCP on a home machine and let OpenClaw reach it over
Tailscale.

```bash
# On the home PC
git clone https://github.com/OctavioCriollo/youtube-transcription-mcp.git
cd youtube-transcription-mcp
uv sync --frozen
export GROQ_API_KEY=gsk_... ELEVENLABS_API_KEY=...
MCP_TRANSPORT=streamable-http uv run --frozen youtube-transcription-mcp
```

```bash
# On the OpenClaw host
docker exec openclaw-openclaw-gateway-1 \
  openclaw mcp set youtube-transcription \
  '{"url":"http://<tailscale-hostname>:8000/mcp","transport":"streamable-http"}'
```

In this variant the gateway cannot read MCP artifacts from a shared volume, so
file delivery via `create_transcription_bundle` is not available — the agent
gets the transcript text only.

---

## Troubleshooting

### `GROQ_API_KEY env var is required` only when the audio fallback is hit

The captions path does not need Groq. If you see this, the captions path failed
(video had no captions) **and** Groq is not configured.

### `yt-dlp failed: HTTP Error 403` (audio fallback)

YouTube blocked the host IP. Options, in order:

- configure `YT_COOKIES_FILE` if you have a valid cookies.txt for the host;
- configure `YT_PROXY` if you operate a trusted proxy;
- let the fallback chain use ElevenLabs `source_url`, which does not depend on
  the MCP host IP;
- or fall back to the Tailscale variant above.

### Tool is registered but invisible to the agent

Restart OpenClaw if it does not hot-reload MCP changes:

```bash
docker compose restart openclaw
```

Then check OpenClaw's logs for MCP discovery messages.

### Container reports `unhealthy` / `/health` returns 503

The MCP could not reach its workspace or job store. Confirm `WORKSPACE_DIR` is
a valid path inside the container and that the shared volume is mounted at
that path.

### Gateway cannot find the bundle file

When `create_transcription_bundle` returns a `bundle_path_for_openclaw` that
the gateway cannot open, check that:

- `OPENCLAW_WORKSPACE_DIR` matches the path where the gateway mounts the shared
  volume (read-only);
- the gateway container actually mounts `openclaw_mcp_workspace` at that path;
- the run was written by the MCP (the bundle lives at
  `<run_dir>/exports/transcription_bundle.zip`).
