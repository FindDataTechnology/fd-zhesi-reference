"""Atlas 种子图谱摄取器（task 2.2；zhesi-atlas-resilience 1.2/1.3 改为双门摄取）。

从 Philosophy Atlas（https://philosophyatlas.com.br，CC BY 4.0 需署名，
见 docs/data-license.md）拉取三个静态数据文件入库：

  data/raw/philosophers.csv            人物（六轴坐标 / 学派 / 地区 / 影响力分）
  data/raw/influences.csv              影响边（positive / critical / synthetic）
  data/catalogue/topics/topics.json    当代议题目录

摄取流程（design 决策 1，双门）：
  许可检查 → 可得性探测 → 快照落盘（不可变，内容寻址）→ 解析入库

任何一门不过即**整层不摄取**：缺口写进 source_status（reason 明确），
ingest_log 记 failed，本函数返回 status=unavailable 而不抛异常——「源挂了」
是正常结果，不是程序错误。解析器自身的错误（如未知极性）仍然抛错。

特性：
- 幂等：单事务内 DELETE 后重插，可安全重跑。注意 philosophers.wikidata_qid
  由 task 2.3 的 Wikidata 对齐任务负责回填，本脚本 INSERT 时恒置 NULL，
  不写该列的其他语义。
- 快照：解析的输入永远是刚落盘的那份只读快照（不是网络响应），所以同一
  快照重复解析结果一致；每行数据的 snapshot_version 指向具体快照 id。
- 台账：每次运行在 ingest_log 记一行 source='philosophy-atlas'
  （status ok/partial/failed/unavailable + 计数明细），供 source_status 读取。
- 离线：--data-dir 指向含 philosophers.csv / influences.csv / topics.json
  的本地缓存目录（测试与断网用），不发起任何 HTTP 请求；离线输入同样落快照
  （probe_status=available, detail=local-cache）。

用法：
  python3 scripts/ingest_atlas.py                 # 直连拉取（UA fd-zhesi-reference/0.1）
  python3 scripts/ingest_atlas.py --data-dir DIR  # 本地缓存目录
  python3 scripts/ingest_atlas.py --db PATH       # 覆盖 DB 路径（默认 FD_ZHESI_REF_DB）
  python3 scripts/ingest_atlas.py --version V     # 覆盖数据集版本标签
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from atlas_labels import (  # noqa: E402  (scripts/ 目录在 sys.path[0])
    FAMOUS_NAMES,
    REGION_EN,
    REGION_ZH,
    SCHOOL_EN,
    SCHOOL_ZH,
)
from fd_zhesi_reference import license as license_gate  # noqa: E402
from fd_zhesi_reference import probe as availability  # noqa: E402
from fd_zhesi_reference import schema, snapshot  # noqa: E402
from fd_zhesi_reference.config import Settings  # noqa: E402
from fd_zhesi_reference.provenance import ATTRIBUTIONS  # noqa: E402

SOURCE = "philosophy-atlas"
UA = "fd-zhesi-reference/0.1"
# 数据集版本标签：白皮书版本（docs/data-license.md 记录 v2.1 / corpus v8.0）。
# 精确身份由快照 sha256 钉住，标签只用于人读与「同一查询跨时间可解释」。
DEFAULT_VERSION = "whitepaper-v2.1"
AXES = (
    "individualism",
    "materialism",
    "rationalism",
    "determinism",
    "transcendence",
    "universalism",
)
POLARITIES = {"positive", "critical", "synthetic"}  # 白皮书口径
PATHS = {
    "philosophers": "data/raw/philosophers.csv",
    "influences": "data/raw/influences.csv",
    "topics": "data/catalogue/topics/topics.json",
}


PHILOSOPHER_COLUMNS = (
    ("atlas_id", "name_pt", "name_en", "name_zh", "birth_year", "death_year")
    + ("school", "school_en", "school_zh", "region", "region_en", "region_zh")
    + AXES
    + ("influence_score",)
)


def _int_or_none(raw: str | None) -> int | None:
    if raw is None or not str(raw).strip():
        return None
    return int(float(str(raw).strip()))  # 源文件混有 "-545.0" 风格年份


def _float_or_none(raw: str | None) -> float | None:
    if raw is None or not str(raw).strip():
        return None
    return float(str(raw).strip())


def _url_for(key: str, settings: Settings) -> str:
    return f"{settings.atlas_base.rstrip('/')}/{PATHS[key]}"


def _http_get(url: str, settings: Settings) -> bytes:
    """直连取一个文件（延迟导入 httpx：离线路径不依赖网络栈）。"""
    import httpx

    license_gate.assert_allowed_url(url)  # 红线守卫：出站前必过
    kwargs: dict = {
        "headers": {"User-Agent": UA},
        "timeout": 60.0,
        "follow_redirects": True,
    }
    if settings.http_proxy:
        kwargs["proxy"] = settings.http_proxy
    with httpx.Client(**kwargs) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content


def _snapshot_root(settings: Settings) -> Path:
    """快照根目录：env 优先，否则与参照系库同目录（测试库与快照同落 tmp_path）。"""
    env = os.environ.get(snapshot.ENV_ROOT)
    if env:
        return Path(env).expanduser()
    return Path(settings.db_path).expanduser().parent / "snapshots"


def _collect(
    settings: Settings,
    data_dir: Path | None,
    probe_fetch=None,
    http_get=None,
) -> tuple[dict[str, bytes], availability.ProbeResult]:
    """第一门（可得性）：探测 + 取内容。不可得时返回探测结果与空内容。"""
    if data_dir is not None:
        # 离线：本地缓存即"已取到的快照"，不探测网络（detail 如实标注 local-cache）
        blobs: dict[str, bytes] = {}
        for key in PATHS:
            path = data_dir / Path(PATHS[key]).name
            if not path.is_file():
                raise FileNotFoundError(
                    f"本地缓存缺少 {path}（--data-dir 布局应含 {Path(PATHS[key]).name}）"
                )
            blobs[key] = path.read_bytes()
        return blobs, availability.ProbeResult(
            source=SOURCE,
            url=str(data_dir),
            status=availability.STATUS_AVAILABLE,
            reason=None,
            checked_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            detail="local-cache（未探测网络）",
        )

    # 在线：先探主数据文件，不可得即整层不摄取（不拿半份数据入库）
    head = _url_for("philosophers", settings)
    result = availability.probe(
        SOURCE,
        head,
        proxy=settings.http_proxy or None,
        fetch=probe_fetch,
    )
    if not result.available:
        return {}, result
    getter = http_get or (lambda url: _http_get(url, settings))
    return {key: getter(_url_for(key, settings)) for key in PATHS}, result


def _freeze(
    blobs: dict[str, bytes],
    settings: Settings,
    probe_status: str,
    version: str,
    data_dir: Path | None,
) -> dict[str, snapshot.SnapshotManifest]:
    """第二门之后：内容落不可变快照并登记清单，返回 key → 清单。"""
    root = _snapshot_root(settings)
    out: dict[str, snapshot.SnapshotManifest] = {}
    for key, content in blobs.items():
        out[key] = snapshot.store(
            dataset=f"{SOURCE}/{key}",
            version=version,
            content=content,
            source_url=None if data_dir is not None else _url_for(key, settings),
            attribution=ATTRIBUTIONS[SOURCE],
            probe_status=probe_status,
            filename=Path(PATHS[key]).name,
            root=root,
        )
    return out



def parse_philosophers(text: str) -> list[dict]:
    out: list[dict] = []
    for row in csv.DictReader(io.StringIO(text)):
        name_pt = row["name"].strip()
        name_en, name_zh = FAMOUS_NAMES.get(name_pt, (None, None))
        school = row["school"].strip()
        region = row["region"].strip()
        rec = {
            "atlas_id": int(row["id"]),
            "name_pt": name_pt,
            "name_en": name_en,
            "name_zh": name_zh,
            "birth_year": _int_or_none(row.get("birth_year")),
            "death_year": _int_or_none(row.get("death_year")),
            "school": school,
            "school_en": SCHOOL_EN.get(school),
            "school_zh": SCHOOL_ZH.get(school),
            "region": region,
            "region_en": REGION_EN.get(region),
            "region_zh": REGION_ZH.get(region),
            **{axis: _float_or_none(row.get(axis)) for axis in AXES},
            "influence_score": _float_or_none(row.get("influence_score")),
        }
        out.append(rec)
    if not out:
        raise ValueError("philosophers.csv 解析结果为空（表头或数据不符）")
    return out


def parse_influences(text: str, valid_ids: set[int]) -> tuple[list[dict], list[str]]:
    edges: list[dict] = []
    skipped: list[str] = []
    for row in csv.DictReader(io.StringIO(text)):
        polarity = (row.get("type") or "").strip()
        if polarity not in POLARITIES:
            # 数据质量门：白皮书口径只有三种极性，出现未知值宁可失败也不静默入库
            raise ValueError(
                f"influences.csv 出现未知极性 {polarity!r}（口径仅 positive/critical/synthetic），拒绝入库"
            )
        try:
            source_id = int(row["source"])
            target_id = int(row["target"])
            weight = float(row["weight"])
        except (KeyError, TypeError, ValueError) as exc:
            skipped.append(f"unparseable row {dict(row)}: {exc}")
            continue
        if source_id not in valid_ids or target_id not in valid_ids:
            skipped.append(f"dangling endpoint {source_id}->{target_id}")
            continue
        edges.append(
            {"source_id": source_id, "target_id": target_id, "weight": weight, "polarity": polarity}
        )
    return edges, skipped


def parse_topics(text: str) -> list[dict]:
    out: list[dict] = []
    for item in json.loads(text):
        out.append(
            {
                "topic_id": item["id"],
                "slug": item.get("slug"),
                # 源目录只有 pt 标签；en/zh 留 NULL，后续任务补
                "label_pt": item.get("label"),
                "label_en": None,
                "label_zh": None,
                "category": item.get("category"),
                "description": item.get("description"),
                "key_philosophers": json.dumps(item.get("key_philosophers") or [], ensure_ascii=False),
            }
        )
    return out


def _log(settings: Settings, status: str, detail: dict) -> None:
    """台账：任何结局（含拒绝摄取）都留一行 ingest_log（best-effort，不掩盖原错误）。"""
    try:
        conn = settings.connect()
        try:
            schema.init(conn)
            with conn:
                conn.execute(
                    "INSERT INTO ingest_log (source, status, detail, as_of) VALUES (?,?,?,?)",
                    (
                        SOURCE,
                        status,
                        json.dumps(detail, ensure_ascii=False),
                        datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                    ),
                )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"[ingest_atlas] 台账写入失败：{exc}", file=sys.stderr)


def _log_failed(settings: Settings, message: str) -> None:
    _log(settings, "failed", {"error": message})


def _persist_probe(settings: Settings, result: availability.ProbeResult) -> None:
    """探测结果入来源状态表（缺口可见的数据面）。"""
    try:
        conn = settings.connect()
        try:
            schema.init(conn)
            availability.record(conn, result)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"[ingest_atlas] 探测台账写入失败：{exc}", file=sys.stderr)


def run(
    settings: Settings | None = None,
    data_dir: str | Path | None = None,
    *,
    version: str = DEFAULT_VERSION,
    probe_fetch=None,
    http_get=None,
) -> dict:
    """执行一次双门摄取，返回计数明细（含 status）。

    status 取值：
      ok / partial         —— 已入库（partial=有跳过边或未映射学派/地区）
      unavailable          —— 可得性门未过，该层未入库（缺口已入 source_status）
      license_unconfirmed  —— 许可门未过，该层未入库（缺口已入 source_status）
    """
    settings = settings or Settings.from_env()
    dd = Path(data_dir) if data_dir is not None else None

    # ---- 第一门：许可（静态事实） ----
    try:
        license_gate.require_license(SOURCE)
    except license_gate.LicenseNotConfirmed as exc:
        refusal = availability.ProbeResult(
            source=SOURCE,
            url=str(dd) if dd is not None else settings.atlas_base,
            status=availability.STATUS_UNAVAILABLE,
            reason="license_unconfirmed",
            checked_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            detail=exc.reason,
        )
        _persist_probe(settings, refusal)
        detail = {
            "status": "license_unconfirmed",
            "reason": "license_unconfirmed",
            "detail": exc.reason,
            "as_of": refusal.checked_at,
        }
        _log(settings, "unavailable", detail)
        return detail

    # ---- 第二门：可得性（动态事实；探测结果无条件入来源状态表） ----
    try:
        blobs, probed = _collect(settings, dd, probe_fetch=probe_fetch, http_get=http_get)
    except Exception as exc:  # noqa: BLE001 取内容阶段失败（本地缓存缺失等）
        _log_failed(settings, f"{type(exc).__name__}: {exc}")
        raise
    _persist_probe(settings, probed)
    if not probed.available:
        detail = {
            "status": "unavailable",
            "reason": probed.reason,
            "detail": probed.detail,
            "http_status": probed.http_status,
            "as_of": probed.checked_at,
        }
        _log(settings, "unavailable", detail)
        return detail

    # ---- 快照落盘 + 清单登记：解析的输入永远是这份只读快照 ----
    manifests = _freeze(blobs, settings, probed.status, version, dd)
    conn = settings.connect()
    try:
        schema.init(conn)
        for manifest in manifests.values():
            snapshot.record(conn, manifest)
    finally:
        conn.close()

    # ---- 解析（输入为快照字节，因此同一快照重复解析结果一致） ----
    try:
        philosophers = parse_philosophers(
            snapshot.read(manifests["philosophers"]).decode("utf-8")
        )
        valid_ids = {p["atlas_id"] for p in philosophers}
        edges, skipped = parse_influences(
            snapshot.read(manifests["influences"]).decode("utf-8"), valid_ids
        )
        topics = parse_topics(snapshot.read(manifests["topics"]).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        _log_failed(settings, f"{type(exc).__name__}: {exc}")
        raise

    unmapped_schools = sorted(
        {p["school"] for p in philosophers if p["school_en"] is None or p["school_zh"] is None}
    )
    unmapped_regions = sorted(
        {p["region"] for p in philosophers if p["region_en"] is None or p["region_zh"] is None}
    )
    status = "ok" if not (skipped or unmapped_schools or unmapped_regions) else "partial"
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    versions = {key: m.snapshot_id for key, m in manifests.items()}
    detail = {
        "philosophers": len(philosophers),
        "relations": len(edges),
        "topics": len(topics),
        "skipped_relations": skipped,
        "unmapped_schools": unmapped_schools,
        "unmapped_regions": unmapped_regions,
        "version": version,
        "snapshots": versions,
        "probe_status": probed.status,
        "as_of": now,
    }

    conn = settings.connect()
    try:
        schema.init(conn)
        with conn:  # 单事务：DELETE 后重插 → 幂等；wikidata_qid 重置为 NULL（2.3 负责回填）
            conn.execute("DELETE FROM philosophers")
            conn.execute("DELETE FROM relations")
            conn.execute("DELETE FROM topics")
            placeholders = ", ".join(["?"] * len(PHILOSOPHER_COLUMNS))
            conn.executemany(
                f"INSERT INTO philosophers ({', '.join(PHILOSOPHER_COLUMNS)}, wikidata_qid,"
                f" ingested_at, snapshot_version) VALUES ({placeholders}, NULL, ?, ?)",
                [
                    tuple(p[c] for c in PHILOSOPHER_COLUMNS)
                    + (now, versions["philosophers"])
                    for p in philosophers
                ],
            )
            conn.executemany(
                "INSERT INTO relations (source_id, target_id, weight, polarity, ingested_at,"
                " snapshot_version) VALUES (?,?,?,?,?,?)",
                [
                    (
                        e["source_id"], e["target_id"], e["weight"], e["polarity"],
                        now, versions["influences"],
                    )
                    for e in edges
                ],
            )
            conn.executemany(
                "INSERT INTO topics (topic_id, slug, label_pt, label_en, label_zh, category, "
                "description, key_philosophers, ingested_at, snapshot_version)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        t["topic_id"], t["slug"], t["label_pt"], t["label_en"], t["label_zh"],
                        t["category"], t["description"], t["key_philosophers"], now,
                        versions["topics"],
                    )
                    for t in topics
                ],
            )
            conn.execute(
                "INSERT INTO ingest_log (source, status, detail, as_of) VALUES (?,?,?,?)",
                (SOURCE, status, json.dumps(detail, ensure_ascii=False), now),
            )
    finally:
        conn.close()

    detail["status"] = status
    return detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Philosophy Atlas 种子图谱摄取（双门摄取）")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="本地缓存目录（应含 philosophers.csv / influences.csv / topics.json），离线用",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite 路径（默认 FD_ZHESI_REF_DB 或 ~/.fd-zhesi/reference.db）",
    )
    parser.add_argument(
        "--version",
        default=DEFAULT_VERSION,
        help=f"数据集版本标签（默认 {DEFAULT_VERSION}；精确身份由快照 sha256 钉住）",
    )
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if args.db:
        settings.db_path = args.db

    detail = run(settings, args.data_dir, version=args.version)
    print(json.dumps(detail, ensure_ascii=False, indent=2))
    return 0 if detail["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
