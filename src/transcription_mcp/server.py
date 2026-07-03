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


def _register_health_route(mcp: FastMCP, cfg: Config) -> None:
    """Add a real /health route used by the container healthcheck.

    A plain TCP probe can pass while the MCP is actually hung. This route does a
    real internal check: the workspace is reachable and the job store can be
    listed. It returns 200 only when those succeed.
    """
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:  # noqa: ANN001
        try:
            from transcription_mcp.jobs import count_active_jobs

            from transcription_mcp.circuit_breaker import snapshot as breaker_snapshot

            cfg.ensure_directories()
            active = count_active_jobs(workspace_dir=cfg.workspace_dir)
            return JSONResponse(
                {
                    "status": "ok",
                    "transport": cfg.transport,
                    "workspace_dir": str(cfg.workspace_dir),
                    "active_jobs": active,
                    # Item 4 observability: per-provider breaker state and
                    # success/failure totals since last workspace reset.
                    "providers": breaker_snapshot(cfg.workspace_dir),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("health check failed: %s", exc)
            return JSONResponse(
                {"status": "error", "error": f"{type(exc).__name__}: {exc}"},
                status_code=503,
            )


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
    _register_health_route(mcp, cfg)
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
