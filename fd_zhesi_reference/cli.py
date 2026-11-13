"""CLI：serve（HTTP 面 = /mcp + /healthz）。"""

from __future__ import annotations

import asyncio

import click
import uvicorn

from . import __version__
from .server import SERVER_NAME, create_server


@click.group()
@click.version_option(__version__)
def cli() -> None:
    """fd-zhesi-reference：哲思参照系 MCP。"""


@cli.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8000, show_default=True)
def serve(host: str, port: int) -> None:
    """启动 HTTP 服务（/mcp + /healthz）。"""
    from . import wellknown
    from .auth import BearerAuthMiddleware, configured_token

    middleware = None
    if configured_token():
        # 配了 token 才挂共享 bearer 门（change converge-line-identity-and-keys 8.6）；
        # 未配 = 与接入前逐字节一致（不挂任何中间件）。门对 /healthz 与线自描述
        # 端点精确豁免（豁免表在 auth.EXEMPT_PATHS，与端点常量同一处来源）。
        from starlette.middleware import Middleware

        middleware = [Middleware(BearerAuthMiddleware)]

    mcp = create_server()
    # 线自描述端点必须在 `http_app()` 之前挂：它走 FastMCP 的 custom_route，
    # 路由属于应用本身，配/未配令牌两个分支都能命中。
    wellknown.register(mcp)
    app = mcp.http_app(path="/mcp", middleware=middleware)

    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse(
            {"status": "ok", "service": SERVER_NAME, "version": __version__}
        )

    app.routes.append(Route("/healthz", healthz))

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    asyncio.run(uvicorn.Server(config).serve())


if __name__ == "__main__":
    cli()
