"""SQLite 持久层：底表与签发状态。

写入一律走单事务 BEGIN/COMMIT，任何异常 ROLLBACK，保证写入失败时原状不变。
"""
import csv
import io
import os
import sqlite3
from datetime import datetime, timezone

from .engine import GROUPS, GROUP_SIZE

SCHEMA = """
CREATE TABLE IF NOT EXISTS visitors (
    id         TEXT PRIMARY KEY,
    group_name TEXT NOT NULL CHECK (group_name IN ('A','B')),
    seq        INTEGER NOT NULL,
    status     INTEGER CHECK (status IN (0, 1)),
    UNIQUE (group_name, seq)
);
CREATE TABLE IF NOT EXISTS corrections (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    visitor_id TEXT NOT NULL,
    old_status INTEGER,
    new_status INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (visitor_id) REFERENCES visitors(id)
);
CREATE TABLE IF NOT EXISTS issuances (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    conclusion        TEXT NOT NULL CHECK (conclusion IN ('A','B','NONE')),
    issued_at         TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('VALID','INVALIDATED')),
    invalidated_at    TEXT,
    invalidate_reason TEXT
);
"""

TRUE_TOKENS = {"1", "y", "yes", "true", "t", "完成", "是"}
FALSE_TOKENS = {"0", "n", "no", "false", "f", "未完成", "否"}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db():
    conn = sqlite3.connect(os.environ.get("APP_DB", "/data/guide.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)
    n = conn.execute("SELECT COUNT(*) FROM visitors").fetchone()[0]
    if n == 0:
        rows = [
            (f"{g}{i:03d}", g, i, None)
            for g in GROUPS
            for i in range(1, GROUP_SIZE + 1)
        ]
        conn.executemany(
            "INSERT INTO visitors (id, group_name, seq, status) VALUES (?, ?, ?, ?)",
            rows,
        )
    conn.commit()


def all_visitors(conn):
    return conn.execute(
        "SELECT id, group_name, seq, status FROM visitors ORDER BY group_name, seq"
    ).fetchall()


def parse_csv(raw_bytes):
    """解析上传 CSV，返回 [(visitor_id, new_status 0/1), ...]。

    接受表头 id,status（表头可有可无，自动识别）；
    整文件任一非法行都抛 ValueError，由上层整体拒绝。
    """
    if not raw_bytes:
        raise ValueError("上传文件为空")
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("文件不是 UTF-8 编码")
    reader = csv.reader(io.StringIO(text))
    records = []
    seen = set()
    for lineno, row in enumerate(reader, start=1):
        if not row or all(not c.strip() for c in row):
            continue
        if len(row) < 2:
            raise ValueError(f"第 {lineno} 行字段不足，应为 id,status 两列")
        vid, token = row[0].strip(), row[1].strip().lower()
        if lineno == 1 and token not in TRUE_TOKENS and token not in FALSE_TOKENS:
            continue  # 表头（状态列不是合法取值）
        if token in TRUE_TOKENS:
            status = 1
        elif token in FALSE_TOKENS:
            status = 0
        else:
            raise ValueError(f"第 {lineno} 行状态值无法识别: {row[1]!r}")
        if vid in seen:
            raise ValueError(f"编号 {vid} 在文件中重复出现")
        seen.add(vid)
        records.append((vid, status))
    if not records:
        raise ValueError("CSV 中没有任何有效记录")
    return records


def apply_corrections(conn, records, commit=True):
    """事务化补报/更正。失败抛异常并回滚，底表保持原状。

    commit=False 时不提交，供调用方在同一事务内追加失效处理。
    返回 {visitor_id: (old, new)} 实际发生的变更。
    """
    changes = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for vid, new_status in records:
            row = conn.execute(
                "SELECT id, status FROM visitors WHERE id = ?", (vid,)
            ).fetchone()
            if row is None:
                raise ValueError(f"编号 {vid} 不在预置名单中，整批拒绝")
            old_status = row["status"]
            if old_status != new_status:
                conn.execute(
                    "UPDATE visitors SET status = ? WHERE id = ?",
                    (new_status, vid),
                )
                conn.execute(
                    "INSERT INTO corrections (visitor_id, old_status, new_status, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (vid, old_status, new_status, now_iso()),
                )
                changes[vid] = (old_status, new_status)
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    return changes


def issue(conn, conclusion):
    """按最新底表签发结论（调用方负责先核实唯一性）。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO issuances (conclusion, issued_at, status) VALUES (?, ?, 'VALID')",
            (conclusion, now_iso()),
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        conn.rollback()
        raise


def invalidate_open_issuances(conn, reason):
    """更正后使仍有效的旧签发失效。事务内执行。"""
    cur = conn.execute(
        "UPDATE issuances SET status = 'INVALIDATED', invalidated_at = ?, "
        "invalidate_reason = ? WHERE status = 'VALID'",
        (now_iso(), reason),
    )
    return cur.rowcount


def list_issuances(conn):
    return conn.execute(
        "SELECT * FROM issuances ORDER BY id DESC"
    ).fetchall()


def list_corrections(conn, limit=100):
    return conn.execute(
        "SELECT * FROM corrections ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
