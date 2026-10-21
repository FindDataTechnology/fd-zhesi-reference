"""SEP 引用层工具（task 2.4 接线；zhesi-atlas-resilience 6.2 收紧存储）。

concept_lookup：概念查询。SEP 许可只链不抄——**只存标题、URL 与短引用**
（`MAX_SNIPPET_CHARS` 以内的引用性片段），正文与摘要不落库、不抓取词条正文页。
写入即截断（写时守卫），因此即使解析器将来抓到更长的片段，存储面也不会膨胀成
「条目摘要镜像」。结果带 provenance；空结果必带原因，绝不编造。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from fastmcp import FastMCP

from . import schema
from .config import Settings
from .provenance import provenance
from .sep import (
    DEFAULT_TIMEOUT,
    SepHit,
    SepUnreachable,
    SepUpstream,
    fetch_search_html,
    parse_search_results,
)

CACHE_TTL_DAYS = 30  # 缓存命中窗口：retrieved_at 在 30 天内
DEFAULT_LIMIT = 5  # 默认取前 5 条
MAX_SNIPPET_CHARS = 200  # 短引用上限：超过即截断（只链不抄的存储面守卫）
MAX_TITLE_CHARS = 200  # 标题上限（防异常长标题把存储变成正文）

# register() 时由 server 注入；直接调用（CLI/测试）时回退 from_env()
_settings: Settings | None = None


def _short_quote(text: str, limit: int = MAX_SNIPPET_CHARS) -> str:
    """压空白后按词/字边界截断为短引用（不保留正文），总长不超过 limit。"""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed[: limit - 1]  # 预留一个字符给省略号，保证总长 ≤ limit
    # 尽量在空格或句读处收口，避免半个词
    for sep in (" ", "。", "；", ";", ",", "，"):
        idx = cut.rfind(sep)
        if idx >= (limit - 1) // 2:
            return cut[:idx].rstrip() + "…"
    return cut.rstrip() + "…"



def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_fresh(retrieved_at: str, now: datetime) -> bool:
    try:
        ts = datetime.fromisoformat(retrieved_at)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts >= now - timedelta(days=CACHE_TTL_DAYS)


def _read_cache(conn: sqlite3.Connection, term: str) -> tuple[list[SepHit], str]:
    """读 30 天内的缓存行（rowid 序 = 写入时的排名序）。

    返回 (hits, retrieved_at)；无新鲜行时 hits 为空。
    """
    now = datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT title, url, snippet, retrieved_at FROM concepts WHERE term = ? ORDER BY rowid",
        (term,),
    ).fetchall()
    hits: list[SepHit] = []
    retrieved_at = ""
    for r in rows:  # 同词同批写入共用一个 retrieved_at，取首行即可
        if not _is_fresh(r["retrieved_at"], now):
            continue
        if not retrieved_at:
            retrieved_at = r["retrieved_at"]
        hits.append(SepHit(title=r["title"], url=r["url"], snippet=r["snippet"] or ""))
    return hits, retrieved_at


def _write_cache(conn: sqlite3.Connection, term: str, hits: list[SepHit]) -> str:
    """整词覆写缓存（删旧插新，单事务），返回本次 retrieved_at。

    写时守卫（6.2）：标题与片段一律经 `_short_quote` 截断后才落库——存储面
    只有标题、URL 与短引用，不会因为解析器改动而变成条目正文/摘要的镜像。
    """
    retrieved_at = _now_iso()
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM concepts WHERE term = ?", (term,))
        conn.executemany(
            "INSERT INTO concepts (term, title, url, snippet, retrieved_at)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (
                    term,
                    _short_quote(h.title, MAX_TITLE_CHARS),
                    h.url,
                    _short_quote(h.snippet),
                    retrieved_at,
                )
                for h in hits
            ],
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    return retrieved_at


def storage_policy_violations(conn: sqlite3.Connection) -> list[dict]:
    """存储内容检查：列出违反「只存标题/URL/短引用」的行（应为空）。

    供测试与运维自检使用：任何一行的 title/snippet 超上限即视为存储面失守。
    """
    rows = conn.execute("SELECT term, title, url, snippet FROM concepts").fetchall()
    out: list[dict] = []
    for r in rows:
        title_len = len(r["title"] or "")
        snippet_len = len(r["snippet"] or "")
        if title_len > MAX_TITLE_CHARS or snippet_len > MAX_SNIPPET_CHARS:
            out.append(
                {
                    "term": r["term"],
                    "url": r["url"],
                    "title_len": title_len,
                    "snippet_len": snippet_len,
                }
            )
    return out



def _as_of(hits: list[SepHit], retrieved_at: str) -> dict:
    return provenance("sep", stable_id=hits[0].url, as_of=retrieved_at)


async def concept_lookup(term: str) -> dict:
    """概念查询：哲学词语的定义摘要与 SEP 引用链接（只链不抄，正文不落库）。

    命中 30 天内缓存直接返回；未命中实时查 SEP 搜索端点并写缓存。
    空结果必带原因：unreachable（不可达/超时 8s）/ not_indexed（无 entries 结果）/
    upstream（可达但响应异常）。
    """
    settings = _settings or Settings.from_env()
    query = " ".join(term.split())
    if not query:
        return {"results": [], "reason": "not_indexed"}

    conn = settings.connect()
    try:
        schema.init(conn)
        cached, cached_at = _read_cache(conn, query)
        if cached:
            return {
                "results": [h.as_dict() for h in cached],
                "provenance": _as_of(cached, cached_at),
            }
        try:
            html = await fetch_search_html(query, base_url=settings.sep_base, timeout=DEFAULT_TIMEOUT)
        except SepUnreachable:
            return {"results": [], "reason": "unreachable"}
        except SepUpstream:
            return {"results": [], "reason": "upstream"}
        hits = parse_search_results(html, base_url=settings.sep_base, limit=DEFAULT_LIMIT)
        if not hits:
            return {"results": [], "reason": "not_indexed"}
        # 归一化先于写缓存：返回值与落库内容逐字一致（缓存命中时结果不变）
        hits = [
            SepHit(
                title=_short_quote(h.title, MAX_TITLE_CHARS),
                url=h.url,
                snippet=_short_quote(h.snippet),
            )
            for h in hits
        ]
        retrieved_at = _write_cache(conn, query, hits)
        return {"results": [h.as_dict() for h in hits], "provenance": _as_of(hits, retrieved_at)}
    finally:
        conn.close()


def register(mcp: FastMCP, settings: Settings) -> None:
    global _settings
    _settings = settings
    mcp.tool(concept_lookup)
