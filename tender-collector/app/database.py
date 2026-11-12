"""SQLite 持久化层。

announcements 以公告编号 (code) 为唯一键：重复采集走 UPSERT，不会产生重复记录。

并发采集的发布顺序由每个采集入口（列表页 source_url）上的轮次序号 (seq)
决定：

* 每个采集请求开始时先分配一个单调递增的 seq（同一入口内）；
* 抓取与归并在内存中完成，只有“完整成功”的轮次才会尝试发布；
* 发布时在同一个写事务里检查是否已有 **更晚发起且已发布** 的轮次：
  - 若有，本轮回写 collection_runs 为 unpublished，不触碰公告；
  - 若无，公告快照、来源状态与生效轮次在同一事务里一次性更新；
* 抓取/归并失败的轮次只写一条运行记录，不留下任何公告片段。

因此后发起且先成功的轮次会立即可查，旧轮随后结束也不会覆盖它；
反之，若后轮失败，先发起且完整成功的轮次仍可正常发布。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import settings

TABLES_SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('original', 'correction')),
    project_name    TEXT NOT NULL,
    published_at    TEXT NOT NULL,
    deadline        TEXT,
    corrects_code   TEXT,
    detail_url      TEXT NOT NULL,
    source_url      TEXT,
    published_run_id INTEGER,
    last_seen_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    seq             INTEGER NOT NULL DEFAULT 0,
    source_url      TEXT NOT NULL,
    status          TEXT NOT NULL
                    CHECK (status IN ('running', 'success', 'failed', 'unpublished')),
    published       INTEGER NOT NULL DEFAULT 0,
    items_collected INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT
);

CREATE TABLE IF NOT EXISTS source_state (
    source_url       TEXT PRIMARY KEY,
    published_run_id INTEGER NOT NULL,
    published_seq    INTEGER NOT NULL,
    published_at     TEXT NOT NULL
);
"""

INDEXES_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_announcements_project
    ON announcements(project_name);
CREATE INDEX IF NOT EXISTS idx_announcements_corrects
    ON announcements(corrects_code);
CREATE INDEX IF NOT EXISTS idx_announcements_source
    ON announcements(source_url);
