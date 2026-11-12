"""并发采集的发布顺序测试：慢页、故障页、后发起轮次先成功等场景。

约束（同一入口 list_url 内）：
1. 后发起且先成功的轮次立即生效、立即可查；
2. 旧轮随后完成不得覆盖它，必须明确返回未发布（409 + unpublished 记录）；
3. 后轮抓取/归并失败时，先发起且完整成功的轮次仍可发布；
4. 公告、来源与生效轮次一同更新；失败/未发布轮次不留下部分结果。
"""
from __future__ import annotations

import threading

import pytest

PROJECT = "湖滨公园配套市政道路工程"
OLD_DEADLINE = "2026-11-20 15:00"
NEW_DEADLINE = "2026-12-11 15:00"
PROJECT_DETAIL = "/SZ-2026-005-G1.html"


def _projects(client) -> dict[str, dict]:
    return {p["project_name"]: p for p in client.get("/api/projects").json()}

def _runs(client) -> list[dict]:
    return client.get("/api/collections?limit=50").json()


def _start_round(
    client, base, result: dict, key: str = "resp"
) -> threading.Thread:
    def run() -> None:
        result[key] = client.post(
            "/api/collect", json={"list_url": f"{base}/list_1.html"}
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _stop(thread: threading.Thread, result: dict, key: str = "resp") -> None:
    thread.join(timeout=20)
    assert not thread.is_alive(), "采集轮次未在预期时间内结束"


def test_old_round_late_finish_cannot_overwrite_newer(client, demo_server):
    c, main, database = client
    base, control = demo_server

    # 旧轮读取详情页时被拖慢：页面快照在延迟前已取好（旧截标时间）
    control.slow(PROJECT_DETAIL, delay=2.5, times=1)
    old_result: dict = {}
    old_thread = _start_round(c, base, old_result)
    control.wait_slow_consumed(PROJECT_DETAIL)  # 旧轮已在延迟中

    # 页面内容更新为新的截止时间；后发起轮次立即成功（不再命中慢页）
    control.clear_slow()
    control.override(PROJECT_DETAIL, "投标截止时间", NEW_DEADLINE)
    new_resp = c.post("/api/collect", json={"list_url": f"{base}/list_1.html"})
    assert new_resp.status_code == 200, new_resp.text
    new_body = new_resp.json()
    assert new_body["published"] is True
    assert new_body["status"] == "success"

    # 后轮先成功 -> 立即可查
    projects = _projects(c)
    assert projects[PROJECT]["current_deadline"] == NEW_DEADLINE

    # 旧轮结束：不得覆盖新结果，且明确告知本轮未发布
    _stop(old_thread, old_result)
    old_resp = old_result["resp"]
    assert old_resp.status_code == 409
    detail = old_resp.json()["detail"]
    assert detail["status"] == "unpublished"
    assert detail["run_id"] == new_body["run_id"] - 1  # 旧轮先登记的 id
    assert detail["published_run_id"] == new_body["run_id"]
    assert "未发布" in detail["message"]

    # 查询仍是新截标时间，且公告、来源、生效轮次一致
    projects = _projects(c)
    assert projects[PROJECT]["current_deadline"] == NEW_DEADLINE
    assert projects[PROJECT]["deadline_source_code"] == "SZ-2026-005-G1"
    sources = c.get("/api/sources").json()
    assert sources and sources[0]["published_run_id"] == new_body["run_id"]

    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM announcements WHERE code = 'SZ-2026-005-G1'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["deadline"] == NEW_DEADLINE
        assert rows[0]["published_run_id"] == new_body["run_id"]
        assert rows[0]["source_url"] == f"{base}/list_1.html"

    runs = _runs(c)
    unpublished = [r for r in runs if r["status"] == "unpublished"]
    assert len(unpublished) == 1
    assert unpublished[0]["id"] == detail["run_id"]
    assert unpublished[0]["published"] == 0
    assert unpublished[0]["finished_at"]

    control.reset()


def test_late_older_round_still_publishes_when_newer_fails(client, demo_server):
    c, main, database = client
    base, control = demo_server

    control.slow(PROJECT_DETAIL, delay=2.5, times=1)
    old_result: dict = {}
    old_thread = _start_round(c, base, old_result)
    control.wait_slow_consumed(PROJECT_DETAIL)

    # 后发起轮次遇到故障页 -> 整体失败、不发布
    control.clear_slow()
    control.override(PROJECT_DETAIL, "投标截止时间", NEW_DEADLINE)
    control.fail("/SZ-2026-002-G2.html", status=500, times=1)
    new_resp = c.post("/api/collect", json={"list_url": f"{base}/list_1.html"})
    assert new_resp.status_code == 422

    # 注入的故障只影响新轮：先发起的旧轮页面已取好，仍完整成功并发布
    _stop(old_thread, old_result)
    old_resp = old_result["resp"]
    assert old_resp.status_code == 200, old_resp.text
    old_body = old_resp.json()
    assert old_body["published"] is True

    projects = _projects(c)
    assert projects is not None
    # 旧轮读取的是旧快照（延迟前取页），因此发布旧截止时间
    assert projects[PROJECT]["current_deadline"] == OLD_DEADLINE
    sources = c.get("/api/sources").json()
    assert sources[0]["published_run_id"] == old_body["run_id"]

    # 新轮失败记录存在，且没有发布任何片段
    runs = _runs(c)
    failed = [r for r in runs if r["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["published"] == 0
    assert failed[0]["id"] == old_body["run_id"] + 1

    with database.get_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM announcements"
        ).fetchone()[0] == 10

    control.reset()


def test_newer_merge_failure_does_not_block_older_success(client, demo_server):
    """后轮失败可发生在归并阶段：先发起轮次仍发布，且无部分结果。"""
    c, main, database = client
    base, control = demo_server

    control.slow(PROJECT_DETAIL, delay=2.5, times=1)
    old_result: dict = {}
    old_thread = _start_round(c, base, old_result)
    control.wait_slow_consumed(PROJECT_DETAIL)

    # 让后轮归并失败：把更正公告的引用改成悬空编号（属性优先，解析必然取到）
    control.clear_slow()
    control.attr(PROJECT_DETAIL, "data-corrects", "SZ-MISSING")
    new_resp = c.post("/api/collect", json={"list_url": f"{base}/list_1.html"})
    assert new_resp.status_code == 422
    assert "SZ-MISSING" in new_resp.json()["detail"]

    _stop(old_thread, old_result)
    assert old_result["resp"].status_code == 200
    assert old_result["resp"].json()["published"] is True

    with database.get_conn() as conn:
        codes = {
            r["code"]
            for r in conn.execute("SELECT code FROM announcements").fetchall()
        }
    assert codes == {a.code for a in __import__("conftest").gen.ANNOUNCEMENTS}
    assert _projects(c)[PROJECT]["current_deadline"] == OLD_DEADLINE
    control.reset()


def test_failed_round_leaves_no_partial_rows(client, demo_server):
    """抓取失败时不得出现本轮任何公告/来源状态。"""
    c, main, database = client
    base, control = demo_server

    # 从未成功采集过的空库上直接失败
    control.fail(PROJECT_DETAIL, status=404)
    resp = c.post("/api/collect", json={"list_url": f"{base}/list_1.html"})
    assert resp.status_code == 422

    with database.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM source_state").fetchone()[0] == 0
        run = conn.execute("SELECT * FROM collection_runs").fetchone()
    assert run["status"] == "failed"
    assert run["published"] == 0
    assert run["finished_at"]
    assert c.get("/api/projects").json() == []
    control.reset()


def test_newer_round_waits_to_start_after_older_is_running(client, demo_server):
    """两轮起点严格有序：旧轮先登记，后轮后发起；后轮仍应生效。"""
    c, main, database = client
    base, control = demo_server

    control.slow(PROJECT_DETAIL, delay=2.0, times=1)
    old_result: dict = {}
    old_thread = _start_round(c, base, old_result)
    control.wait_slow_consumed(PROJECT_DETAIL)

    old_run_id, old_seq = _latest_run(c)
    assert (old_run_id, old_seq) == (1, 1)

    control.clear_slow()
    control.override(PROJECT_DETAIL, "投标截止时间", NEW_DEADLINE)
    new_resp = c.post("/api/collect", json={"list_url": f"{base}/list_1.html"})
    assert new_resp.status_code == 200
    assert new_resp.json()["seq"] == 2

    _stop(old_thread, old_result)
    assert old_result["resp"].status_code == 409
    control.reset()


def _latest_run(client) -> tuple[int, int]:
    runs = client.get("/api/collections?limit=1").json()
    return runs[0]["id"], runs[0]["seq"]


def test_concurrent_first_rounds_newest_seq_wins(client, demo_server):
    """并发启动的两轮：seq 较大的成功后，seq 较小且晚完成的不得覆盖。"""
    c, main, database = client
    base, control = demo_server

    control.slow(PROJECT_DETAIL, delay=2.0, times=1)
    old_result: dict = {}
    old_thread = _start_round(c, base, old_result)
    control.wait_slow_consumed(PROJECT_DETAIL)

    control.clear_slow()
    control.override(PROJECT_DETAIL, "投标截止时间", NEW_DEADLINE)
    new_resp = c.post("/api/collect", json={"list_url": f"{base}/list_1.html"})
    assert new_resp.status_code == 200
    new_body = new_resp.json()
    assert (new_body["run_id"], new_body["seq"]) == (2, 2)

    _stop(old_thread, old_result)
    assert old_result["resp"].status_code == 409

    rows = c.get("/api/collections?limit=10").json()
    success = [r for r in rows if r["status"] == "success"]
    assert len(success) == 1
    assert success[0]["seq"] == 2
    assert success[0]["id"] == new_body["run_id"]
    assert _projects(c)[PROJECT]["current_deadline"] == NEW_DEADLINE
    control.reset()
