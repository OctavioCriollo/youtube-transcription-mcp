<div align="center">

# 🎙️ YouTube Transcription MCP

**Turn any YouTube link into clean text — straight from your AI assistant.**

An [MCP](https://modelcontextprotocol.io) server that transcribes YouTube videos through a
smart 3-level fallback chain: **Groq → ElevenLabs → YouTube captions**. Drop it into
OpenClaw, Claude Code, Claude Desktop, Cursor, or any MCP client and ask for a transcript
from any chat.

[![MCP](https://img.shields.io/badge/MCP-compatible-blueviolet)](https://modelcontextprotocol.io)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org)
[![Transport](https://img.shields.io/badge/transport-stdio%20%7C%20http-green)]()
[![Providers](https://img.shields.io/badge/providers-Groq%20%7C%20ElevenLabs%20%7C%20CC-orange)]()

</div>

---

## ✨ Why this exists

You send a YouTube link to your assistant on Telegram (or any channel) and ask:
*"transcribe this for me."* This MCP makes that work — reliably, cheaply, and from
the cloud.

The hard part of YouTube transcription is not the speech-to-text. It's **getting the
audio** when YouTube actively blocks downloads from cloud server IPs. This server solves
that with a chain that always finds a working path:

```
┌──────────────────────────────────────────────────────────────────────┐
│  transcribe_youtube(url)                                               │
│                                                                        │
│  1. Groq Whisper        ─ cheapest (~$0.04/hr), needs yt-dlp download  │
│        │ fails? (e.g. YouTube blocks the host IP)                      │
│        ▼                                                               │
│  2. ElevenLabs Scribe   ─ cloud-safe (~$0.22/hr); ElevenLabs fetches   │
│        │ fails?           the URL on THEIR servers — no IP block       │
│        ▼                                                               │
│  3. YouTube captions    ─ free, lower quality, last resort            │
└──────────────────────────────────────────────────────────────────────┘
```

The first level that succeeds wins. The response tells the agent **which method was used**
and **why earlier ones failed**, so it can be transparent with the user and never silently
hand back low-quality captions as if they were premium audio transcription.

---

## 🚀 Features

- **One tool, zero friction.** `transcribe_youtube(url, language?)` — that's the whole API.
- **Cloud-proof.** The ElevenLabs `source_url` level bypasses YouTube IP blocking entirely.
  No residential proxy, no Tailscale, no cookie juggling required.
- **Cost-aware.** Tries the cheapest provider first; only escalates when needed.
- **Auto language detection.** Detects the spoken language; never translates unless asked.
- **Transparent results.** Every response reports `method`, `provider`,
  `estimated_cost_usd`, and `failed_attempts`.
- **Quality signals.** Audio transcripts come with a structural + linguistic audit
  (token parity, low-confidence detection, suspicious unicode, repeated-word loops).
- **Universal.** Standard MCP — works in OpenClaw, Claude Code, Claude Desktop, Cursor, etc.
- **Two transports.** `stdio` (default, for `uvx`/`npx`-style launch) or `streamable-http`
  (for hosting as a remote service).

---

## 📦 Installation

### As an MCP server (recommended)

The server is launched on demand by your MCP client via `uvx`, straight from this repo —
no manual clone, no global install.

**Prerequisites on the host:**

```bash
# uv / uvx (the Python launcher)
curl -LsSf https://astral.sh/uv/install.sh | sh      # Linux/macOS
# Windows: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# ffmpeg (only used by the Groq audio path)
sudo apt install ffmpeg        # Debian/Ubuntu
brew install ffmpeg            # macOS
```

**Register with OpenClaw:**

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

**Register with Claude Code:**

```bash
claude mcp add transcription-youtube \
  --env GROQ_API_KEY=gsk_... \
  --env ELEVENLABS_API_KEY=... \
  -- uvx --from git+https://github.com/OctavioCriollo/youtube-transcription-mcp.git youtube-transcription-mcp
```

**Register with Claude Desktop / Cursor** (`mcp.json` / `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "transcription-youtube": {
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
    }
  }
}
```

> **Private repo note:** this repository is private. The host running `uvx` must have Git
> credentials for GitHub (e.g. `gh auth login`, an SSH key, or a token in the URL). Once
> configured, `uvx` clones and caches it automatically.

---

## 🎬 Usage

From any connected chat (Telegram, Discord, WebChat, or the Claude Code prompt):

> **Transcribe this video for me:** https://www.youtube.com/watch?v=jNQXAC9IVRw

The agent calls `transcribe_youtube` and replies with the text. Behind the scenes:

```text
[level 1] groq + yt-dlp ........ ✓  → method=groq, cost≈$0.0002
```

or, on a cloud host where YouTube blocks the download:

```text
[level 1] groq + yt-dlp ........ ✗  (HTTP 403 from YouTube)
[level 2] elevenlabs source_url  ✓  → method=elevenlabs, cost≈$0.0012
```

### Tool reference

| Parameter  | Type            | Required | Description                                                                 |
| ---------- | --------------- | -------- | --------------------------------------------------------------------------- |
| `url`      | string          | ✅       | YouTube URL — `watch?v=`, `youtu.be/`, or `shorts/` forms.                   |
| `language` | string \| null  | ❌       | ISO 639-1 code (`es`, `en`, `pt`…). Omit for auto-detect. Never translates.  |

### Response shape

```jsonc
{
  "transcript": "Alright, so here we are, one of the elephants...",
  "language": "english",
  "duration_s": 19.021,
  "model": "whisper-large-v3-turbo",
  "provider": "groq",
  "method": "groq",                       // groq | elevenlabs | subtitles
  "estimated_cost_usd": 0.0002,
  "youtube": { "video_id": "jNQXAC9IVRw", "title": "Me at the zoo", "channel": "jawed" },
  "quality_status": "pass",
  "audit": { "status": "pass", "verdict": "artifacts passed structural and quality checks" },
  "failed_attempts": {                     // present only if an earlier level failed
    "groq": "GroqProviderError[auth]: ..."
  }
}
```

---

## ⚙️ Configuration

All configuration is via environment variables. Each provider level is optional —
a missing key simply skips that level.

| Variable             | Default                          | Purpose                                                      |
| -------------------- | -------------------------------- | ----------------------------------------------------------- |
| `GROQ_API_KEY`       | —                                | **Level 1.** Groq Whisper. Free tier at console.groq.com.   |
| `ELEVENLABS_API_KEY` | —                                | **Level 2.** ElevenLabs Scribe v2 (cloud-safe fallback).    |
| `MCP_TRANSPORT`      | `stdio`                          | `stdio` for `uvx`-launched, or `streamable-http` for remote. |
| `WORKSPACE_DIR`      | `~/.transcription-mcp/workspace` | Cache for downloads + transcript artifacts.                 |
| `MCP_HOST`           | `0.0.0.0`                        | HTTP mode only.                                             |
| `MCP_PORT`           | `8000`                           | HTTP mode only.                                             |
| `MCP_HTTP_PATH`      | `/mcp`                           | HTTP mode only.                                             |

> With **no** API keys set, only the free YouTube-captions level is available.

---

## 🌐 Hosting as a remote service (optional)

Prefer to run it as a long-lived HTTP service (e.g. on a separate machine reached over
Tailscale, so the Groq path uses a residential IP)? Switch the transport:

```bash
MCP_TRANSPORT=streamable-http \
GROQ_API_KEY=gsk_... ELEVENLABS_API_KEY=... \
  uvx --from git+https://github.com/OctavioCriollo/youtube-transcription-mcp.git youtube-transcription-mcp
```

Then point the client at the URL instead of a command:

```bash
openclaw mcp set transcripcion '{
  "url": "http://<host>:8000/mcp",
  "transport": "streamable-http"
}'
```

A `Dockerfile` and `docker-compose.snippet.yml` are included for container deployment.

---

## 🏗️ Architecture

```text
youtube-transcription-mcp/
├── src/
│   ├── transcription_mcp/            # MCP layer (~350 LOC)
│   │   ├── server.py                 # FastMCP setup, stdio/http dispatch
│   │   ├── tools.py                  # the transcribe_youtube tool
│   │   ├── pipeline.py               # 3-level fallback orchestration
│   │   ├── youtube_subtitles.py      # captions via youtube-transcript-api
│   │   └── config.py                 # env-var configuration
│   └── transcription_v4/             # vendored transcription engine
│       ├── providers.py              # Groq + ElevenLabs (+ local) providers
│       ├── pipeline.py               # download, chunk, merge, finalize
│       ├── chunking.py               # split long audio, merge by absolute time
│       ├── subtitles.py              # SRT/VTT builders (lossless)
│       ├── quality.py / audit.py     # structural + linguistic validation
│       └── ...
├── docs/
│   ├── deploy.md                     # step-by-step deployment + troubleshooting
│   └── decisions.md                  # architectural rationale
├── tests/                            # smoke + URL-parsing tests
├── Dockerfile
└── docker-compose.snippet.yml
```

The MCP layer is deliberately thin: it imports the vendored `transcription_v4` engine
directly (no subprocess), orchestrates the fallback chain, and returns a clean JSON object.
See [`docs/decisions.md`](docs/decisions.md) for the full rationale.

---

## 🧪 Development

```bash
git clone https://github.com/OctavioCriollo/youtube-transcription-mcp.git
cd youtube-transcription-mcp

python -m venv .venv
.venv/Scripts/python -m pip install -e .[dev]     # Windows
# .venv/bin/python -m pip install -e .[dev]        # macOS/Linux

.venv/Scripts/python -m pytest                     # run tests
```

Run locally over stdio (what `uvx` does):

```bash
GROQ_API_KEY=gsk_... ELEVENLABS_API_KEY=... .venv/Scripts/youtube-transcription-mcp
```

Quick HTTP smoke test:

```bash
MCP_TRANSPORT=streamable-http .venv/Scripts/youtube-transcription-mcp &
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

---

## 🗺️ Roadmap

- [ ] Async job model with status polling (for videos > 30 min).
- [ ] File-upload ingestion (transcribe attachments forwarded from chat, not just YouTube).
- [ ] Optional SRT / VTT output for the agent.
- [ ] Diarization passthrough (speaker labels) on the ElevenLabs path.
- [ ] Background TTL cleanup of the workspace cache.

---

## 📄 License

Choose a license (MIT recommended for MCP servers) and add a `LICENSE` file.

---

<div align="center">
<sub>Built on the <a href="https://modelcontextprotocol.io">Model Context Protocol</a> ·
Powered by <a href="https://groq.com">Groq</a> &amp; <a href="https://elevenlabs.io">ElevenLabs</a></sub>
</div>
