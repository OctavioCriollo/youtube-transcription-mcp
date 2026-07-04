# Roadmap: designed but not yet implemented

This document captures corrective items that were designed during the
July 2026 hardening effort (items 1-4, shipped in commits `c9a4922`,
`2b44c84`, `17a3c06`, `c21516f`) but deliberately deferred. Each section
contains enough design detail to implement without re-deriving the analysis.

> **Item 8 — Remote human-in-the-loop YouTube login: SHIPPED (v0.4.0).**
> The datacenter "Sign in to confirm you're not a bot" wall blocks the Groq
> download before PO tokens matter; only a real session cookie clears it. The
> `authgate/` service now mints those cookies via a human login in a
> server-side browser (noVNC over Traefik + ForwardAuth), drops them on the
> shared volume with a sliding 24h idle TTL, and the MCP consumes them
> automatically and guides the agent to `request_youtube_login` when a job is
> bot-walled. See `authgate/README.md`. Items 5 (residential proxy) and 6
> (captions Data API) remain as complementary tiers.

Context for all items: the Groq tier depends on downloading audio with yt-dlp,
which datacenter IPs struggle with. Items 1-4 addressed extractor freshness
(nightly auto-update), PO tokens (sidecar), error classification (bounded
same-tier retries), and a per-provider circuit breaker with `/health`
observability. The items below are the remaining tiers of defense plus one
efficiency win.

---

## Item 5 — Residential proxy pool as tier 1b (optional)

**Problem.** Even with PO tokens and client rotation, some datacenter IP
ranges are blocked outright. A residential proxy is the only ~100% guarantee
from cloud hosts.

**Design.**
- Extend the existing `YT_PROXY` support to accept a comma-separated pool:
  `YT_PROXY_POOL=http://u:p@r1.example:8080,http://u:p@r2.example:8080`.
- Selection: rotate per attempt (round-robin or random), not per process, so
  a burned proxy does not pin the whole deployment.
- Chain placement: tier 1b. First attempt is direct (with PO token). If the
  error classifies as BLOCKED (see `retry_policy.py`), retry once through a
  proxy from the pool **before** recording the blocked failure that feeds the
  circuit breaker and before escalating to ElevenLabs.
- Cost rationale: audio-only opus for 1h of video is ~20-30 MB. Residential
  bandwidth at typical market rates costs cents; an ElevenLabs run for the
  same hour costs dollars. The proxy retry is nearly always cheaper than the
  escalation it prevents.
- Keep it opt-in: unset pool = current behavior, zero new dependencies.

**Implementation sketch.**
- `youtube.py`: `YtDlpYoutubeDownloader` gains `proxy_pool: Sequence[str]`
  (env fallback `YT_PROXY_POOL`, same pattern as `YT_PLAYER_CLIENTS`), plus a
  `download_audio(..., use_proxy: bool)` knob or a `with_proxy()` clone.
- `pipeline.py` chain: inside the retry loop, on first BLOCKED error, if a
  pool is configured and this attempt was direct, retry once with
  `use_proxy=True` (does not consume the transient/rate-limit retry budget).
- Tests: classification-driven, mirror `tests/test_retry_policy.py`
  `TestChainBehavior` (fake engine raising 403 on direct, succeeding via
  proxy; assert one direct + one proxied call, no escalation).

---

## Item 6 — Captions tier via the official YouTube Data API

**Problem.** The captions fallback uses `youtube-transcript-api`, which
scrapes and therefore suffers the same datacenter-IP blocking as yt-dlp. The
last-resort tier should not share a failure mode with the first tier.

**Design.**
- Use YouTube Data API v3 with an API key (free tier quota is generous for
  caption listing). Two relevant endpoints: `captions.list` (which tracks
  exist, which languages, auto vs manual) and `captions.download`.
- **Important limitation discovered during design:** `captions.download`
  requires OAuth from the *video owner* for most videos; an API key alone
  cannot download third-party captions. Therefore the realistic architecture
  is hybrid:
  1. `captions.list` via API key: cheap, unblockable discovery of whether
     captions exist and in which languages (avoids wasted scraping attempts).
  2. Actual caption text still comes from the scraping path or from yt-dlp's
     `--write-subs` (which benefits from items 1-2: PO tokens + fresh
     extractor apply to subtitle downloads too).
- So the practical item is: (a) add `YT_DATA_API_KEY`; (b) use `captions.list`
  as a pre-check to skip the subtitles tier instantly when no captions exist;
  (c) route subtitle *download* through yt-dlp (PO-token-aware) instead of
  `youtube-transcript-api`, keeping the latter as final fallback.

**Implementation sketch.**
- New module `transcription_mcp/youtube_data_api.py` with `list_captions()`
  (httpx, 5s timeout, quota-error → classify as RATE_LIMITED).
- `youtube_subtitles.py`: try yt-dlp subtitle download first when a downloader
  is configured; fall back to `youtube-transcript-api`.
- Tests: mock httpx responses for list; assert tier skip when no captions.

---

## Item 7 — Parallel chunk transcription (efficiency)

**Problem.** Long videos are transcribed sequentially chunk by chunk; wall
time grows linearly with duration.

**Design.**
- The chunking layer already exists (`transcription_engine/chunking.py`).
  Add a bounded worker pool (`concurrent.futures.ThreadPoolExecutor`,
  provider I/O-bound so threads suffice) with `MCP_CHUNK_CONCURRENCY`
  (default 3, conservative vs Groq rate limits).
- Interaction with item 3: a 429 inside a chunk should pause the *pool*
  (shared token-bucket / semaphore backoff), not fail the job — otherwise N
  parallel chunks turn one rate limit into N failures.
- Ordering: chunks return out of order; reassembly must sort by chunk index
  and re-offset timestamps (the sequential code already offsets; keep that
  logic, feed it results sorted by index).
- Resume/checkpointing (`resume=True`) must remain correct: mark a chunk
  complete only after its artifact is written.

**Risks.** Rate-limit amplification (mitigated by shared backoff), memory
(N chunks in flight), and subtle timestamp regressions — this is the item
that most needs the test suite run before merge.

---

## Suggested order if resumed

5 (only if PO tokens prove insufficient in production — check `/health`
provider stats first), then 6, then 7. Item 5 and 6 are independent; item 7
touches the engine core and should be done alone, with no other change in the
same commit.
