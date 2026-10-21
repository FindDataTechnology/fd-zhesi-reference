"""可得性探测（zhesi-atlas-resilience 1.1）。

与许可门分开：许可确认是静态事实，可得性是动态事实（design 决策 1）。
探测结果落 `source_status` 表，带 HTTP 状态、超时、内容 sha256 与探测时间——
让「源挂了」与「解析器坏了」在台账上可分。

判定口径：
- 2xx                     → available
- 超时 / DNS / 连接失败     → unavailable，reason=unreachable（timeout 单列）
- 4xx/5xx                 → unavailable，reason=http_error，保留 http_status
- 内容为空                 → unavailable，reason=empty_content（取到了但没有数据）

探测永不抛异常给调用方：不可得是**正常结果**，由调用方据此决定不摄取。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable

from .license import assert_allowed_url

DEFAULT_TIMEOUT = 30.0
UA = "fd-zhesi-reference/0.1"

STATUS_AVAILABLE = "available"
STATUS_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ProbeResult:
    source: str
    url: str
    status: str
    reason: str | None
    checked_at: str
    http_status: int | None = None
    elapsed_ms: int | None = None
    content_sha256: str | None = None
    content_bytes: int | None = None
    detail: str | None = None

    @property
    def available(self) -> bool:
        return self.status == STATUS_AVAILABLE

    def as_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def default_fetch(url: str, timeout: float, proxy: str | None) -> tuple[int, bytes]:
    """默认传输：httpx 直连（延迟导入，离线/测试路径不依赖网络栈）。"""
    import httpx

    kwargs: dict = {
        "headers": {"User-Agent": UA},
        "timeout": timeout,
        "follow_redirects": True,
    }
    if proxy:
        kwargs["proxy"] = proxy
    with httpx.Client(**kwargs) as client:
        resp = client.get(url)
        return resp.status_code, resp.content


def probe(
    source: str,
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    proxy: str | None = None,
    fetch: Callable[[str, float, str | None], tuple[int, bytes]] | None = None,
) -> ProbeResult:
    """探测一个源的可得性。永不抛异常（红线命中的 ForbiddenSource 除外——那是配置错误）。"""
    assert_allowed_url(url)  # 红线守卫：探测也不许打到禁用源
    fetch = fetch or default_fetch
    started = time.monotonic()
    try:
        http_status, body = fetch(url, timeout, proxy)
    except Exception as exc:  # noqa: BLE001 —— 不可得是正常结果，不向上抛
        elapsed = int((time.monotonic() - started) * 1000)
        name = type(exc).__name__.lower()
        reason = "timeout" if "timeout" in name else "unreachable"
        return ProbeResult(
            source=source,
            url=url,
            status=STATUS_UNAVAILABLE,
            reason=reason,
            checked_at=_now(),
            elapsed_ms=elapsed,
            detail=f"{type(exc).__name__}: {exc}",
        )

    elapsed = int((time.monotonic() - started) * 1000)
    digest = _sha256(body or b"")
    size = len(body or b"")
    if not 200 <= http_status < 300:
        return ProbeResult(
            source=source,
            url=url,
            status=STATUS_UNAVAILABLE,
            reason="http_error",
            checked_at=_now(),
            http_status=http_status,
            elapsed_ms=elapsed,
            content_sha256=digest,
            content_bytes=size,
            detail=f"HTTP {http_status}",
        )
    if not size:
        return ProbeResult(
            source=source,
            url=url,
            status=STATUS_UNAVAILABLE,
            reason="empty_content",
            checked_at=_now(),
            http_status=http_status,
            elapsed_ms=elapsed,
            content_sha256=digest,
            content_bytes=0,
            detail="响应体为空（取到了但没有数据）",
        )
    return ProbeResult(
        source=source,
        url=url,
        status=STATUS_AVAILABLE,
        reason=None,
        checked_at=_now(),
        http_status=http_status,
        elapsed_ms=elapsed,
        content_sha256=digest,
        content_bytes=size,
    )


def record(conn, result: ProbeResult) -> None:
    """探测结果写入来源状态表（source_status）。"""
    conn.execute(
        "INSERT OR REPLACE INTO source_status (source, checked_at, status, reason, url,"
        " http_status, elapsed_ms, content_sha256, content_bytes, detail)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            result.source,
            result.checked_at,
            result.status,
            result.reason,
            result.url,
            result.http_status,
            result.elapsed_ms,
            result.content_sha256,
            result.content_bytes,
            result.detail,
        ),
    )
    conn.commit()


def latest(conn, source: str) -> dict | None:
    """该源最近一次探测结果（供 source_status 工具与降级判定读取）。"""
    row = conn.execute(
        "SELECT * FROM source_status WHERE source=? ORDER BY checked_at DESC LIMIT 1",
        (source,),
    ).fetchone()
    if row is None:
        return None
    return {k: row[k] for k in row.keys()} if hasattr(row, "keys") else None
