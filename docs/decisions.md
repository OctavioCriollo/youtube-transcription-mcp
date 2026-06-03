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

> **Superseded (2026-06-03) for the production OpenClaw deployment.** The uvx
> child-process model was demoted to a local/dev option. Production now runs the
> containerized streamable-http model in a separate stack — see
> "Production deployment: containerized streamable-http" below for why.

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

## Corrective: Groq word/segment alignment

2026-05-31 corrective after a real YouTube run:

- Groq returned HTTP 200, transcript text, provider `segments`, and
  top-level `words` with timestamps.
- The MCP incorrectly treated Groq as failed because the old normalizer
  assigned a word to a segment only when `segment.start <= word.start <
  segment.end`.
- Real Groq payloads can have words that cross segment boundaries, for
  example a word that starts just before `segment.start` but overlaps
  that segment. Those words were dropped from the segment.
- `SubtitleBuilder` then rejected the transcript because some segments
  had no aligned words, even though Groq had returned word timestamps.

The fix keeps Groq `words` as the timing source of truth:

- assign each word to exactly one segment by temporal overlap;
- if a word overlaps multiple segments, use the segment with the largest
  overlap and break ties by midpoint distance;
- use a small tolerance for near-boundary timestamp jitter;
- if provider segment alignment would still lose words or leave text
  segments without words, rebuild segments from Groq words instead of
  estimating subtitles globally.

Do not solve this by enabling `allow_estimated_subtitles=True` globally.
Estimated subtitles are a fallback for missing word timestamps, not a
replacement for real Groq word timestamps.

## Corrective: Groq words are canonical text when available

2026-06-01 corrective after a 30-minute YouTube live transcription:

- The run completed with Groq and generated transcript, SRT, VTT, canonical,
  quality, and audit artifacts.
- `subtitle_token_parity` failed because `transcript.txt` was built from
  `segment.text`, while SRT/VTT were built from `segment.words`.
- The affected tokens were mostly accented Spanish words. The segment text
  contained degraded forms such as `Tendr`, `decisi`, `hincapi`, and `memor`,
  while the word-level entries contained `Tendrá`, `decisión`, `hincapié`, and
  `memorándum`.
- The raw Groq payload is not persisted, so the project should not assert that
  Groq itself returned bad segment text. The MCP-owned failure is that the
  normalizer trusted segment text even when better word-level text existed.

The fix makes Groq word timestamps the canonical text source too:

- provider segment start/end/text are still used to assign words to segments;
- once words are assigned, `Segment.text` is rebuilt with
  `smart_join(word.text for word in segment.words)`;
- when a segment has words, its start/end are reset to the first/last word
  timestamps;
- provider segment text remains only a fallback when word timestamps are absent.

This keeps `canonical.json`, `transcript.txt`, SRT, VTT, and audit checks on the
same text source. It is intentionally not a Spanish-accent repair rule; it fixes
the broader normalization invariant.

## Production async transcription jobs

Long videos can take several minutes because the MCP may need to download
audio, split it into remote-size chunks, transcribe each chunk, merge the
canonical transcript, and generate subtitle artifacts. A single blocking MCP
tool call gives the client no reliable visibility during that work.

The production path uses a persistent job model:

- `start_youtube_transcription` writes `mcp-jobs/<run_id>/request.json`,
  starts a separate Python worker process, and returns `run_id` immediately.
- `get_transcription_status` reads `mcp-jobs/<run_id>/job.json` and, when
  available, enriches it with v4 `run-state.json` / chunk progress.
- `get_transcription_result` reads `result.json` only after completion.
- `cancel_transcription` terminates the worker process tree on a best-effort
  basis and marks the job canceled.

This design deliberately does not rely only on MCP progress notifications:
client support varies, while persisted state is visible to any MCP client and
survives a silent or long-running worker. `notifications/progress` can still be
added later as a convenience layer, but persisted polling is the contract.

## LLM self-guidance contract

The MCP should be usable by an LLM client that has no prior conversation
context. Tool descriptions alone are not enough for long jobs because the
client must know what to tell the user, when to poll, and when to stop polling.

Async job responses therefore include:

- `user_visible_message`: concise text that is safe to show directly to the
  user while the job is running or after it reaches a terminal state.
- `recommended_next_tool`: the next MCP tool the agent should call, or `null`
  when the response is terminal and no tool call is required.
- `recommended_poll_seconds`: a conservative polling delay for queued/running
  jobs.
- `agent_instructions`: compact operational guidance for the LLM.
- `progress_percent`, `available_next_actions`, and `recommended_artifacts`
  when those values are meaningful.

The server also registers a prompt named `transcribe_with_progress` so clients
with prompt support can request the recommended async workflow explicitly.

This does not replace the persistent job contract and does not guarantee UI
progress in every MCP client. It is advisory metadata: clients that ignore
custom fields or prompts may still behave silently. The robust production path
remains start/status/result polling backed by persisted job files.

## Workspace directory defaults

The MCP stores mutable runtime data separately from the package cache used by
`uvx`. Package installation belongs to `uv`; transcription jobs, downloads,
artifacts, and cache entries belong to the MCP workspace.

`WORKSPACE_DIR` is the explicit operator override and is the right choice for
Docker volumes, remote servers, and deployments that need predictable storage.
When it is omitted, the MCP uses per-user operating-system defaults:

- Windows: `%LOCALAPPDATA%\transcription-mcp\workspace`, falling back to
  `%APPDATA%` and then the user home only if needed.
