# Architectural decisions

This document captures the trade-offs of this MVP so the next iteration
does not repeat patterns this codebase deliberately avoided.

## Walking skeleton first

This version implements one tool, synchronously, for YouTube URLs only.
The smallest thing that closes the loop:

```text
user in Telegram → OpenClaw agent → MCP tool → text back
```

Build smallest, validate, then iterate. The previous attempt
(`transcription-mcp/` in the parent directory) designed for every
future concern from day one — DDD layers, SQLite registry, manifest
checksums, 12-state job machine, multi-tenancy hooks — and never
validated the end-to-end flow with OpenClaw.

## Distribution via `uvx --from git+https://...`, not as a Docker side-car

OpenClaw's first-class MCP distribution pattern is to launch the server
as a child process via `uvx` (Python) or `npx` (Node). Per the official
docs, the configuration is just:

```json
{
  "command": "uvx",
  "args": ["--from", "git+https://github.com/.../repo.git", "transcription-mcp"]
}
```

This pattern wins because:

- Zero infrastructure changes on the OpenClaw host beyond `uv` install.
- Updates ride a git tag bump — no Docker rebuild, no compose edit.
- Same model OpenClaw itself documents for `context7-mcp` and similar.

The Docker side-car / remote HTTP model is still supported in the same
binary (see `MCP_TRANSPORT=streamable-http`), and is the right escape
hatch when the MCP needs to run on a different host than OpenClaw
(typically your home PC via Tailscale, to avoid YouTube IP blocking).

## Default transport: stdio

`uvx` launches the process and talks to it over stdin/stdout. So the
default is stdio. The HTTP transport is opt-in via env var.

Consequence: anything written to stdout other than the MCP protocol
corrupts the session. v4's `print()`-based progress logging is silenced
by always passing `progress=False` to v4's pipeline. Operational
visibility uses Python `logging` to stderr instead.

## 3-level provider chain: Groq → ElevenLabs URL → CC

The pipeline tries three methods in order; first success wins:

1. **Groq Whisper + yt-dlp** (cheapest, ~$0.04/hr). We download the
   audio with yt-dlp and POST it to Groq. This is the preferred
   path purely by cost. Failure mode: YouTube blocks yt-dlp from
   cloud VPS IPs (DigitalOcean, AWS, Hetzner, etc.) with HTTP 403.

2. **ElevenLabs Scribe v2 via `source_url`** (~$0.22/hr). We hand
   the YouTube URL directly to ElevenLabs. ElevenLabs downloads it
   on **their** infrastructure, so our host IP is irrelevant.
   Works from any cloud host. Higher quality than Groq Whisper
   turbo. v4 already had this implemented and used it as the
   default for `transcribe_youtube` — we inherit that capability.

3. **YouTube captions/CC** via `youtube-transcript-api` (free,
   degraded). Last resort when both pay providers fail. Often
   auto-generated, no punctuation, errors on proper nouns.

The tool response always includes a `method` field
(`"groq"` / `"elevenlabs"` / `"subtitles"`). When earlier levels
fail, the response also includes `failed_attempts` (a dict of
provider → reason) so the agent can transparently tell the user
what happened and why.

### Why this beats picking one provider

- **Cost-optimised when possible:** Groq is tried first whenever
  yt-dlp can reach YouTube (residential IP, Tailscale exit node,
  unblocked cloud egress).
- **Cloud-deploy-friendly without extra infrastructure:**
  ElevenLabs as level 2 means the cloud VPS deployment "just
  works" without needing a proxy, Tailscale, or any IP-changing
  setup. The cost ceiling on a YouTube-only workload is $0.22/hr.
- **Graceful degradation:** even with no API keys at all, captions
  give the agent something to work with.
- **No silent quality degradation:** the agent sees `method` and
  `failed_attempts` and can warn the user when results come from
  CC instead of a real STT provider.

### What this rules out

- We are NOT building a cost-minimiser that requires the operator
  to maintain residential proxies, cookies, PO tokens, or Tailscale
  topologies. Those are valid optimisations but they push complexity
  onto the operator. The 3-level chain ships working in cloud out
  of the box.
- We are NOT exposing the provider choice as a tool argument. The
  chain runs deterministically. If the operator wants to force a
  specific provider, they can do so by omitting the other API keys
  at the env-var level. Surface area stays at one tool, one knob
  (language).

## Import the engine, do not subprocess it

`transcription_v4` is vendored under `src/transcription_v4/` and
imported directly. The previous attempt invoked the `transcribe-v4`
CLI as a subprocess and parsed stdout to find the run directory.
That:

- threw away typed errors,
- forced fragile string parsing of CLI output,
- reloaded the Whisper model per job,
- doubled the testing surface.

Importing is simpler, faster, and gives the MCP access to all the
structured information v4 produces.

## No SQLite job store, no DDD layers, no `publish` tool

The MCP SDK is the adapter layer. Underneath, one small pipeline
module calls v4. There is no `application/`, `infrastructure/`,
`adapters/`, `domain/` packaging — that surface only makes sense at
scale.

There is also no `publish` tool: the agent IS the publisher (it
delivers the transcript text back to the chat). The MCP returns
content, not workflow.

## No multi-tenancy hooks

`tenant_id` parameters were sprinkled throughout the previous attempt
for "future multi-tenant support". This is a personal tool. Adding the
parameter later is mechanical; carrying it now adds noise.

## No `cleanup` / `cancel` exposed as MCP tools

Operations like cleanup are not agent decisions. The MVP simply omits
cleanup; manual housekeeping of the workspace dir is the recovery path.
A future iteration may add a background sweeper.

## Why Groq for the audio fallback

- Fast: `whisper-large-v3-turbo` returns in under 60 seconds for short
  videos.
- Free tier available; no GPU required on the host.
- Returns word timestamps; v4's `GroqProvider` already normalises them.
- The 25 MB upload limit is handled by v4's chunking + merge for free.

When a need appears for local-only transcription (privacy) or larger
direct uploads (ElevenLabs accepts 3 GB), the v4 abstraction already
covers it — add a `provider` argument to the tool, dispatch.

## Anti-patterns to watch for in future iterations

Three failure modes to recognise if they reappear:

1. **Enterprise vocabulary for personal scale.** SQLite registries,
   manifest checksums, multi-tenant hooks, 12-state workflow
   machines — these are tools for high-concurrency multi-tenant
   systems. Match the design to the load.

2. **Hexagonal architecture inside an MCP server.** The SDK is the
   adapter layer. Anything beyond one or two thin domain modules is
   premature.

3. **Operational tools as MCP tools.** If something should be done by
   a cron job or an operator, it does not belong as an MCP tool.
   Agents are tool consumers, not service administrators.
