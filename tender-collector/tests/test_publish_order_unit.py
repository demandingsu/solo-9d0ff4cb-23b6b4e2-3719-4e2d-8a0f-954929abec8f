"""发布顺序的 SQLite 层单元测试（不经过 HTTP）。"""
from __future__ import annotations

import sqlite3

import pytest

from app import database


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from app import config

    path = str(tmp_path / "unit.db")
    monkeypatch.setenv("TENDER_DB_PATH", path)
    monkeypatch.setattr(database, "settings", config.Settings(db_path=path))
    database.init_db(path)
    return path


def _row(code: str, deadline: str, *, kind: str = "original") -> dict:
    return {
        "code": code,
        "title": f"标题 {code}",
        "kind": kind,
        "project_name": "项目 P",
        "published_at": "2026-10-01 10:00",
        "deadline": deadline,
        "corrects_code": None,
        "detail_url": f"http://site/{code}.html",
    }


def test_publish_and_republish_same_source(db):
    site = "http://site/list_1.html"
    run1, seq1 = database.begin_run(site)
    result = database.publish_run(run1, seq1, site, [_row("A", "2026-11-01 10:00")])
    assert result.published is True
    assert result.published_run_id == run1

    run2, seq2 = database.begin_run(site)
    assert seq2 == seq1 + 1
    result2 = database.publish_run(run2, seq2, site, [_row("A", "2026-12-01 10:00")])
    assert result2.published is True

    with database.get_conn(db) as conn:
        ann = conn.execute("SELECT * FROM announcements").fetchall()
        state = conn.execute("SELECT * FROM source_state").fetchone()
        runs = conn.execute("SELECT * FROM collection_runs ORDER BY id").fetchall()

    assert len(ann) == 1
    assert ann[0]["deadline"] == "2026-12-01 10:00"
    assert ann[0]["published_run_id"] == run2
    assert state["published_run_id"] == run2
    assert state["published_seq"] == seq2
    assert [r["status"] for r in runs] == ["success", "success"]
    assert [r["published"] for r in runs] == [1, 1]


def test_older_run_is_unpublished_and_writes_nothing(db):
    site = "http://site/list_1.html"
    old_run, old_seq = database.begin_run(site)
    new_run, new_seq = database.begin_run(site)

    new_result = database.publish_run(new_run, new_seq, site, [_row("B", "2026-12-01 10:00")])
    assert new_result.published is True

    old_result = database.publish_run(old_run, old_seq, site, [_row("B", "2026-11-01 10:00")])
    assert old_result.published is False
    assert old_result.superseded_by_run_id == new_run

    with database.get_conn(db) as conn:
        rows = conn.execute("SELECT * FROM announcements").fetchall()
        runs = {r["id"]: r for r in conn.execute("SELECT * FROM collection_runs")}

    assert len(rows) == 1
    assert rows[0]["deadline"] == "2026-12-01 10:00"
    assert rows[0]["published_run_id"] == new_run
    assert runs[old_run]["status"] == "unpublished"
    assert runs[old_run]["published"] == 0
    assert runs[old_run]["items_collected"] == 1
    assert runs[new_run]["status"] == "success"
    assert runs[new_run]["published"] == 1


def test_failed_newer_run_does_not_block_older_publish(db):
    site = "http://site/list_1.html"
    old_run, old_seq = database.begin_run(site)
    new_run, _ = database.begin_run(site)
    database.mark_run_failed(new_run, error="boom")

    result = database.publish_run(old_run, old_seq, site, [_row("C", "2026-12-01 10:00")])
    assert result.published is True

    with database.get_conn(db) as conn:
        runs = {r["id"]: r for r in conn.execute("SELECT * FROM collection_runs")}
        ann = conn.execute("SELECT * FROM announcements").fetchall()
    assert runs[new_run]["status"] == "failed"
    assert runs[new_run]["published"] == 0
    assert runs[old_run]["status"] == "success"
    assert ann[0]["published_run_id"] == old_run


def test_snapshot_replacement_removes_vanished_codes(db):
    site = "http://site/list_1.html"
    run1, seq1 = database.begin_run(site)
    database.publish_run(
        run1, seq1, site, [_row("A", "2026-11-01 10:00"), _row("B", "2026-11-02 10:00")]
    )
    run2, seq2 = database.begin_run(site)
    database.publish_run(run2, seq2, site, [_row("A", "2026-11-01 10:00")])

    with database.get_conn(db) as conn:
        codes = [r["code"] for r in conn.execute("SELECT code FROM announcements")]
    assert codes == ["A"]


def test_begin_run_seq_is_per_source(db):
    a, b = "http://a/list.html", "http://b/list.html"
    _, seq_a1 = database.begin_run(a)
    _, seq_b1 = database.begin_run(b)
    _, seq_a2 = database.begin_run(a)
    assert (seq_a1, seq_b1, seq_a2) == (1, 1, 2)


def test_legacy_database_migrates_and_keeps_history(tmp_path, monkeypatch):
    """旧版 schema（无 seq/published/source_url）升级后仍可正常分配轮次。"""
    from app import config

    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('original', 'correction')),
            project_name TEXT NOT NULL, published_at TEXT NOT NULL,
            deadline TEXT, corrects_code TEXT, detail_url TEXT NOT NULL,
            last_seen_at TEXT NOT NULL);
        CREATE TABLE collection_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_url TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
            items_collected INTEGER NOT NULL DEFAULT 0, error TEXT,
            started_at TEXT NOT NULL, finished_at TEXT NOT NULL);
        INSERT INTO collection_runs
            (source_url, status, items_collected, error, started_at, finished_at)
        VALUES ('http://x/list.html', 'success', 1, NULL,
                '2026-11-01T00:00:00+00:00', '2026-11-01T00:00:01+00:00');
        INSERT INTO announcements
            (code, title, kind, project_name, published_at, deadline,
             corrects_code, detail_url, last_seen_at)
        VALUES ('C1', 't', 'original', 'P', '2026-10-01 10:00',
                '2026-12-01 10:00', NULL, 'http://x/C1.html',
                '2026-11-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("TENDER_DB_PATH", path)
    monkeypatch.setattr(database, "settings", config.Settings(db_path=path))
    database.init_db(path)

    with database.get_conn(path) as c:
        run = c.execute("SELECT * FROM collection_runs").fetchone()
        ann = c.execute("SELECT * FROM announcements").fetchone()
        state = c.execute("SELECT * FROM source_state").fetchone()
    assert run["status"] == "success"
    assert run["published"] == 1
    assert ann["source_url"] == "http://x/list.html"
    assert ann["published_run_id"] == run["id"]
    assert state["published_seq"] == 0

    run_id, seq = database.begin_run("http://x/list.html")
    assert seq == 1
    assert run_id > run["id"]