- macOS: `~/Library/Application Support/transcription-mcp/workspace`.
- Linux: `$XDG_STATE_HOME/transcription-mcp/workspace`, falling back to
  `~/.local/state/transcription-mcp/workspace`.

Do not implicitly probe `/workspace` in application code. In Docker that path is
fine, but the Dockerfile already sets `WORKSPACE_DIR=/workspace`. On Windows,
`Path("/workspace")` resolves to a drive-root path, which is surprising and can
create data outside the user's normal application area.

## Production hardening roadmap implementation

The MCP layer exposes production features without changing the default path:

- Completed v4 runs are treated as the cache. The MCP searches completed runs
  in `v4-storage`, validates provider/order/language/diarization criteria, and
  returns a cache hit only while `MCP_CACHE_TTL_HOURS` is fresh.
- Result payloads expose an artifact manifest instead of embedding every large
  artifact. `get_transcription_artifact` fetches a named text artifact on
  demand.
- Cookies and proxy are host configuration only (`YT_COOKIES_FILE`, `YT_PROXY`).
  They are not MCP tool arguments, so a chat user cannot inject host paths or
  arbitrary proxies.
- Diarization is a normal tool option but is routed only to ElevenLabs. Groq and
  local providers are skipped for diarized requests instead of failing the whole
  chain.
- `local` is supported only through explicit `provider_order`; it is not part of
  the default chain because CPU/GPU runtime is an operational choice.
- Public media URLs and local files use separate tools from YouTube. This keeps
  YouTube captions fallback scoped to YouTube and avoids ambiguous source
  semantics.
- Async workers enforce a simple host-level concurrency limit and clean old job
  records. v4 transcript artifacts are not deleted by that job cleanup.

## Production deployment: containerized streamable-http (supersedes uvx side-car)

2026-06-03, after validating the end-to-end flow on the production VPS.

The walking skeleton shipped with the uvx child-process model as the recommended
distribution. Running it for real against OpenClaw exposed three problems that the
uvx model could not solve cleanly:

- The audio path needs `ffmpeg` and `yt-dlp`. These are not in OpenClaw's gateway
  image. Installing them into the gateway at runtime is lost on container recreate;
  rebuilding the provider image is not our call.
- The uvx registration stored provider API keys inside `openclaw.json`.
- Adding another MCP meant editing OpenClaw's own config/host each time.

Decision: production runs a **prebuilt GHCR image** (`youtube-transcription-mcp`) as
its **own Docker Compose stack** next to OpenClaw, over `streamable-http`. The image
is self-contained (ffmpeg + yt-dlp + engine baked in). The MCP stack and the OpenClaw
gateway share a private network (`openclaw-mcp-network`) and an artifacts volume
(`openclaw_mcp_workspace`), both **owned/created by the OpenClaw stack** (fixed name,
not external) and joined as external by the MCP stack. Adding an MCP is just another
service in the MCP stack plus one `openclaw mcp set`. The uvx model remains documented
as a local/dev option. Full operator procedure lives in the OpenClaw repo.

## Corrective: dropped `VOLUME ["/workspace"]` from the image

2026-06-03. The Dockerfile declared `VOLUME ["/workspace"]` from the walking-skeleton
era. Once production moved `WORKSPACE_DIR` to `/mcp-workspace/transcription-mcp` (a
subdirectory of the shared volume), `/workspace` was unused — but the `VOLUME`
directive still forced Docker to create a throwaway **anonymous volume** for it on
every container (re)create. Those accumulate as orphaned dangling volumes across
redeploys.

Decision: remove the `VOLUME` directive entirely. Persistence is the deployer's job
via the externally-mounted shared volume. `ENV WORKSPACE_DIR=/workspace` stays only as
a harmless standalone default. Verified on the VPS: after the fix + redeploy the MCP
container has a single mount (`/mcp-workspace`), and dangling volumes dropped to 0.

## Bundle delivery and host path rebasing

The agent (OpenClaw) runs in a different container than the MCP, so it cannot reach
the MCP's internal filesystem to send a file to the user. Rather than stream large
artifacts back through the MCP protocol, `create_transcription_bundle` packages a
completed run's artifacts into `<run_dir>/exports/transcription_bundle.zip` (atomic
write) on the **shared** volume and returns two paths: `bundle_path_for_mcp` (MCP view)
and `bundle_path_for_openclaw` (the same file rebased to the host's read-only mount,
computed from `WORKSPACE_DIR` and `OPENCLAW_WORKSPACE_DIR`). The host sends the second.

The bundle is temporary and regenerable; the source of truth stays in `v4-storage`, so
TTL cleanup of bundles loses no data. This keeps the MCP a content provider, not a
delivery/transport service — consistent with "the agent IS the publisher".

## Job liveness: heartbeat, `stale_failed`, and `/health`

Persisted job polling (above) tells a client *what the job said last*, but a worker
that dies or hangs would otherwise leave a job stuck in `running` forever, holding a
concurrency slot. Two additions close that gap:

- The worker writes `heartbeat_at` every ~2s. A non-terminal job with no heartbeat for
  longer than `TRANSCRIPTION_JOB_STALE_SECONDS` is moved to the terminal state
  `stale_failed` and stops counting as active. `TRANSCRIPTION_JOB_TIMEOUT_SECONDS` is a
  hard ceiling.
- A real `GET /health` route reports workspace + job-store reachability and active job
  count. The Docker `HEALTHCHECK` hits it instead of a bare TCP check, so a
  hung-but-listening MCP is reported `unhealthy` and restarted.

This is deliberately host-level liveness, not a distributed scheduler — matching the
personal-scale design.

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
