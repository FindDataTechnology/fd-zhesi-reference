"""不可变快照存储与快照清单（zhesi-atlas-resilience 1.2）。

清单字段（design 决策 2）：`dataset` / `version` / `fetched_at` / `source_url` /
`sha256` / `attribution` / `probe_status`，另存 `storage_path` 与字节数。

不可变性：
- 存储路径内容寻址：`<root>/<dataset>/<sha256>/<原文件名>`
- 同内容重复写入 → 复用同一路径，不覆盖（写一次即冻结）
- 同路径内容若与清单 sha256 不符 → 视为损坏，抛 SnapshotCorrupt（绝不"修好"它）
- 快照 id = `<dataset>@<version>#<sha256 前 12 位>`，解析产物引用此 id

因此「同一快照可重复解析得到一致结果」是结构保证：解析的输入是同一个只读文件。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROOT = "~/.fd-zhesi/snapshots"
ENV_ROOT = "FD_ZHESI_SNAPSHOT_DIR"


class SnapshotCorrupt(RuntimeError):
    """已存快照与清单 sha256 不符（存储被改写）。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def root_dir() -> Path:
    return Path(os.environ.get(ENV_ROOT) or DEFAULT_ROOT).expanduser()


def _safe(name: str) -> str:
    """dataset/version 进路径前清洗：只留字母数字与 . _ -（防目录穿越）。"""
    return re.sub(r"[^A-Za-z0-9._-]", "-", name).strip(".-") or "unnamed"


@dataclass(frozen=True)
class SnapshotManifest:
    snapshot_id: str
    dataset: str
    version: str
    fetched_at: str
    source_url: str | None
    sha256: str
    attribution: str
    probe_status: str
    storage_path: str
    bytes: int
    created_at: str

    def as_dict(self) -> dict:
        return asdict(self)


def make_id(dataset: str, version: str, sha256: str) -> str:
    return f"{dataset}@{version}#{sha256[:12]}"


def store(
    dataset: str,
    version: str,
    content: bytes,
    *,
    source_url: str | None = None,
    attribution: str = "",
    probe_status: str = "available",
    filename: str = "content.bin",
    fetched_at: str | None = None,
    root: Path | None = None,
) -> SnapshotManifest:
    """写入（或复用）一份不可变快照，返回清单。"""
    digest = _sha256(content)
    base = (root or root_dir()) / _safe(dataset) / digest
    target = base / _safe(filename)
    if target.is_file():
        # 已存在：校验内容一致后复用（同内容同路径 → 幂等）
        existing = target.read_bytes()
        if _sha256(existing) != digest:
            raise SnapshotCorrupt(f"{target} 内容与路径 sha256 不符（快照被改写）")
    else:
        base.mkdir(parents=True, exist_ok=True)
        tmp = base / (target.name + ".tmp")
        tmp.write_bytes(content)
        shutil.move(str(tmp), str(target))  # 原子落盘，避免半截文件被读
    ts = _now()
    return SnapshotManifest(
        snapshot_id=make_id(dataset, version, digest),
        dataset=dataset,
        version=version,
        fetched_at=fetched_at or ts,
        source_url=source_url,
        sha256=digest,
        attribution=attribution,
        probe_status=probe_status,
        storage_path=str(target),
        bytes=len(content),
        created_at=ts,
    )


def read(manifest: SnapshotManifest | dict) -> bytes:
    """读回快照内容并校验完整性（不一致即抛 SnapshotCorrupt）。"""
    path = Path(
        manifest.storage_path if isinstance(manifest, SnapshotManifest)
        else manifest["storage_path"]
    )
    expected = (
        manifest.sha256 if isinstance(manifest, SnapshotManifest) else manifest["sha256"]
    )
    if not path.is_file():
        raise SnapshotCorrupt(f"快照文件缺失：{path}")
    payload = path.read_bytes()
    if _sha256(payload) != expected:
        raise SnapshotCorrupt(f"{path} 内容与清单 sha256 不符（快照被改写）")
    return payload


def record(conn, manifest: SnapshotManifest) -> None:
    """清单登记入库（同一 snapshot_id 重复登记是幂等的）。"""
    conn.execute(
        "INSERT OR REPLACE INTO snapshots (snapshot_id, dataset, version, fetched_at,"
        " source_url, sha256, attribution, probe_status, storage_path, bytes, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            manifest.snapshot_id, manifest.dataset, manifest.version,
            manifest.fetched_at, manifest.source_url, manifest.sha256,
            manifest.attribution, manifest.probe_status, manifest.storage_path,
            manifest.bytes, manifest.created_at,
        ),
    )
    conn.commit()


def get(conn, snapshot_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
    ).fetchone()
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def latest(conn, dataset: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM snapshots WHERE dataset=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (dataset,),
    ).fetchone()
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def list_for(conn, dataset: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM snapshots WHERE dataset=? ORDER BY created_at DESC, rowid DESC",
        (dataset,),
    ).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]
