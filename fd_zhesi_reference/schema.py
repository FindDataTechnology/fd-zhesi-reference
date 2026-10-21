"""参照系库 DDL（SQLite，可重建缓存；design D3/D7）。

中西无感：school/region 为开放词表（pt 原值 + en/zh 映射列），不绑西哲枚举。
"""

from __future__ import annotations

import sqlite3

DDL = [
    # Atlas 种子图谱（task 2.2）
    """CREATE TABLE IF NOT EXISTS philosophers (
        atlas_id INTEGER PRIMARY KEY,
        name_pt TEXT NOT NULL,
        name_en TEXT,
        name_zh TEXT,
        birth_year INTEGER,
        death_year INTEGER,
        school TEXT, school_en TEXT, school_zh TEXT,
        region TEXT, region_en TEXT, region_zh TEXT,
        individualism REAL, materialism REAL, rationalism REAL,
        determinism REAL, transcendence REAL, universalism REAL,
        influence_score REAL,
        wikidata_qid TEXT,
        ingested_at TEXT NOT NULL,
        snapshot_version TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS relations (
        atlas_id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL REFERENCES philosophers(atlas_id),
        target_id INTEGER NOT NULL REFERENCES philosophers(atlas_id),
        weight REAL NOT NULL,
        polarity TEXT NOT NULL,  -- positive / critical / synthetic
        ingested_at TEXT NOT NULL,
        snapshot_version TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS topics (
        topic_id TEXT PRIMARY KEY,
        slug TEXT, label_pt TEXT, label_en TEXT, label_zh TEXT,
        category TEXT,
        description TEXT,
        key_philosophers TEXT,  -- JSON array of atlas_id
        ingested_at TEXT NOT NULL,
        snapshot_version TEXT
    )""",
    # SEP 引用层缓存（task 2.4；只存标题+摘要+链接，不存正文）
    """CREATE TABLE IF NOT EXISTS concepts (
        term TEXT NOT NULL,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        snippet TEXT,
        retrieved_at TEXT NOT NULL,
        PRIMARY KEY (term, url)
    )""",
    # PhilPapers Survey 立场分布（task 2.5）
    """CREATE TABLE IF NOT EXISTS stances (
        survey_round TEXT NOT NULL,  -- 2009 / 2020
        question TEXT NOT NULL,
        question_zh TEXT,
        option TEXT NOT NULL,
        option_zh TEXT,
        pct REAL,
        retrieved_at TEXT NOT NULL,
        snapshot_version TEXT,
        PRIMARY KEY (survey_round, question, option)
    )""",
    # 摄取台账（task 2.6 source_status 的数据面）
    """CREATE TABLE IF NOT EXISTS ingest_log (
        source TEXT NOT NULL,
        status TEXT NOT NULL,     -- ok / partial / failed / not_ingested
        detail TEXT,
        as_of TEXT NOT NULL,
        PRIMARY KEY (source, as_of)
    )""",
    # 来源状态表（zhesi-atlas-resilience 1.1）：可得性探测结果，与许可判定分开记录
    """CREATE TABLE IF NOT EXISTS source_status (
        source TEXT NOT NULL,
        checked_at TEXT NOT NULL,
        status TEXT NOT NULL,       -- available / unavailable
        reason TEXT,                -- unreachable / timeout / http_error / not_probed
        url TEXT,
        http_status INTEGER,
        elapsed_ms INTEGER,
        content_sha256 TEXT,
        content_bytes INTEGER,
        detail TEXT,
        PRIMARY KEY (source, checked_at)
    )""",
    # 快照清单（zhesi-atlas-resilience 1.2）：静态数据集版本化的唯一摄取入口
    """CREATE TABLE IF NOT EXISTS snapshots (
        snapshot_id TEXT PRIMARY KEY,   -- <dataset>@<version>#<sha256 前 12>
        dataset TEXT NOT NULL,
        version TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        source_url TEXT,
        sha256 TEXT NOT NULL,
        attribution TEXT,
        probe_status TEXT NOT NULL,     -- available / unavailable
        storage_path TEXT NOT NULL,
        bytes INTEGER,
        created_at TEXT NOT NULL
    )""",
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_philosophers_names ON philosophers(name_en, name_zh)",
    "CREATE INDEX IF NOT EXISTS idx_relations_src ON relations(source_id)",
    "CREATE INDEX IF NOT EXISTS idx_relations_dst ON relations(target_id)",
    "CREATE INDEX IF NOT EXISTS idx_stances_q ON stances(question)",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_dataset ON snapshots(dataset, fetched_at)",
    "CREATE INDEX IF NOT EXISTS idx_source_status_source ON source_status(source, checked_at)",
]

# 增量列（只追加不改历史）：旧库（已存在的 reference.db / seed 快照）经
# ensure_columns 补列，新库由 DDL 直接建出。snapshot_version 让每行结果能回到
# 具体快照（zhesi-atlas-resilience 1.4）。
ADDITIVE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "philosophers": [("snapshot_version", "TEXT")],
    "relations": [("snapshot_version", "TEXT")],
    "topics": [("snapshot_version", "TEXT")],
    "stances": [("snapshot_version", "TEXT")],
}


def ensure_columns(conn: sqlite3.Connection) -> None:
    """给既有库补增量列（幂等）。表不存在时跳过——由 DDL 负责建表。"""
    for table, columns in ADDITIVE_COLUMNS.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:  # 表还没有：DDL 会带上这些列
            continue
        for name, ddl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    conn.commit()


def init(conn: sqlite3.Connection) -> None:
    for ddl in DDL:
        conn.execute(ddl)
    for idx in INDEXES:
        conn.execute(idx)
    ensure_columns(conn)
    conn.commit()
