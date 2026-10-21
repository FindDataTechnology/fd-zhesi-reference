"""PhilPapers Survey 立场分布工具（task 2.5 接线；zhesi-atlas-resilience 3.2 分路）。

**独立加载、独立 provenance、独立失败口径**（design 决策 4）：
Survey 立场分布只来自 stances 表（由 scripts/ingest_survey.py 从快照/镜像产物
入库），与论文元数据通道（tools_papers，经 OpenAlex 的 PhilPapers 源）完全分离。
两者不混用、不互相兜底——论文元数据不能替代立场分布；Survey 缺失时本工具
返回空结果并附原因，绝不用论文元数据填出「看似有数据」的结果。

口径随行（tabulation + respondents + 年份），2009 为初步波口径（见 docs/survey-probe.md）。
"""

from __future__ import annotations

import sqlite3

from fastmcp import FastMCP

from .config import Settings
from .provenance import provenance
from .schema import init

SOURCE = "philpapers-survey"

# 分路声明（测试与审计读此常量，防止后人把论文元数据接成兜底）
PAPER_METADATA_FALLBACK = False


def _rows(conn: sqlite3.Connection, question: str) -> list[sqlite3.Row]:
    q = question.strip().casefold()
    return conn.execute(
        "SELECT survey_round, question, option, option_zh, pct, category,"
        " tabulation, respondents, retrieved_at, snapshot_version FROM stances"
        " WHERE lower(question) LIKE ? ORDER BY survey_round, pct DESC",
        (f"%{q}%",),
    ).fetchall()


async def stance_distribution(question: str, settings: Settings | None = None) -> dict:
    """立场分布：经典立场问题上职业哲学家的立场分布（带轮次年份与口径注记）。

    Survey 未摄取 → `reason=not_ingested`（附 hint 说明不以论文元数据兜底）；
    问题不在 Survey 内 → `reason=not_indexed`。
    """
    settings = settings or Settings.from_env()
    conn = settings.connect()
    try:
        init(conn)
        total = conn.execute("SELECT COUNT(*) AS c FROM stances").fetchone()["c"]
        if not total:
            return {
                "results": [],
                "reason": "not_ingested",
                "hint": (
                    "PhilPapers Survey 立场分布未摄取（运行 scripts/ingest_survey.py）。"
                    "该层与论文元数据分路：不以 OpenAlex/PhilPapers 论文元数据兜底。"
                ),
            }

        rows = _rows(conn, question)
        if not rows:
            return {
                "results": [],
                "reason": "not_indexed",
                "hint": f"Survey（{total} 行分布）中无与 {question!r} 匹配的问题",
            }

        by_round: dict[str, dict] = {}
        for r in rows:
            bucket = by_round.setdefault(
                r["survey_round"],
                {
                    "survey_round": r["survey_round"],
                    "question": r["question"],
                    "category": r["category"],
                    "tabulation": r["tabulation"],
                    "respondents": r["respondents"],
                    "as_of": r["retrieved_at"],
                    "options": [],
                    "provenance": provenance(
                        SOURCE,
                        f"survey-{r['survey_round']}:{r['question']}",
                        r["retrieved_at"],
                        r["snapshot_version"],
                    ),
                },
            )
            bucket["options"].append(
                {
                    "label": r["option"],
                    "label_zh": r["option_zh"],  # 通俗释义，未命中为 null（宁缺毋滥）
                    "pct": r["pct"],
                }
            )
        return {"results": list(by_round.values())}
    finally:
        conn.close()


def register(mcp: FastMCP, settings: Settings) -> None:
    """挂工具：闭包绑定 settings（每个 server 实例各自的 DB）。"""

    async def stance_distribution_tool(question: str) -> dict:
        """立场分布：经典立场问题上职业哲学家的立场分布（带轮次年份与口径注记）。
        数据独立来自 PhilPapers Survey（2009/2020），与论文元数据分路、互不兜底；
        结果带 provenance（含轮次与 tabulation 口径）。Survey 未摄取时返回空结果并带
        reason=not_ingested（不以论文元数据替代），问题不在 Survey 内时 reason=not_indexed。"""

        return await stance_distribution(question, settings)

    stance_distribution_tool.__name__ = "stance_distribution"
    mcp.tool(stance_distribution_tool)