CREATE INDEX IF NOT EXISTS idx_runs_source_seq
    ON collection_runs(source_url, seq);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def get_conn(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path or settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_transaction(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    """写事务：BEGIN IMMEDIATE 先取得写锁，避免“先检查后写入”的竞态。"""
    conn = _connect(db_path or settings.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.DatabaseError:
        return set()
    return {row["name"] for row in info}


def _migrate(conn: sqlite3.Connection) -> None:
    """兼容旧版数据库（旧表没有 seq/published/source_url 等字段）。

    旧库的公告行没有 source_url，无法按入口归属；这里为它们回填最近一次
    成功采集的入口，避免升级后重建快照时误删历史公告。
    """
    ann_cols = _column_names(conn, "announcements")
    run_info = conn.execute("PRAGMA table_info(collection_runs)").fetchall()
    run_cols = {row["name"] for row in run_info}
    finished_notnull = any(
        row["name"] == "finished_at" and row["notnull"] for row in run_info
    )

    if ann_cols:
        if "source_url" not in ann_cols:
            conn.execute("ALTER TABLE announcements ADD COLUMN source_url TEXT")
            conn.execute(
                """
                UPDATE announcements
                SET source_url = (
                    SELECT r.source_url FROM collection_runs r
                    WHERE r.status = 'success'
                    ORDER BY r.id DESC LIMIT 1
                )
                WHERE source_url IS NULL
                """
            )
        if "published_run_id" not in ann_cols:
            conn.execute(
                "ALTER TABLE announcements ADD COLUMN published_run_id INTEGER"
            )
            conn.execute(
                """
                UPDATE announcements
                SET published_run_id = (
                    SELECT r.id FROM collection_runs r
                    WHERE r.status = 'success'
                    ORDER BY r.id DESC LIMIT 1
                )
                WHERE published_run_id IS NULL
                """
            )

    if run_cols and ({"seq", "published"} - run_cols or finished_notnull):
        # 旧表的 status CHECK 只允许 success/failed，且 finished_at 非空；
        # SQLite 不能就地放宽约束，重建并保留历史运行记录。
        conn.executescript(
            """
            ALTER TABLE collection_runs RENAME TO collection_runs_legacy;
            CREATE TABLE collection_runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                seq             INTEGER NOT NULL DEFAULT 0,
                source_url      TEXT NOT NULL,
                status          TEXT NOT NULL
                                CHECK (status IN
                                       ('running', 'success', 'failed', 'unpublished')),
                published       INTEGER NOT NULL DEFAULT 0,
                items_collected INTEGER NOT NULL DEFAULT 0,
                error           TEXT,
                started_at      TEXT NOT NULL,
                finished_at     TEXT
            );
            INSERT INTO collection_runs
                (id, seq, source_url, status, published, items_collected,
                 error, started_at, finished_at)
            SELECT id, 0, source_url, status,
                   CASE WHEN status = 'success' THEN 1 ELSE 0 END,
                   items_collected, error, started_at, finished_at
            FROM collection_runs_legacy;
            DROP TABLE collection_runs_legacy;
            """
        )

    # 为历史成功轮次补一份来源状态；旧公告没有 source_url 标记，仅用于
    # 保证后续轮次的 seq 仍然单调递增。
    conn.execute(
        """
        INSERT OR IGNORE INTO source_state
            (source_url, published_run_id, published_seq, published_at)
        SELECT r.source_url, r.id, r.seq,
               COALESCE(r.finished_at, r.started_at)
        FROM collection_runs r
        WHERE r.status = 'success'
          AND r.id = (
              SELECT MAX(id) FROM collection_runs x
              WHERE x.source_url = r.source_url AND x.status = 'success'
          )
        """
    )


def init_db(db_path: str | None = None) -> None:
    with get_conn(db_path) as conn:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
        conn.executescript(TABLES_SCHEMA)
        _migrate(conn)
        conn.executescript(INDEXES_SCHEMA)
        conn.commit()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_announcements(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    source_url: str,
    run_id: int,
) -> None:
    """在调用方给定的事务内 UPSERT；同时写入来源与生效轮次。"""
    now = utcnow_iso()
    # 原公告先写，保证更正公告引用的数据已存在（顺序不影响业务判定，仅约束友好）
    rows = sorted(rows, key=lambda r: 0 if r["kind"] == "original" else 1)
    conn.executemany(
        """
        INSERT INTO announcements
            (code, title, kind, project_name, published_at, deadline,
             corrects_code, detail_url, source_url, published_run_id,
             last_seen_at)
        VALUES
            (:code, :title, :kind, :project_name, :published_at, :deadline,
             :corrects_code, :detail_url, :source_url, :published_run_id,
             :last_seen_at)
        ON CONFLICT(code) DO UPDATE SET
            title=excluded.title,
            kind=excluded.kind,
            project_name=excluded.project_name,
            published_at=excluded.published_at,
            deadline=excluded.deadline,
            corrects_code=excluded.corrects_code,
            detail_url=excluded.detail_url,
            source_url=excluded.source_url,
            published_run_id=excluded.published_run_id,
            last_seen_at=excluded.last_seen_at
        """,
        [
            {
                **r,
                "source_url": source_url,
                "published_run_id": run_id,
                "last_seen_at": now,
            }
            for r in rows
        ],
    )


def _replace_source_snapshot(
    conn: sqlite3.Connection,
    *,
    source_url: str,
    run_id: int,
    rows: list[dict[str, Any]],
) -> None:
    """以本轮结果替换该入口的公告快照。

    只删除“归属本入口、且最近一次由本轮之前更早的轮次发布”的公告：
    同一份公告若也被其它入口引用，其它入口的快照保持不动；被本轮结果
    重新覆盖时 UPSERT 会把它归到本轮。旧快照中已从源站消失的公告一并清理。
    """
    if rows:
        conn.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS _publish_codes (code TEXT PRIMARY KEY)
            """
        )
        conn.execute("DELETE FROM _publish_codes")
        conn.executemany(
            "INSERT OR IGNORE INTO _publish_codes(code) VALUES (?)",
            [(r["code"],) for r in rows],
        )
        conn.execute(
            """
            DELETE FROM announcements
            WHERE source_url = ?
              AND COALESCE(published_run_id, -1) < ?
              AND code NOT IN (SELECT code FROM _publish_codes)
            """,
            (source_url, run_id),
        )
        conn.execute("DROP TABLE IF EXISTS _publish_codes")
    else:
        conn.execute(
            """
            DELETE FROM announcements
            WHERE source_url = ? AND COALESCE(published_run_id, -1) < ?
            """,
            (source_url, run_id),
        )
    upsert_announcements(conn, rows, source_url=source_url, run_id=run_id)


@dataclass(frozen=True)
class PublishResult:
    published: bool
    published_run_id: int
    superseded_by_run_id: int | None = None


def begin_run(source_url: str, started_at: str | None = None) -> tuple[int, int]:
    """分配同一入口内单调递增的轮次序号，返回 (run_id, seq)。"""
    started = started_at or utcnow_iso()
    with write_transaction() as conn:
        seq = int(
            conn.execute(
                """
                SELECT COALESCE(MAX(seq), 0) + 1
                FROM collection_runs WHERE source_url = ?
                """,
                (source_url,),
            ).fetchone()[0]
        )
        cur = conn.execute(
            """
            INSERT INTO collection_runs
                (seq, source_url, status, published, items_collected,
                 error, started_at, finished_at)
            VALUES (?, ?, 'running', 0, 0, NULL, ?, NULL)
            """,
            (seq, source_url, started),
        )
        return int(cur.lastrowid), seq


def mark_run_failed(
    run_id: int,
    *,
    error: str,
    items_collected: int = 0,
    finished_at: str | None = None,
) -> None:
    with write_transaction() as conn:
        conn.execute(
            """
            UPDATE collection_runs
            SET status='failed', published=0, items_collected=?,
                error=?, finished_at=?
            WHERE id=?
            """,
            (items_collected, error, finished_at or utcnow_iso(), run_id),
        )


def publish_run(
    run_id: int,
    seq: int,
    source_url: str,
    rows: list[dict[str, Any]],
) -> PublishResult:
    """在单个写事务里发布一轮结果。

    若同一入口已有更晚发起（seq 更大）且已发布的轮次，则本轮的采集虽然
    完整成功，也不得覆盖，只把运行记录标记为 unpublished 并返回其 id；
    否则原子地替换公告快照、更新来源状态与生效轮次。
    """
    with write_transaction() as conn:
        state = conn.execute(
            """
            SELECT published_run_id, published_seq
            FROM source_state WHERE source_url = ?
            """,
            (source_url,),
        ).fetchone()

        if state is not None and int(state["published_seq"]) > seq:
            newer_run_id = int(state["published_run_id"])
            conn.execute(
                """
                UPDATE collection_runs
                SET status='unpublished', published=0, items_collected=?,
                    error=NULL, finished_at=?
                WHERE id=? AND status='running'
                """,
                (len(rows), utcnow_iso(), run_id),
            )
            return PublishResult(
                published=False,
                published_run_id=newer_run_id,
                superseded_by_run_id=newer_run_id,
            )

        _replace_source_snapshot(
            conn, source_url=source_url, run_id=run_id, rows=rows
        )
        conn.execute(
            """
            INSERT INTO source_state
                (source_url, published_run_id, published_seq, published_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_url) DO UPDATE SET
                published_run_id=excluded.published_run_id,
                published_seq=excluded.published_seq,
                published_at=excluded.published_at
            """,
            (source_url, run_id, seq, utcnow_iso()),
        )
        conn.execute(
            """
            UPDATE collection_runs
            SET status='success', published=1, items_collected=?,
                error=NULL, finished_at=?
            WHERE id=? AND status='running'
            """,
            (len(rows), utcnow_iso(), run_id),
        )
        return PublishResult(published=True, published_run_id=run_id)


def fetch_all_announcements(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM announcements ORDER BY project_name, published_at, code"
        )
    )


def fetch_runs(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM collection_runs ORDER BY id DESC LIMIT ?", (limit,)
        )
    )


def fetch_source_states(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM source_state ORDER BY source_url"
        )
    )
