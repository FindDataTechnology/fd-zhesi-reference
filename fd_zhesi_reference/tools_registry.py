"""摄取台账工具（task 2.6）：源状态与缺口，未摄取必带原因。

zhesi-atlas-resilience 1.3：缺口除了「未摄取」，还要能区分**许可未确认**与
**不可得**（reason=license_unconfirmed / unavailable / unreachable / http_error /
timeout），并带上最近一次可得性探测与在用快照版本——否则「源挂了」与
「解析器坏了」在台账上不可分。
"""

from __future__ import annotations

from fastmcp import FastMCP

from .config import Settings
from .schema import init

# 摄取型源基线：未摄取时按此给出缺口原因（spec：缺口可见并注明原因）
SOURCES = {
    "philosophy-atlas": "种子图谱（376 人×六轴+极性关系边），摄取脚本 scripts/ingest_atlas.py",
    "wikidata": "全量层对齐（QID/name_en/name_zh 回填），scripts/ingest_wikidata.py --apply",
    "sep": "概念引用层（on-demand 缓存），concept_lookup 调用时自动建立",
    "philpapers-survey": "立场分布（2009/2020），摄取走 scripts/ingest_survey.py（快照/镜像产物）",
}

# on-demand 查询通道（无批量摄取，故不参与「未摄取」判定，单列报告可得性）
ON_DEMAND = {
    "philpapers-openalex": (
        "论文元数据（经 OpenAlex 的 PhilPapers 源 S4306402130，CC0），"
        "philpapers_paper_search 调用时按需查询"
    ),
}


def _probe_of(conn, source: str) -> dict | None:
    row = conn.execute(
        "SELECT source, checked_at, status, reason, http_status, elapsed_ms,"
        " content_sha256, detail FROM source_status WHERE source=?"
        " ORDER BY checked_at DESC LIMIT 1",
        (source,),
    ).fetchone()
    return {k: row[k] for k in row.keys()} if row else None


def _snapshot_of(conn, dataset: str) -> dict | None:
    row = conn.execute(
        "SELECT snapshot_id, dataset, version, fetched_at, sha256, attribution,"
        " probe_status FROM snapshots WHERE dataset=?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (dataset,),
    ).fetchone()
    return {k: row[k] for k in row.keys()} if row else None


def source_status(settings: Settings | None = None) -> dict:
    """源状态：各外部源的摄取状态与缺口（未摄取必带原因，含许可/可得性口径）。"""
    settings = settings or Settings.from_env()
    conn = settings.connect()
    try:
        init(conn)
        rows = conn.execute(
            "SELECT source, status, detail, as_of FROM ingest_log "
            "WHERE as_of = (SELECT MAX(as_of) FROM ingest_log l2 WHERE l2.source = ingest_log.source)"
        ).fetchall()
        sep_cached = conn.execute("SELECT COUNT(*) AS c FROM concepts").fetchone()["c"]
        probes = {s: _probe_of(conn, s) for s in list(SOURCES) + list(ON_DEMAND)}
        snapshots = {"philosophy-atlas": _snapshot_of(conn, "philosophy-atlas/philosophers")}
    finally:
        conn.close()

    by_source = {r["source"]: r for r in rows}
    results = []
    for source, expectation in SOURCES.items():
        probe = probes.get(source)
        if source == "sep":
            # SEP 是按需缓存层（无批量摄取）：状态由 concepts 缓存行数决定
            results.append(
                {
                    "source": source,
                    "status": "ok" if sep_cached else "not_ingested",
                    "detail": f"on-demand 缓存 {sep_cached} 条" if sep_cached else None,
                    "reason": None if sep_cached else "not_ingested",
                    "probe": probe,
                    "expectation": expectation,
                }
            )
            continue

        row = by_source.get(source)
        if row is None:
            results.append(
                {
                    "source": source,
                    "status": "not_ingested",
                    "reason": "not_ingested",
                    "probe": probe,
                    "expectation": expectation,
                }
            )
            continue

        entry = {
            "source": source,
            "status": row["status"],
            "as_of": row["as_of"],
            "probe": probe,
            "expectation": expectation,
        }
        if row["status"] != "ok":
            # 缺口原因优先取台账 detail 里的 reason（许可/可得性口径），否则用原 detail
            entry["reason"] = _reason_from(row["detail"]) or row["detail"] or row["status"]
        snapshot_entry = snapshots.get(source)
        if snapshot_entry:
            entry["snapshot"] = {
                "snapshot_id": snapshot_entry["snapshot_id"],
                "version": snapshot_entry["version"],
                "fetched_at": snapshot_entry["fetched_at"],
                "sha256": snapshot_entry["sha256"],
                "probe_status": snapshot_entry["probe_status"],
            }
        results.append(entry)

    on_demand = [
        {
            "source": source,
            "status": "on-demand",
            "reason": None,
            "probe": probes.get(source),
            "expectation": expectation,
        }
        for source, expectation in ON_DEMAND.items()
    ]

    return {
        "results": results,
        "on_demand": on_demand,
        "provenance": {
            "source": "fd-zhesi-reference",
            "stable_id": "ingest-log",
            "as_of": max((r["as_of"] for r in rows), default=None),
            "snapshot_version": None,
        },
    }


def _reason_from(detail: str | None) -> str | None:
    """从台账 detail（JSON 字符串）里取 reason 字段。"""
    if not detail:
        return None
    import json

    try:
        parsed = json.loads(detail)
    except (TypeError, ValueError):
        return None
    if isinstance(parsed, dict):
        reason = parsed.get("reason")
        return str(reason) if reason else None
    return None


def register(mcp: FastMCP, settings: Settings) -> None:
    """挂工具：闭包绑定 settings（每个 server 实例各自的 DB）。"""

    async def source_status_tool() -> dict:
        """源状态：各外部源的摄取状态与缺口。未摄取必带 reason（not_ingested /
        license_unconfirmed / unavailable），并附最近一次可得性探测（HTTP 状态、
        耗时、内容 sha256、探测时间）与在用快照版本——让「源挂了」与「解析器坏了」
        可区分。on-demand 通道（论文元数据）单列在 on_demand。"""

        return source_status(settings)

    source_status_tool.__name__ = "source_status"
    mcp.tool(source_status_tool)
