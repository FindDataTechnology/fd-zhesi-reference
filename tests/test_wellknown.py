"""线自描述端点 ``/.well-known/line-manifest.json`` 的判据（change converge-line-identity-and-keys 1.4）。

判据都对着**这个端点自己**，不拿邻近信号冒充：

1. 成功响应与仓根 ``line-manifest.json`` **逐字节相同**——端点是投影，不是第二真源；
2. ``content-type`` 必须是 JSON：本线有 SPA/网页面时，一个 200 的 HTML 是假绿
   （2026-11-13 实测：本线未挂端点时，公网那条路径返回的是网关的 889 字节 SPA 壳）；
3. ``Cache-Control`` 只在 200 上出现，别把"暂时没有"缓存成长期事实；
4. 路径在门的 ``EXEMPT_PATHS`` 里，且 ``register()`` 挂的是**同一个常量**——
   路由与豁免是两处落点，缺一处就是「配了令牌的线仍对匿名发现请求回 401」；
5. ``LINE_MANIFEST_PATH`` 显式指路时**一项即封顶**；
6. 清单缺失 = 404、存在但坏 = 500，两者不能混（404 会让调用方以为"本来就没发布"）；
7. 真装配：匿名 GET 端点 200 且字节相同，而 ``/mcp`` 仍 401（豁免没把门放漏）。
"""
from __future__ import annotations

import json
import pathlib

import pytest

from fd_zhesi_reference import wellknown

#: 仓根（本仓真源）：tests/ 在仓根下，故 parents[1]。
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_MANIFEST = REPO_ROOT / "line-manifest.json"


def test_serves_repo_manifest_byte_for_byte():
    assert REPO_MANIFEST.exists(), f"仓内真源不见了：{REPO_MANIFEST}"
    status, body, headers = wellknown.manifest_response()
    assert status == 200
    assert body == REPO_MANIFEST.read_bytes()
    assert json.loads(body)["line"]


def test_success_carries_cache_control_only_on_200():
    status, _, headers = wellknown.manifest_response()
    assert status == 200
    assert headers == {"cache-control": "public, max-age=300"}


def test_path_is_exempt_from_the_token_guard():
    """路由与豁免必须成对；这里钉住豁免那一半（常量只有一个来源）。"""
    from fd_zhesi_reference import auth

    assert wellknown.WELL_KNOWN_PATH in auth.EXEMPT_PATHS
    assert "/mcp" not in auth.EXEMPT_PATHS  # 豁免没有把门放漏
    # 健康面豁免不能被这次改动吃掉（探针永远不带钥匙）
    assert "/health" in auth.EXEMPT_PATHS and "/healthz" in auth.EXEMPT_PATHS


def test_register_mounts_exactly_that_path():
    class _FakeMCP:
        def __init__(self):
            self.routes: list[str] = []

        def custom_route(self, path, methods=None):
            def deco(fn):
                self.routes.append(path)
                return fn

            return deco

    mcp = _FakeMCP()
    wellknown.register(mcp)
    assert mcp.routes == [wellknown.WELL_KNOWN_PATH]


def test_register_refuses_to_skip_silently():
    """上游把 custom_route 改名时要响亮失败，不能静默少一个端点。"""
    class _NoCustomRoute:
        pass

    with pytest.raises(RuntimeError, match="custom_route"):
        wellknown.register(_NoCustomRoute())


def test_missing_manifest_is_404_not_500(tmp_path, monkeypatch):
    monkeypatch.setenv(wellknown.ENV_MANIFEST_PATH, str(tmp_path / "nope.json"))
    status, body, headers = wellknown.manifest_response()
    assert status == 404
    assert b"not published" in body
    assert "cache-control" not in headers


def test_invalid_json_is_500_not_404(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv(wellknown.ENV_MANIFEST_PATH, str(bad))
    status, body, headers = wellknown.manifest_response()
    assert status == 500
    assert b"not valid JSON" in body
    assert "cache-control" not in headers


def test_env_override_caps_the_candidate_chain(tmp_path, monkeypatch):
    monkeypatch.setenv(wellknown.ENV_MANIFEST_PATH, str(tmp_path / "only.json"))
    (tmp_path / "only.json").write_text('{"line":"probe"}')
    assert wellknown.manifest_candidates() == [tmp_path / "only.json"]
    status, body, _ = wellknown.manifest_response()
    assert status == 200 and json.loads(body) == {"line": "probe"}


def test_candidate_chain_finds_repo_manifest_from_any_cwd(tmp_path, monkeypatch):
    """源码树一档必须命中仓根：镜像里靠 CWD，源码树里靠 parents[N]。"""
    monkeypatch.delenv(wellknown.ENV_MANIFEST_PATH, raising=False)
    monkeypatch.chdir(tmp_path)
    assert wellknown.manifest_path() == REPO_MANIFEST


@pytest.mark.parametrize("token", ["", "t0ken"])
def test_route_is_anonymous_behind_the_token_guard(monkeypatch, token):
    """真装配：配了共享令牌时 /mcp 401，而自描述端点仍匿名 200 且字节相同。

    按 ``cli.serve()`` 的**同一顺序**拼应用（``register(mcp)`` 在 ``http_app()`` 之前，
    门作为 middleware 传给 ``http_app``），而不是调 ``serve()``：后者会读环境、绑端口
    并阻塞。用的是同一个 ``wellknown.register`` 与同一个门类，所以"匿名可达"是被真门
    验过的。
    """
    pytest.importorskip("fastmcp")
    from starlette.middleware import Middleware
    from starlette.testclient import TestClient

    from fd_zhesi_reference import wellknown as wk
    from fd_zhesi_reference.auth import BearerAuthMiddleware
    from fd_zhesi_reference.config import Settings
    from fd_zhesi_reference.server import create_server

    monkeypatch.setenv("MCP_BEARER_TOKEN", token)

    mcp = create_server(Settings(db_path=":memory:"))
    wk.register(mcp)
    middleware = [Middleware(BearerAuthMiddleware)] if token else None
    app = mcp.http_app(path="/mcp", middleware=middleware)
    client = TestClient(app)

    res = client.get(wk.WELL_KNOWN_PATH)
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("application/json")
    assert res.content == REPO_MANIFEST.read_bytes()
    assert res.headers["cache-control"] == "public, max-age=300"

    if token:
        # 门本身没被这条豁免削弱：其余路径照样 401。
        assert client.get("/mcp").status_code == 401
