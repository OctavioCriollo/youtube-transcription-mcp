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
corrupts the session. The engine's `print()`-based progress logging is silenced
by always passing `progress=False` to the engine pipeline. Operational
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
   turbo. The vendored engine already had this implemented and used it as the
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

`transcription_engine` is vendored under `src/transcription_engine/` and
imported directly. The previous attempt invoked the engine CLI
CLI as a subprocess and parsed stdout to find the run directory.
That:

- threw away typed errors,
- forced fragile string parsing of CLI output,
- reloaded the Whisper model per job,
- doubled the testing surface.

Importing is simpler, faster, and gives the MCP access to all the
structured information the engine produces.

## No SQLite job store, no DDD layers, no `publish` tool

The MCP SDK is the adapter layer. Underneath, one small pipeline
module calls the engine. There is no `application/`, `infrastructure/`,
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
- Returns word timestamps; the engine's `GroqProvider` already normalises them.
- The 25 MB upload limit is handled by the engine's chunking + merge for free.

When a need appears for local-only transcription (privacy) or larger
direct uploads (ElevenLabs accepts 3 GB), the engine abstraction already
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

> Clarification (2026-06-04): this rule is about the **audio** path (Groq/ElevenLabs),
> where word timestamps exist and must not be masked by estimation. The **subtitles**
> path legitimately uses `allow_estimated_subtitles=True`, because YouTube captions
> genuinely have no word-level timing — see "Subtitles produce a full run" below.

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
  available, enriches it with engine `run-state.json` / chunk progress.
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

- Completed runs are treated as the cache. The MCP searches completed runs
  in `storage`, validates provider/order/language/diarization criteria, and
  returns a cache hit only while `MCP_CACHE_TTL_HOURS` is fresh. Selection is by
  provider priority and excludes the subtitles fallback — see "Cache respects
  priority and never serves subtitles" below.
- Result payloads expose an artifact manifest instead of embedding every large
  artifact. `get_transcription_artifact` fetches a named text artifact on
  demand.
- Cookies and proxy are host configuration only (`YT_COOKIES_FILE`, `YT_PROXY`).
  They are not MCP tool arguments, so a chat user cannot inject host paths or
  arbitrary proxies.
- Diarization is a normal tool option but is routed only to ElevenLabs. Groq and
  local providers are skipped for diarized requests instead of failing the whole
  chain.
- `local` is not part of the default chain because CPU/GPU runtime is an
  operational choice. Since `provider_order` is no longer a public tool argument
  (see "Provider order is server policy" below), `local` is reachable only via
  server config (`MCP_*_PROVIDER_ORDER`) or a debug tool.
- Public media URLs and local files use separate tools from YouTube. This keeps
  YouTube captions fallback scoped to YouTube and avoids ambiguous source
  semantics.
- Async workers enforce a simple host-level concurrency limit and clean old job
  records. Engine transcript artifacts are not deleted by that job cleanup.

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

The bundle is temporary and regenerable; the source of truth stays in `storage`, so
TTL cleanup of bundles loses no data. This keeps the MCP a content provider, not a
delivery/transport service — consistent with "the agent IS the publisher".

## Provider order is server policy; storage dir renamed to `storage`

2026-06-03 (development phase, nothing in production yet, so no migration needed).

**Provider order owned by the server.** `provider_order` was a public argument on all
six transcribe/start tools, letting a client weaken the contract (e.g. forcing
`subtitles` before `groq`). It is now **removed from the public tool schema**. The order
is server policy: defaults stay in `pipeline.py` (YouTube `groq,elevenlabs,subtitles`;
media/file `groq,elevenlabs`) and are overridable per source type via
`MCP_YOUTUBE_PROVIDER_ORDER` / `MCP_MEDIA_PROVIDER_ORDER` / `MCP_FILE_PROVIDER_ORDER`.
`MCP_LOCK_PROVIDER_ORDER` (default true) ignores any future debug-tool override. Every
result now reports `provider_order_effective` so the chosen order is auditable. A future
debug tool (registered only behind an env flag) may re-expose an override for testing.

**Storage dir renamed `v4-storage` -> `storage`.** The old name leaked the vendored
engine version into the runtime layout, bundle paths and docs. A single constant
(`STORAGE_DIR_NAME` in `config.py`) is the source of truth; the dir stays **under
`WORKSPACE_DIR`** (required for bundle path rebasing). Done now, in development, because
renaming is free with no production data to migrate — doing it later would have needed a
legacy-read fallback and a migrator.

**Engine package and progress fields use neutral names.** The vendored engine package is
`src/transcription_engine/`, and runtime job progress no longer writes or returns
`v4_run_dir`, `v4_status`, `latest_v4_status`, or `v4_*` stages. The MCP-owned contract
uses `engine_run_dir`, `engine_status`, `latest_engine_status`, and `engine_*` stages.
Because this is still development, old `mcp-jobs` data is disposable: delete/recreate
jobs instead of carrying a legacy compatibility reader.

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

## Subtitles produce a full run (blocks are already cues)

