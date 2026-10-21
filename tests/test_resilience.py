"""zhesi-atlas-resilience 1.x / 3.x / 6.1-6.2 验证（全离线）。

覆盖：
- 1.1 可得性探测：HTTP 状态/超时/内容 sha256/探测时间；Atlas 不可达 → unavailable
- 1.2 快照清单与不可变存储；同一快照重复解析结果一致
- 1.3 双门摄取：许可未确认 / 不可得 → 该层不入库且缺口可见
- 1.4 provenance 带 snapshot_version
- 3.1 OpenAlex 的 PhilPapers 源（S4306402130）论文元数据通道 + CC0 归属 + 失败口径
- 6.1 许可红线守卫（InPhO / DBpedia / Kialo / PhilPapers 直连）
- 6.2 SEP 存储只含标题/URL/短引用
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import ingest_atlas  # noqa: E402
from fd_zhesi_reference import license as license_gate  # noqa: E402
from fd_zhesi_reference import probe as availability  # noqa: E402
from fd_zhesi_reference import schema, snapshot, tools_papers  # noqa: E402
from fd_zhesi_reference.config import Settings  # noqa: E402
from fd_zhesi_reference.provenance import ATTRIBUTIONS  # noqa: E402
from fd_zhesi_reference.tools_registry import source_status  # noqa: E402

PHILOSOPHERS_CSV = (
    "id,name,birth_year,death_year,school,region,individualism,materialism,rationalism,"
    "determinism,transcendence,universalism,influence_score\n"
    "6,Sócrates,-470,-399.0,Clássico Grego,Grécia,6,1,9,4,7,9,10\n"
    "7,Platão,-428,-348.0,Clássico Grego,Grécia,3,2,10,5,9,10,10\n"
    "8,Aristóteles,-384,-322.0,Clássico Grego,Grécia,5,7,10,6,4,8,10\n"
)
INFLUENCES_CSV = "source,target,weight,type\n8,7,0.7,critical\n"
TOPICS_JSON = (
    '[{"id": "T1", "slug": "teste", "label": "Teste", "category": "Ética",'
    ' "key_philosophers": [6]}]'
)


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cache"
    d.mkdir()
    (d / "philosophers.csv").write_text(PHILOSOPHERS_CSV, encoding="utf-8")
    (d / "influences.csv").write_text(INFLUENCES_CSV, encoding="utf-8")
    (d / "topics.json").write_text(TOPICS_JSON, encoding="utf-8")
    return d


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(db_path=str(tmp_path / "ref.db"))


def _rows(settings: Settings, table: str) -> int:
    conn = settings.connect()
    try:
        schema.init(conn)
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _status_of(settings: Settings, source: str) -> dict | None:
    out = source_status(settings)
    for entry in out["results"] + out["on_demand"]:
        if entry["source"] == source:
            return entry
    return None


# ---------------------------------------------------------------- 1.1 探测 ----
def test_probe_records_unavailable_on_timeout(settings: Settings) -> None:
    """Atlas 不可达（超时）→ 记录 unavailable + reason，且带探测时间与耗时。"""

    def timeout_fetch(url, timeout, proxy):
        raise TimeoutError("simulated timeout")

    result = availability.probe(
        "philosophy-atlas", "https://philosophyatlas.com.br/data/raw/philosophers.csv",
        fetch=timeout_fetch,
    )
    assert result.status == "unavailable" and result.reason == "timeout"
    assert result.checked_at and result.elapsed_ms is not None

    conn = settings.connect()
    try:
        schema.init(conn)
        availability.record(conn, result)
        latest = availability.latest(conn, "philosophy-atlas")
    finally:
        conn.close()
    assert latest["status"] == "unavailable" and latest["reason"] == "timeout"


def test_probe_records_http_error_and_sha256(settings: Settings) -> None:
    def err_fetch(url, timeout, proxy):
        return 503, b""

    result = availability.probe("philosophy-atlas", "https://philosophyatlas.com.br/x",
                                fetch=err_fetch)
    assert (result.status, result.reason, result.http_status) == ("unavailable", "http_error", 503)

    def ok_fetch(url, timeout, proxy):
        return 200, b"id,name\n1,a\n"

    ok = availability.probe("philosophy-atlas", "https://philosophyatlas.com.br/y", fetch=ok_fetch)
    assert ok.status == "available" and ok.content_sha256 and ok.content_bytes > 0


# ---------------------------------------------------------- 1.2 快照与重放 ----
def test_snapshot_store_is_immutable_and_reusable(tmp_path: Path) -> None:
    root = tmp_path / "snaps"
    first = snapshot.store("ds", "v1", b"payload", root=root, filename="a.csv")
    again = snapshot.store("ds", "v1", b"payload", root=root, filename="a.csv")
    assert first.storage_path == again.storage_path          # 内容寻址：同内容同路径
    assert first.snapshot_id == again.snapshot_id
    assert Path(first.storage_path).read_bytes() == b"payload"
    assert snapshot.read(first) == b"payload"


def test_snapshot_corruption_detected(tmp_path: Path) -> None:
    root = tmp_path / "snaps"
    manifest = snapshot.store("ds", "v1", b"payload", root=root, filename="a.csv")
    Path(manifest.storage_path).write_bytes(b"tampered")
    with pytest.raises(snapshot.SnapshotCorrupt):
        snapshot.read(manifest)


def test_same_snapshot_reparses_identically(settings: Settings, data_dir: Path) -> None:
    """同一快照重复解析得到一致结果（解析输入是只读快照，不是网络响应）。"""
    first = ingest_atlas.run(settings, data_dir=data_dir)
    second = ingest_atlas.run(settings, data_dir=data_dir)
    assert first["snapshots"] == second["snapshots"]         # 快照 id 不变
    assert first["philosophers"] == second["philosophers"] == 3

    conn = settings.connect()
    try:
        schema.init(conn)
        manifest = snapshot.latest(conn, "philosophy-atlas/philosophers")
        assert manifest is not None
        replay_a = ingest_atlas.parse_philosophers(
            snapshot.read(manifest).decode("utf-8")
        )
        replay_b = ingest_atlas.parse_philosophers(
            snapshot.read(manifest).decode("utf-8")
        )
    finally:
        conn.close()
    assert replay_a == replay_b


def test_manifest_fields_present(settings: Settings, data_dir: Path) -> None:
    ingest_atlas.run(settings, data_dir=data_dir)
    conn = settings.connect()
    try:
        schema.init(conn)
        row = snapshot.latest(conn, "philosophy-atlas/philosophers")
    finally:
        conn.close()
    for field in ("dataset", "version", "fetched_at", "source_url", "sha256",
                  "attribution", "probe_status", "storage_path"):
        assert field in row
    assert row["probe_status"] == "available"
    assert "CC BY 4.0" in row["attribution"]


# -------------------------------------------------------- 1.3 双门摄取 ----
def test_license_gate_blocks_ingest_and_visible(settings: Settings, data_dir: Path,
                                                monkeypatch) -> None:
    """许可未确认 → 该层不入库，缺口在 source_status 可见且注明原因。"""
    monkeypatch.setitem(
        license_gate.LICENSE_REGISTRY, "philosophy-atlas",
        {**license_gate.LICENSE_REGISTRY["philosophy-atlas"], "status": "unconfirmed"},
    )
    detail = ingest_atlas.run(settings, data_dir=data_dir)
    assert detail["status"] == "license_unconfirmed"
    assert _rows(settings, "philosophers") == 0

    entry = _status_of(settings, "philosophy-atlas")
    assert entry["status"] == "unavailable"
    assert entry["reason"] == "license_unconfirmed"
    assert entry["probe"]["reason"] == "license_unconfirmed"


def test_availability_gate_blocks_ingest_and_visible(settings: Settings, data_dir: Path) -> None:
    """Atlas 不可达 → 该层不入库（即便本地有缓存），缺口标注 unavailable。"""

    def unreachable(url, timeout, proxy):
        raise OSError("nodename nor servname provided")

    # 走在线路径（data_dir=None）以便注入探测失败
    detail = ingest_atlas.run(settings, probe_fetch=unreachable)
    assert detail["status"] == "unavailable"
    assert detail["reason"] == "unreachable"
    assert _rows(settings, "philosophers") == 0

    entry = _status_of(settings, "philosophy-atlas")
    assert entry["status"] == "unavailable" and entry["reason"] == "unreachable"
    assert entry["probe"]["http_status"] is None


def test_double_gate_happy_path_records_snapshot_and_probe(settings: Settings,
                                                           data_dir: Path) -> None:
    detail = ingest_atlas.run(settings, data_dir=data_dir)
    assert detail["status"] == "ok"
    entry = _status_of(settings, "philosophy-atlas")
    assert entry["status"] == "ok"
    assert entry["snapshot"]["probe_status"] == "available"
    assert entry["snapshot"]["snapshot_id"].startswith("philosophy-atlas/philosophers@")


# ------------------------------------------------------ 1.4 snapshot_version ----
@pytest.mark.asyncio
async def test_provenance_carries_snapshot_version(settings: Settings,
                                                   data_dir: Path) -> None:
    from fd_zhesi_reference.server import create_server

    detail = ingest_atlas.run(settings, data_dir=data_dir)
    expected = detail["snapshots"]["philosophers"]

    mcp = create_server(settings)
    tools = await mcp.list_tools()
    fn = next(t.fn for t in tools if t.name == "philosopher_query")
    res = await fn(name="Sócrates")
    prov = res["results"][0]["provenance"]
    assert prov["source"] == "philosophy-atlas"
    assert prov["as_of"]
    assert prov["snapshot_version"] == expected
    assert "CC BY 4.0" in prov["attribution"]


# ------------------------------------------- 3.1 OpenAlex PhilPapers 通道 ----
def _openalex_payload() -> dict:
    return {
        "meta": {"count": 1},
        "results": [
            {
                "id": "https://openalex.org/W123",
                "title": "What Do Philosophers Believe?",
                "publication_year": 2014,
                "doi": "https://doi.org/10.1007/s11098-013-0259-7",
                "cited_by_count": 512,
                "primary_location": {"source": {"display_name": "Philosophical Studies"}},
            }
        ],
    }


def test_philpapers_channel_hit_with_cc0(monkeypatch) -> None:
    captured: dict = {}

    def fake_get(url, timeout=25.0):
        captured["url"] = url
        return _openalex_payload(), {"x-ratelimit-remaining": "999"}

    monkeypatch.setattr(tools_papers, "_get_json", fake_get)
    out = tools_papers.search_papers("philosophers believe", limit=5)

    assert "api.openalex.org" in captured["url"]
    assert "S4306402130" in captured["url"]              # 限定 PhilPapers 源
    assert out["channel"]["philpapers_source_id"] == "S4306402130"
    hit = out["results"][0]
    assert hit["title"] == "What Do Philosophers Believe?"
    assert hit["year"] == 2014 and hit["cited_by_count"] == 512
    assert hit["provenance"]["source"] == "philpapers-openalex"
    assert "CC0" in hit["provenance"]["attribution"]
    assert "S4306402130" in ATTRIBUTIONS["philpapers-openalex"]


@pytest.mark.parametrize(
    "exc, reason",
    [
        (tools_papers._Failure("quota", "429"), "quota"),
        (tools_papers._Failure("upstream", "502"), "upstream"),
        (tools_papers._Failure("unreachable", "dns"), "unreachable"),
        (tools_papers._Failure("blocked", "403"), "blocked"),
    ],
)
def test_philpapers_channel_failure_reason(monkeypatch, exc, reason) -> None:
    """失败必带原因（不得静默为空、不得冒充无数据）。"""

    def fake_get(url, timeout=25.0):
        raise exc

    monkeypatch.setattr(tools_papers, "_get_json", fake_get)
    out = tools_papers.search_papers("anything")
    assert out["results"] == [] and out["reason"] == reason
    assert out["hint"]


def test_philpapers_channel_not_indexed_is_distinct(monkeypatch) -> None:
    monkeypatch.setattr(tools_papers, "_get_json",
                        lambda url, timeout=25.0: ({"meta": {"count": 0}, "results": []}, {}))
    out = tools_papers.search_papers("no such thing")
    assert out["results"] == [] and out["reason"] == "not_indexed"


def test_philpapers_never_used_for_stance_distribution() -> None:
    """分路：Survey 层不以论文元数据兜底（3.2 的结构性保证）。"""
    from fd_zhesi_reference import tools_survey

    assert tools_survey.PAPER_METADATA_FALLBACK is False
    tree = ast.parse(Path(tools_survey.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module}.{a.name}" for a in node.names)
    assert not any("tools_papers" in name for name in imported), imported


# -------------------------------------------------------- 6.1 红线守卫 ----
def test_redline_guard_rejects_forbidden_hosts() -> None:
    for host in ("inpho.indiana.edu", "dbpedia.org", "kialo.com", "philpapers.org"):
        with pytest.raises(license_gate.ForbiddenSource):
            license_gate.assert_allowed_url(f"https://{host}/some/path")
    # 允许的源照常通过
    assert license_gate.assert_allowed_url("https://api.openalex.org/works")
    assert license_gate.assert_allowed_url("https://plato.stanford.edu/search")


def test_no_forbidden_host_outside_citation_layer() -> None:
    """源码里除引用层外不得出现红线主机（即：不得把红线源接成请求目标）。

    例外仅两处，且都写在明面上：
    - `provenance.py` / `license.py`：引用文本与红线定义本身（允许链接，不允许抓取）
    - `scripts/probe_survey.py`：一次性侦察脚本，记录 Cloudflare 拦截实测；
      不进镜像（Dockerfile 只 COPY fd_zhesi_reference 与 seed），不是运行时依赖
    """
    allowed_files = {
        (REPO / "fd_zhesi_reference" / "provenance.py").resolve(),
        (REPO / "fd_zhesi_reference" / "license.py").resolve(),
        (REPO / "scripts" / "probe_survey.py").resolve(),
    }
    offenders: list[str] = []
    for path in list((REPO / "fd_zhesi_reference").rglob("*.py")) + list(
        (REPO / "scripts").rglob("*.py")
    ):
        if path.resolve() in allowed_files:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and isinstance(
                    body[0].value, ast.Constant
                ) and isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings:
                continue
            for host in license_gate.FORBIDDEN_HOSTS:
                if host in node.value:
                    offenders.append(f"{path.name}: {host}")
    assert offenders == [], f"红线主机出现在请求面代码里：{offenders}"


def test_license_registry_covers_all_used_sources() -> None:
    for source in ("philosophy-atlas", "sep", "wikidata",
                   "philpapers-survey", "philpapers-openalex"):
        assert license_gate.license_confirmed(source)
        assert source in ATTRIBUTIONS


# -------------------------------------------------------- 6.2 SEP 存储 ----
def test_sep_storage_only_short_quotes(settings: Settings) -> None:
    """存储内容检查：只存标题/URL/短引用，不含条目正文或摘要。"""
    from fd_zhesi_reference import tools_sep
    from fd_zhesi_reference.sep import SepHit

    long_body = "正文" * 500  # 1000 字：若落库即违反只链不抄
    conn = settings.connect()
    try:
        schema.init(conn)
        tools_sep._write_cache(
            conn, "free will",
            [SepHit(title="Free Will", url="https://plato.stanford.edu/entries/freewill/",
                    snippet=long_body)],
        )
        row = conn.execute("SELECT title, snippet FROM concepts").fetchone()
        assert len(row["snippet"]) <= tools_sep.MAX_SNIPPET_CHARS
        assert row["snippet"].endswith("…")
        assert tools_sep.storage_policy_violations(conn) == []
    finally:
        conn.close()


def test_sep_short_quote_keeps_short_text_intact() -> None:
    from fd_zhesi_reference.tools_sep import _short_quote

    assert _short_quote("short quote") == "short quote"
    assert _short_quote("a  b\n c") == "a b c"
