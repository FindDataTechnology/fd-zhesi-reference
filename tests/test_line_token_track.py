"""词元轨的**接线**（converge-line-identity-and-keys 8.x，本线 `zhesi`）。

原型：`fd-erp-ledger-mcp/tests/test_line_token_track.py`（8.5）。规则层
（`test_line_token.py`）证「这枚票本身合不合法」，本文件证**票真的能走进路由**。
两层必须分开测：规则层全绿而接线层断，是这类改动最典型的假绿。

与 erp 那份的差别只有一处，但是**本仓必须单独钉的一条**：fd-zhesi-reference 的
`_serve_http()` 是靠一个谓词决定「挂不挂中间件」。谓词写成 `configured_token()`
时，**只开词元轨的部署会一个中间件都不挂**——数据面对匿名请求全放行，而日志里
一行异常都没有（同族于 erp `gate_enabled()` 那条注释）。所以这里除了中间件本身，
还要用假 fastmcp 把「分支选对了」也证一次。

三条边界各钉一次（与家族同口径）：
1. 共享钥匙照旧能用（平台代调走的就是它，动它就是动生产）；
2. 词元票能用，且身份真的挂进了 scope（`auth.SCOPE_IDENTITY`）；
3. 两个都没配时，行为与接入前**逐字节一致**。
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from fd_zhesi_reference import auth, wellknown
from fd_zhesi_reference.line_token import ENV_ISSUER, ENV_LINE, ENV_PEM, LINE_NAME
from tests.test_line_token import ISSUER, _PUB, mint

SHARED = "t0ken-family-shape"
DATA_PATH = "/mcp"
LINE_ENVS = (ENV_ISSUER, ENV_LINE, ENV_PEM)


def _run(mw, headers, *, path=DATA_PATH, scope_type="http"):
    """喂一个真 ASGI scope 进去，返回 (status, body, downstream_scope)。

    不装 fastmcp、不起 uvicorn：判的是中间件这一层，与传输层无关。
    """
    seen = {}
    sent = []

    async def downstream(scope, receive, send):
        seen["scope"] = scope
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": scope_type,
        "path": path,
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }
    asyncio.run(mw(downstream)(scope, receive, send))

    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body, seen.get("scope")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(auth.ENV_TOKEN, raising=False)
    for env in LINE_ENVS:
        monkeypatch.delenv(env, raising=False)
    yield


def _enable_line_track(monkeypatch):
    monkeypatch.setenv(ENV_ISSUER, ISSUER)
    monkeypatch.setenv(ENV_LINE, LINE_NAME)
    monkeypatch.setenv(ENV_PEM, _PUB)


def test_gate_enabled_covers_either_track(monkeypatch):
    """门存在 = 共享钥匙**或**词元轨任一被配置；两者都空时不挂门。"""
    assert auth.gate_enabled() is False
    monkeypatch.setenv(auth.ENV_TOKEN, SHARED)
    assert auth.gate_enabled() is True
    monkeypatch.delenv(auth.ENV_TOKEN)
    monkeypatch.setenv(ENV_ISSUER, ISSUER)
    monkeypatch.setenv(ENV_PEM, _PUB)
    assert auth.gate_enabled() is True, "只开词元轨的部署也必须有门"


def test_shared_token_still_works_when_line_track_enabled(monkeypatch):
    """开词元轨不影响回退轨——平台代调走的就是共享钥匙这条。"""
    monkeypatch.setenv(auth.ENV_TOKEN, SHARED)
    _enable_line_track(monkeypatch)
    status, body, _ = _run(auth.BearerAuthMiddleware, {"authorization": f"Bearer {SHARED}"})
    assert (status, body) == (200, b"ok")


def test_line_token_passes_and_identity_reaches_scope(monkeypatch):
    """词元票 → 放行；且身份挂进 `auth.SCOPE_IDENTITY`（不是只判了个签名）。"""
    _enable_line_track(monkeypatch)
    status, _, scope = _run(
        auth.BearerAuthMiddleware,
        {"authorization": f"Bearer {mint(sub='caller-42')}"},
    )
    assert status == 200
    ident = (scope or {}).get(auth.SCOPE_IDENTITY)
    assert ident is not None, "票验过了却没把身份交给下游 = 接线层断"
    assert ident.subject == "caller-42"
    assert ident.can_write is True


def test_bad_and_foreign_tokens_are_401_with_the_same_body(monkeypatch):
    """坏票、别线的票、共享钥匙的近似值，都回**同一句** 401（不解释哪一步没过）。"""
    monkeypatch.setenv(auth.ENV_TOKEN, SHARED)
    _enable_line_track(monkeypatch)
    bodies = []
    for token in ("not-a-jwt", mint(source="wire"), mint(aud="wire"), SHARED + "x"):
        status, body, _ = _run(auth.BearerAuthMiddleware, {"authorization": f"Bearer {token}"})
        assert status == 401, token[:12]
        bodies.append(body)
    assert len(set(bodies)) == 1, "不同的失败原因不该给出不同的响应正文"


def test_unconfigured_is_byte_identical_to_before(monkeypatch):
    """两个都没配 ⇒ 门不存在：数据面与豁免面都不 401（与接入前逐字节一致）。"""
    assert auth.gate_enabled() is False
    for path in (DATA_PATH, *auth.EXEMPT_PATHS):
        status, _, _ = _run(auth.BearerAuthMiddleware, {}, path=path)
        assert status == 200, path


def test_exempt_paths_survive_with_line_track_only(monkeypatch):
    """只开词元轨时，探针与自描述端点仍匿名可读（门不能把它们一起关掉）。"""
    _enable_line_track(monkeypatch)
    for path in ("/health", wellknown.WELL_KNOWN_PATH):
        assert _run(auth.BearerAuthMiddleware, {}, path=path)[0] == 200, path
    assert _run(auth.BearerAuthMiddleware, {})[0] == 401


def test_serve_mounts_gate_with_line_track_only(monkeypatch):
    """**本仓最容易错的一处**：只配词元轨时 `cli.serve()` 也必须挂中间件。

    谓词写成 `configured_token()` 时，只开词元轨的部署（客户拿 `sk-` 换票直调、
    不共用线钥匙）会一个中间件都不挂——数据面对匿名请求全放行，而日志里一行
    异常都没有。用假 server/uvicorn 观测分支：不真起 uvicorn，只证明
    「分支选对了、中间件挂上了」。
    """
    from fd_zhesi_reference import cli as cli_mod

    calls = {}

    class _Middleware:
        def __init__(self, cls, *args, **kwargs):
            self.cls = cls

    class FakeFastMCP:
        def custom_route(self, *args, **kwargs):
            return lambda fn: fn

        def http_app(self, **kwargs):
            calls["http_app"] = kwargs
            # serve() 之后还要 app.routes.append(Route("/healthz", …))，
            # 所以假 app 必须有 routes 列表（返回裸字符串会 AttributeError）
            return type("FakeASGIApp", (), {"routes": []})()

    monkeypatch.setattr(cli_mod, "create_server", lambda *a, **kw: FakeFastMCP())
    monkeypatch.setattr(cli_mod, "SERVER_NAME", "fake")

    fake_cfg = lambda *a, **kw: ("CONFIG", kw)
    fake_server = type("Server", (), {})
    fake_server.__init__ = lambda self, cfg, *a, **kw: None

    async def _serve(self):
        calls["served"] = True

    fake_server.serve = _serve
    monkeypatch.setattr("uvicorn.Config", fake_cfg)
    monkeypatch.setattr("uvicorn.Server", fake_server)

    _enable_line_track(monkeypatch)  # 只开词元轨，**不设** MCP_BEARER_TOKEN
    # `serve` 是 click 命令对象，直调会先去解析 argv；取它的 callback
    cli_mod.serve.callback(host="127.0.0.1", port=9312)

    assert "http_app" in calls, "只开词元轨却没挂牌 = 数据面全裸而日志无声"
    middleware = calls["http_app"].get("middleware") or []
    assert len(middleware) == 1 and middleware[0].cls is auth.BearerAuthMiddleware
    assert calls["http_app"]["path"] == "/mcp"
    assert calls["served"] is True


