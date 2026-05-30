"""FastMCP server entrypoint.

Default transport is stdio so the server can be launched as a child
process by OpenClaw via `uvx --from git+https://... transcription-mcp`.

Set MCP_TRANSPORT=streamable-http to run as a long-lived HTTP service
instead (useful when hosting the MCP on a separate machine and pointing
OpenClaw at its URL — for example, your home PC via Tailscale, so
yt-dlp can use a residential IP).
"""

from __future__ import annotations

import logging
import sys

from mcp.server.fastmcp import FastMCP

from transcription_mcp.config import Config
from transcription_mcp.tools import register_tools


logger = logging.getLogger("transcription_mcp")


def create_server(config: Config | None = None) -> FastMCP:
    cfg = config or Config.from_env()
    cfg.ensure_directories()
    mcp = FastMCP(
        "transcription-mcp",
        host=cfg.host,
        port=cfg.port,
        streamable_http_path=cfg.http_path,
        stateless_http=True,
        json_response=True,
    )
    register_tools(mcp, cfg)
    return mcp


def main() -> None:
    # Logging goes to stderr by default — never to stdout. In stdio
    # transport mode stdout is reserved exclusively for the JSON-RPC
    # protocol and any extra bytes there would corrupt the session.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = Config.from_env()
    if cfg.transport == "stdio":
        logger.info(
            "starting transcription-mcp on stdio (workspace=%s)", cfg.workspace_dir
        )
    else:
        logger.info(
            "starting transcription-mcp on http %s:%d%s (workspace=%s)",
            cfg.host,
            cfg.port,
            cfg.http_path,
            cfg.workspace_dir,
        )
    server = create_server(cfg)
    server.run(transport=cfg.transport)


if __name__ == "__main__":
    main()
