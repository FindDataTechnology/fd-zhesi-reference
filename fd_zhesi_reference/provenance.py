"""溯源契约：每个结果携带 provenance（source / stable_id / as_of / attribution / snapshot_version）。

Atlas 数据为 CC BY 4.0，attribution 必须随结果下发（docs/data-license.md）。
缺失 provenance 即 bug，不做降级——对齐 fd-legal-search-mcp 的 lex-mcp-surface 契约。

`snapshot_version`（zhesi-atlas-resilience 1.4）：静态数据集结果回到具体快照
（数据集版本 + sha256 前 12 位），使「同一查询在不同时间给出不同结果」可解释。
动态/按需层（SEP 缓存、Wikidata）无快照，该字段为 None。

本模块是**引用型 URL 的唯一落点**（compliance.py 的红线守卫按此判定）：
外部站点链接只作为署名/引用文本出现，绝不作为请求目标。
"""

from __future__ import annotations

from datetime import date

# 引用型 URL（只出现在 attribution / 链接文本里，不得作为 HTTP 请求目标）
CITATION_URLS = {
    "philosophy-atlas": "https://philosophyatlas.com.br",
    "sep": "https://plato.stanford.edu",
    "wikidata": "https://www.wikidata.org",
    "philpapers-survey": "https://philpapers.org/surveys",
    "philpapers-openalex": "https://api.openalex.org/sources/S4306402130",
}

ATTRIBUTIONS = {
    "philosophy-atlas": f"Philosophy Atlas (MBGuedes), CC BY 4.0, {CITATION_URLS['philosophy-atlas']}",
    "sep": f"Stanford Encyclopedia of Philosophy, {CITATION_URLS['sep']} (link-only citation)",
    "wikidata": f"Wikidata (CC0), {CITATION_URLS['wikidata']}",
    "philpapers-survey": (
        f"PhilPapers Surveys (2009/2020), {CITATION_URLS['philpapers-survey']}"
    ),
    # 论文元数据通道：经 OpenAlex 的 PhilPapers 源（S4306402130）取，OpenAlex 数据 CC0
    "philpapers-openalex": (
        "PhilPapers source S4306402130 via OpenAlex (CC0),"
        f" {CITATION_URLS['philpapers-openalex']}"
    ),
    "philosophy-atlas-seed": (
        "Philosophy Atlas seed snapshot (MBGuedes), CC BY 4.0,"
        f" {CITATION_URLS['philosophy-atlas']}"
    ),
}


def provenance(
    source: str,
    stable_id: str,
    as_of: str | None = None,
    snapshot_version: str | None = None,
) -> dict:
    return {
        "source": source,
        "stable_id": stable_id,
        "as_of": as_of or date.today().isoformat(),
        "attribution": ATTRIBUTIONS.get(source, source),
        "snapshot_version": snapshot_version,
    }
