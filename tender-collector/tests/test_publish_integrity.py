"""发布完整性测试：并行抓取、快照替换、来源/生效轮次联动。"""
from __future__ import annotations

import pytest

PROJECT = "湖滨公园配套市政道路工程"
PROJECT_DETAIL = "/SZ-2026-005-G1.html"


def _collect(client, base, status: int = 200) -> dict:
    resp = client.post("/api/collect", json={"list_url": f"{base}/list_1.html"})
    assert resp.status_code == status, resp.text
    return resp.json()


def _projects(client) -> dict[str, dict]:
    return {p["project_name"]: p for p in client.get("/api/projects").json()}


def test_detail_pages_are_fetched_in_parallel(client, demo_server):
    """同一入口的详情页可并行抓取：慢页同时读取时并发峰值 > 1。"""
    c, main, database = client
    base, control = demo_server

    control.slow("/*.html", delay=0.25, times=None)
    try:
        payload = _collect(c, base)
        assert payload["collected"] == 10
        status = control.status()
    finally:
        control.reset()

    assert status["peak_concurrency"] >= 3, status


def test_republish_replaces_source_snapshot(client, demo_server):
    """重新发布时以本轮结果替换该入口快照，消失的公告不会残留。"""
    c, main, database = client
    base, control = demo_server

    _collect(c, base)
    with database.get_conn() as conn:
        before = {
            r["code"]
            for r in conn.execute(
                "SELECT code FROM announcements WHERE source_url = ?",
                (f"{base}/list_1.html",),
            )
        }
    assert "SZ-2026-005-G1" in before

    # 页面公告编号变更：旧编号从快照中消失
    control.attr(PROJECT_DETAIL, "data-code", "SZ-2026-005-G1-NEW")
    payload = _collect(c, base)
    assert payload["published"] is True

    with database.get_conn() as conn:
        after = {
            r["code"]
            for r in conn.execute(
                "SELECT code FROM announcements WHERE source_url = ?",
                (f"{base}/list_1.html",),
            )
        }
        leaked = conn.execute(
            "SELECT COUNT(*) FROM announcements WHERE code = 'SZ-2026-005-G1'"
        ).fetchone()[0]
    assert "SZ-2026-005-G1-NEW" in after
    assert "SZ-2026-005-G1" not in after
    assert leaked == 0
    assert len(after) == len(before)

    # 查询仍由新编号给出截止时间
    assert _projects(c)[PROJECT]["deadline_source_code"] == "SZ-2026-005-G1-NEW"
    control.reset()


def test_source_state_and_run_published_flag_agree(client, demo_server):
    c, main, database = client
    base, control = demo_server

    payload = _collect(c, base)
    source_url = f"{base}/list_1.html"
    with database.get_conn() as conn:
        state = conn.execute("SELECT * FROM source_state").fetchone()
        run = conn.execute(
            "SELECT * FROM collection_runs WHERE id = ?", (payload["run_id"],)
        ).fetchone()
        rows = conn.execute(
            "SELECT published_run_id FROM announcements WHERE source_url = ?",
            (source_url,),
        ).fetchall()

    assert state["published_run_id"] == payload["run_id"]
    assert state["published_seq"] == payload["seq"]
    assert run["status"] == "success"
    assert run["published"] == 1
    assert all(r["published_run_id"] == payload["run_id"] for r in rows)
    assert c.get("/api/sources").json()[0]["published_run_id"] == payload["run_id"]


def test_non_success_rounds_never_publish_rows(client, demo_server):
    """失败轮次（网络错误/非 2xx/归并失败）都不得写公告或来源状态。"""
    c, main, database = client
    base, control = demo_server

    for inject in (
        lambda: control.fail(PROJECT_DETAIL, status=404),
        lambda: control.fail(PROJECT_DETAIL, status=500),
        lambda: control.attr(PROJECT_DETAIL, "data-corrects", "SZ-MISSING"),
    ):
        control.reset()
        inject()
        resp = c.post("/api/collect", json={"list_url": f"{base}/list_1.html"})
        assert resp.status_code == 422
        with database.get_conn() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM announcements"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM source_state"
            ).fetchone()[0] == 0
    control.reset()
