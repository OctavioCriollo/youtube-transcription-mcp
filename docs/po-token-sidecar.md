# PO token sidecar (datacenter-IP hardening)

YouTube requires proof-of-origin (PO) tokens from most datacenter IPs; without
them, downloads fail with 403 errors that look like IP bans. This MCP supports
[bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
as a sidecar that mints those tokens for yt-dlp.

## docker-compose

```yaml
services:
  bgutil-pot:
    image: brainicism/bgutil-ytdlp-pot-provider:latest
    restart: unless-stopped
    # No ports exposed to the host: the MCP reaches it on the compose network.

  transcription-mcp:
    image: ghcr.io/octaviocriollo/youtube-transcription-mcp:latest
    environment:
      YT_POT_PROVIDER_URL: http://bgutil-pot:4416
      YT_PLAYER_CLIENTS: "tv,web_safari,mweb"
    depends_on:
      - bgutil-pot
```

## Environment variables

| Variable | Effect |
| --- | --- |
| `YT_POT_PROVIDER_URL` | Base URL of the sidecar. Unset = plugin stays inert. |
| `YT_PLAYER_CLIENTS` | Comma-separated yt-dlp player clients to try, in order. |

Both apply to the Groq tier only (the yt-dlp download step). ElevenLabs
(`source_url`) and the captions tier are unaffected.

## Verifying

After deploying, run a transcription of a video that previously failed with
403 on the Groq tier and check the job log for the yt-dlp download succeeding
instead of escalating to ElevenLabs.
