# Deploying transcription-mcp into OpenClaw

This guide assumes you already have OpenClaw running and the `openclaw`
CLI accessible on the host machine.

## Deployment models

Three valid models per the official [OpenClaw MCP docs](https://docs.openclaw.ai/cli/mcp):

| Model | When to pick it |
| --- | --- |
| **A. uvx from your GitHub repo** (this guide, recommended) | Standard OpenClaw pattern. Auto-installs the package on first use. Works on cloud for videos with captions. |
| **B. uvx from PyPI** | Same as A but the package is published publicly to PyPI. Add later if you want third-party consumers. |
| **C. Remote HTTP (streamable-http)** | When you need yt-dlp to run from a residential IP — host this MCP on your home PC and reach it over Tailscale. |

This guide focuses on **Model A**.

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
    "ELEVENLABS_API_KEY": "..."
  }
}'
```

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

In any OpenClaw-connected chat (Telegram, Discord, WebChat):

> Transcribe this 19-second video: https://www.youtube.com/watch?v=jNQXAC9IVRw

The agent should call `transcribe_youtube` and reply with the text.

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

YouTube blocked the cloud IP. For videos without captions, switch to
Model C (Tailscale to home PC). See README "Known limit" section.

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
python -m venv .venv
.venv/Scripts/python -m pip install -e .
export GROQ_API_KEY=gsk_...
MCP_TRANSPORT=streamable-http .venv/Scripts/transcription-mcp
```

Then on the OpenClaw host, with Tailscale connecting both:

```bash
openclaw mcp set transcripcion '{
  "url": "http://<tailscale-hostname>:8000/mcp",
  "transport": "streamable-http"
}'
```

Same package, different transport, different host.
