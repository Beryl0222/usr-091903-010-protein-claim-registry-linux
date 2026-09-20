"""存储层：SQLite 关系模式 + 仅追加的哈希链审计事件日志。

关系表承载当前可查询状态，audit_events 记录每次写入的完整意图与哈希链，
论文快照（paper_snapshots）一经写入即不可变，任何后续置信度变化都不会
回溯修改快照内容。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from hashlib import sha256
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS proteins (
    id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    species TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS protein_aliases (
    id TEXT PRIMARY KEY,
    protein_id TEXT NOT NULL REFERENCES proteins(id),
    alias TEXT NOT NULL UNIQUE,
    species_scope TEXT,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prediction_batches (
    id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    model_version TEXT NOT NULL,
    structure_db TEXT NOT NULL,
    structure_db_version TEXT NOT NULL,
    run_note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS structure_predictions (
    id TEXT PRIMARY KEY,
    protein_id TEXT NOT NULL REFERENCES proteins(id),
    batch_id TEXT NOT NULL REFERENCES prediction_batches(id),
    confidence REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS materials (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    species TEXT,
    source TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    retracted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    retracted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS experiment_materials (
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    material_id TEXT NOT NULL REFERENCES materials(id),
    role TEXT NOT NULL CHECK (role IN ('sample', 'control', 'reagent')),
    PRIMARY KEY (experiment_id, material_id, role)
);

CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    label TEXT NOT NULL,
    description TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    retracted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS analyses (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    method TEXT NOT NULL,
    summary TEXT NOT NULL,
    stats_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    retracted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    claim_type TEXT NOT NULL,
    subject_protein_id TEXT NOT NULL REFERENCES proteins(id),
    object_protein_id TEXT REFERENCES proteins(id),
    predicate TEXT NOT NULL,
    statement TEXT NOT NULL,
    species_scope TEXT NOT NULL,
    granularity TEXT NOT NULL,
    sensitive INTEGER NOT NULL DEFAULT 0,
    supersedes_claim_id TEXT REFERENCES claims(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_links (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(id),
    kind TEXT NOT NULL CHECK (kind IN ('experimental', 'computational', 'inferential')),
    weight TEXT NOT NULL CHECK (weight IN ('supporting', 'contradicting')),
    prediction_id TEXT REFERENCES structure_predictions(id),
    experiment_id TEXT REFERENCES experiments(id),
    observation_id TEXT REFERENCES observations(id),
    analysis_id TEXT REFERENCES analyses(id),
    source_claim_id TEXT REFERENCES claims(id),
    inference_basis TEXT,
    species_from TEXT,
    species_to TEXT,
    note TEXT,
    event_seq INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claim_status (
    claim_id TEXT PRIMARY KEY REFERENCES claims(id),
    status TEXT NOT NULL,
    reason TEXT,
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL REFERENCES claims(id),
    status TEXT NOT NULL,
    reason TEXT,
    actor TEXT,
    event_seq INTEGER NOT NULL,
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retractions (
    id TEXT PRIMARY KEY,
    material_id TEXT REFERENCES materials(id),
    experiment_id TEXT REFERENCES experiments(id),
    observation_id TEXT REFERENCES observations(id),
    analysis_id TEXT REFERENCES analyses(id),
    reason TEXT NOT NULL,
    actor TEXT,
    retracted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retraction_impacts (
    retraction_id TEXT NOT NULL REFERENCES retractions(id),
    claim_id TEXT NOT NULL REFERENCES claims(id),
    dependency_path_json TEXT NOT NULL,
    PRIMARY KEY (retraction_id, claim_id)
);

CREATE TABLE IF NOT EXISTS papers (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    authors TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_snapshots (
    id TEXT PRIMARY KEY,
    paper_id TEXT NOT NULL REFERENCES papers(id),
    frozen_at TEXT NOT NULL,
    content_json TEXT NOT NULL,
    digest TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS snapshot_citations (
    snapshot_id TEXT NOT NULL REFERENCES paper_snapshots(id),
    claim_id TEXT NOT NULL REFERENCES claims(id),
    exact_quote TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, claim_id)
);

CREATE TABLE IF NOT EXISTS snapshot_approvals (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES paper_snapshots(id),
    scope TEXT NOT NULL CHECK (scope IN ('internal', 'public')),
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    decision_note TEXT,
    actor TEXT,
    redacted_content_json TEXT,
    decided_at TEXT NOT NULL,
    UNIQUE (snapshot_id, scope)
);

CREATE TABLE IF NOT EXISTS audit_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Store:
    """封装 SQLite 连接；所有写操作在同一把锁内完成并追加审计事件。"""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- 基础工具 -----------------------------------------------------

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def write(self, action: str, payload: dict, actor: str | None, fn) -> Any:
        """在一个事务内追加哈希链事件并执行业务写入 fn(cursor, event_seq)。

        event_seq 取审计事件自增主键，跨表、跨进程严格全序；fn 的返回值透传，
        任一步失败则整体回滚。
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                last = self._conn.execute(
                    "SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                prev_hash = last["hash"] if last else ("0" * 64)
                ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                body = canonical_json(payload)
                digest = sha256(
                    f"{prev_hash}|{ts}|{action}|{body}".encode("utf-8")
                ).hexdigest()
                cur = self._conn.execute(
                    "INSERT INTO audit_events (seq, ts, actor, action, payload_json, prev_hash, hash)"
                    " VALUES (NULL,?,?,?,?,?,?)",
                    (ts, actor, action, body, prev_hash, digest),
                )
                event_seq = cur.lastrowid
                result = fn(self._conn, event_seq)
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise

    # ---- 验证审计链（巡检用）------------------------------------------

    def verify_audit_chain(self) -> dict:
        rows = self.query(
            "SELECT seq, ts, actor, action, payload_json, prev_hash, hash"
            " FROM audit_events ORDER BY seq"
        )
        prev = "0" * 64
        for row in rows:
            if row["prev_hash"] != prev:
                return {"ok": False, "broken_at": row["seq"], "reason": "prev_hash mismatch"}
            digest = sha256(
                f"{prev}|{row['ts']}|{row['action']}|{row['payload_json']}".encode("utf-8")
            ).hexdigest()
            if digest != row["hash"]:
                return {"ok": False, "broken_at": row["seq"], "reason": "hash mismatch"}
            prev = row["hash"]
        return {"ok": True, "events": len(rows)}
