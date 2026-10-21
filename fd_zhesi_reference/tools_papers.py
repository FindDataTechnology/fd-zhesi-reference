"""PhilPapers 论文元数据工具（zhesi-atlas-resilience 3.1）。

通道：**OpenAlex 的 PhilPapers 源**（source id `S4306402130`，数据 CC0）——
PhilPapers 站点直连被 Cloudflare 拦截且条款不明（见 license.REDLINES），
故论文元数据只经 OpenAlex 取，绝不直连 philpapers.org。

分路（design 决策 4）：本工具的产物是**论文元数据**（标题/年份/DOI/被引），
不是立场分布。立场分布只走 tools_survey（stances 表），两者互不兜底。

失败口径（spec「被拦截源的兜底与失败口径」）：配额、上游故障、不可达分别给出
`reason`（quota / upstream / unreachable / blocked），空结果**必带原因**，
不静默为空、不冒充「无数据」。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from fastmcp import FastMCP

from .config import Settings
from .license import assert_allowed_url
from .provenance import provenance

SOURCE = "philpapers-openalex"
PHILPAPERS_SOURCE_ID = "S4306402130"
OPENALEX_API = "https://api.openalex.org"
DEFAULT_MAILTO = "ops@finddata.dev"
TIMEOUT = 25.0


class _Failure(RuntimeError):
    """通道失败，携带 spec 口径的 reason。"""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _ua() -> str:
    return f"fd-zhesi-reference/0.1 (mailto:{DEFAULT_MAILTO})"


def _get_json(url: str, timeout: float = TIMEOUT) -> tuple[dict, dict]:
    """GET JSON。测试经 monkeypatch 替换本函数即可离线。失败按口径抛 _Failure。"""
    assert_allowed_url(url)  # 红线守卫：OpenAlex 允许，philpapers.org 直连被拦
    req = urllib.request.Request(
        url, headers={"User-Agent": _ua(), "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
            quota = {
                k.lower(): v
                for k, v in resp.headers.items()
                if k.lower().startswith("x-ratelimit") or k.lower() == "retry-after"
            }
            return payload, quota
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise _Failure("quota", f"OpenAlex rate limit / quota exceeded (HTTP 429)") from exc
        if exc.code == 403:
            raise _Failure("blocked", f"OpenAlex 拒绝请求 (HTTP 403)") from exc
        if 500 <= exc.code < 600:
            raise _Failure("upstream", f"OpenAlex 上游故障 (HTTP {exc.code})") from exc
        raise _Failure("upstream", f"OpenAlex 请求失败 (HTTP {exc.code})") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _Failure("unreachable", f"OpenAlex 不可达: {exc}") from exc


def _work_row(w: dict) -> dict:
    location = w.get("primary_location") if isinstance(w.get("primary_location"), dict) else {}
    venue = (location.get("source") or {}).get("display_name") if location else None
    return {
        "work_id": str(w.get("id", "")).rstrip("/").split("/")[-1],
        "title": w.get("title"),
        "year": w.get("publication_year"),
        "doi": w.get("doi"),
        "cited_by_count": w.get("cited_by_count"),
        "venue": venue,
        "provenance": provenance(
            SOURCE,
            f"openalex:{str(w.get('id', '')).rstrip('/').split('/')[-1]}",
            None,
            None,  # OpenAlex 是动态索引，无静态快照
        ),
    }


def search_papers(query: str, limit: int = 10, settings: Settings | None = None) -> dict:
    """在 PhilPapers 源（经 OpenAlex）内按标题/关键词检索论文元数据。"""
    settings = settings or Settings.from_env()
    q = (query or "").strip()
    if not q:
        return {"results": [], "reason": "empty_query",
                "hint": "query 不能为空（按标题或关键词检索）"}

    params = {
        "search": q,
        "filter": f"primary_location.source.id:{PHILPAPERS_SOURCE_ID}",
        "per-page": str(max(1, min(int(limit or 10), 200))),
        "select": "id,title,publication_year,doi,cited_by_count,primary_location",
    }
    url = f"{OPENALEX_API}/works?" + urllib.parse.urlencode(params)
    try:
        payload, quota = _get_json(url)
    except _Failure as exc:
        # 空结果必带原因：绝不静默为空，也不冒充「无数据」
        return {
            "results": [],
            "reason": exc.reason,
            "hint": exc.detail,
            "channel": {"source": SOURCE, "philpapers_source_id": PHILPAPERS_SOURCE_ID},
        }

    rows = [_work_row(w) for w in payload.get("results") or []]
    if not rows:
        return {
            "results": [],
            "reason": "not_indexed",
            "hint": f"PhilPapers 源（{PHILPAPERS_SOURCE_ID}）未收录与 {q!r} 匹配的论文",
            "channel": {"source": SOURCE, "philpapers_source_id": PHILPAPERS_SOURCE_ID},
        }
    return {
        "total": (payload.get("meta") or {}).get("count"),
        "results": rows,
        "quota": quota,
        "channel": {"source": SOURCE, "philpapers_source_id": PHILPAPERS_SOURCE_ID},
        "attribution": provenance(SOURCE, "openalex-works")["attribution"],
    }


def register(mcp: FastMCP, settings: Settings) -> None:
    """挂工具：闭包绑定 settings。"""

    async def philpapers_paper_search(query: str, limit: int = 10) -> dict:
        """PhilPapers 论文元数据检索（经 OpenAlex 的 PhilPapers 源 S4306402130，数据 CC0）。
        按标题/关键词检索，返回标题、年份、DOI、被引次数与出处，每条带 provenance
        （OpenAlex CC0 + PhilPapers 源署名）。这是**论文元数据**通道，不是立场分布
        （立场分布见 stance_distribution，两者不互相兜底）。失败时空结果必带 reason
        （quota/upstream/unreachable/blocked），不静默为空。"""

        return search_papers(query, limit, settings)

    philpapers_paper_search.__name__ = "philpapers_paper_search"
    mcp.tool(philpapers_paper_search)