2026-06-04 (corrective 5a). The YouTube-captions fallback used to return a flat dict
and never wrote a run, so `create_transcription_bundle` AND `get_transcription_artifact`
failed for subtitle-only results (no `run_dir`, no `artifacts`). Now the subtitles path
persists a normal run via the shared `FilesystemStorage.save_run`, producing the same
artifact set as Groq/ElevenLabs (transcript, timestamps, SRT/VTT, canonical, quality,
audit) under `storage/`.

Key design point: YouTube delivers captions **already segmented into timed blocks**,
which is the equivalent of the word->cue grouping the audio providers need
`SubtitleBuilder` for. So each block maps **directly** to one canonical `Segment`
(no words) and one `SubtitleCue` (`wrap_lines` for line length) — **no word estimation,
no re-grouping**. Because cues and transcript come from the same block text, token
parity passes naturally.

Timestamps are caption-level: `timestamp_level=caption`, `word_timestamps=false`. The
run is built with `allow_estimated_subtitles=True` so the missing word timestamps are a
**warning**, not an error. Promoting this to a "pass" via a caption-aware quality branch
is deferred (corrective 5b); `warning` is the honest status for now. Cue-timing
normalization (merging ultra-short / overlapping auto-caption blocks) is also 5b.

Because subtitle runs are now real runs, they are cacheable like any other provider —
which makes the cache-priority hardening (corrective 4: select by priority, not mtime)
the recommended next step so a cached subtitles run never shadows a now-working Groq.

## Cache respects priority and never serves subtitles (corrective 4)

2026-06-04. The run cache used to return the **most recently written** run whose
provider was anywhere in the order. That breaks priority: e.g. with
`groq,elevenlabs,subtitles`, a cached `subtitles` run (written on a day Groq was
IP-blocked) would be returned later even when Groq would now succeed. This became
reachable once corrective 5a made subtitle runs real (and thus cacheable).

Two changes in `_read_cached_result_from_runs`, both with **no new metadata**
(provider is already in `run.json` as `transcription_provider`):

1. **Select by priority, not recency.** Collect all valid/fresh/matching candidate
   runs and pick the one whose provider has the **lowest index** in the current
   order (ties broken by most recent). So a cached `groq` run wins over a newer
   `elevenlabs` run.
2. **Subtitles is never served from cache.** The fallback is excluded from cache
   reuse entirely. It is cheap to recompute (one captions API call) and must never
   shadow a real STT provider that may work now. (We do NOT trust a cached run's
   old `failed_attempts`, because those reflect past conditions, not the present.)

Net effect: the cache only saves work when a real-STT result already exists, and
never serves the degraded path when a higher-priority provider could be retried.

## Observable progress via long-poll, not push (corrective 13)

2026-06-04. Problem: after `start_*` returns a `run_id`, an agent that yields sees no
progress until the user sends another message — the job runs but progress is trapped in
the filesystem. MCP `notifications/progress` does not solve this: it is tied to a live
request with a `progressToken`, so it cannot wake an agent that already yielded, and
generic push is not guaranteed across MCP clients.

Decision: expose progress as **durable, observable state** plus a short **long-poll**:

- `job.json` carries a monotonic `revision` that bumps only on a milestone change
  (status or stage), NOT on the 2s heartbeat. So watchers wake on real progress, not
  every tick. `progress`/`message` still ride along in the snapshot.
- `watch_transcription(run_id, since_revision, timeout_seconds)` is an **async** tool
  that blocks until `revision` changes or the job is terminal or `timeout_seconds`
  (capped at 30, default 25, kept under the client's request timeout), then returns the
  same contract as `get_transcription_status` plus `changed` and `terminal`. Async
  (`await anyio.sleep`) so it does not tie up a server thread while waiting.
- The agent loops `watch_transcription` (passing the last `revision`) and shows each
  change, instead of yielding right after `start_*`. `get_transcription_status` stays as
  the instant-snapshot fallback.

`notifications/progress` may still be added later as a best-effort enhancement for
clients that send a `progressToken`, but the reliable contract is the durable
`revision` + long-poll. The agent-side rule ("loop watch_transcription, do not yield")
is an agent instruction (configured separately), not part of the MCP.

## Reproducible installs via uv.lock (corrective 14)

2026-06-04. The project keeps flexible dependency floors in `pyproject.toml`, but commits
`uv.lock` and uses `uv sync --frozen` in CI and Docker. This gives us both:

- human-maintained dependency policy in `pyproject.toml` (`mcp>=1.27.2`, etc.);
- exact, hash-backed dependency resolution in production builds (`uv.lock`).

The publish workflow first runs:

```bash
uv sync --frozen --extra dev
uv run --frozen ruff check .
uv run --frozen python -m pip check
uv run --frozen pytest
```

The Docker image copies `pyproject.toml` + `uv.lock` and installs with:

```bash
uv sync --frozen --no-dev --no-editable --compile-bytecode
```

`--frozen` is intentional: if someone changes dependencies in `pyproject.toml` without
updating `uv.lock`, CI/builds fail instead of silently resolving a new environment.

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
