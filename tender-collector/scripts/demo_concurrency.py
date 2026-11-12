#!/usr/bin/env python3
"""离线演示：并发采集的发布顺序。

不依赖 Docker，也不需要外网：

    python3 scripts/demo_concurrency.py            # 启动本地慢页/故障页示例站
                                                   # 并演示两轮采集
    python3 scripts/demo_concurrency.py --keep     # 演示后保持示例站运行

脚本会启动可控示例站（--port 默认 8765，控制接口 /__control），并用临时
SQLite 数据库启动采集 API（--db 可复用指定库，--keep 演示后保持示例站运行）：

1. 场景一：旧轮读取慢页期间更新页面，后发起的轮次先成功并立即生效，
   旧轮结束时返回 409 且标记 unpublished，查询仍是新截标时间；
2. 场景二：后轮注入故障，先发起的完整成功轮次仍可发布。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _post_json(base: str, path: str, payload: dict) -> dict:
    import httpx

    resp = httpx.post(base + path, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _control(base: str, action: str, payload: dict | None = None) -> dict:
    return _post_json(base, f"/__control/{action}", payload or {})


def _get(base: str, path: str):
    import httpx

    return httpx.get(base + path, timeout=10).json()


def _api_get(api, path: str):
    return api.get(path).json()


def _wait_slow_consumed(base: str, pattern: str, timeout: float = 10.0) -> None:
    """等到慢页规则被旧轮命中（说明旧轮已拿到旧页面并在延迟中）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = _get(base, "/__control/status")
        for rule in status["slow"]:
            if rule["path"] == pattern and (
                rule["remaining"] == 0 or status["active_requests"] > 0
            ):
                return
        time.sleep(0.05)
    raise AssertionError(f"慢页未被命中: {pattern}")


def _projects(api) -> dict[str, dict]:
    return {p["project_name"]: p for p in _api_get(api, "/api/projects")}


def scenario_late_old_round(api, site: str, base: str) -> None:
    print("\n=== 场景一：旧轮晚完成，不得覆盖后发起且先成功的结果 ===")
    old_deadline = "2026-11-20 15:00"
    new_deadline = "2026-12-11 15:00"
    project = "湖滨公园配套市政道路工程"

    # 旧轮开始读取更正公告页时把它拖慢；页面快照在延迟前已取好（旧时间）
    _control(base, "slow", {"path": "/SZ-2026-005-G1.html", "delay": 3.0, "times": 1})
    old_result: dict = {}

    def run_old() -> None:
        old_result["resp"] = api.post(
            "/api/collect", json={"list_url": f"{site}/list_1.html"}
        )

    old_thread = threading.Thread(target=run_old, daemon=True)
    old_thread.start()
    _wait_slow_consumed(base, "/SZ-2026-005-G1.html")  # 旧轮已在延迟中

    # 更新页面；后发起的轮次不再命中慢页（times=1 已用完），立刻成功
    _control(base, "clear_slow")
    _control(
        base,
        "override",
        {"path": "/SZ-2026-005-G1.html", "field": "投标截止时间", "value": new_deadline},
    )
    new_resp = api.post("/api/collect", json={"list_url": f"{site}/list_1.html"})
    print(f"后发起轮次: HTTP {new_resp.status_code} "
          f"{json.dumps(new_resp.json(), ensure_ascii=False)[:160]}...")
    assert new_resp.status_code == 200, new_resp.text

    queried = _projects(api)[project]
    print(f"后轮成功后立即查询: {project} -> {queried['current_deadline']}")
    assert queried["current_deadline"] == new_deadline

    old_thread.join(timeout=15)
    old_resp = old_result.get("resp")
    assert old_resp is not None, "旧轮未在预期时间内结束"
    body = old_resp.json()
    print(f"旧轮结束: HTTP {old_resp.status_code} -> "
          f"{json.dumps(body, ensure_ascii=False)}")
    assert old_resp.status_code == 409
    assert body["detail"]["status"] == "unpublished"

    queried = _projects(api)[project]
    print(f"旧轮结束后查询: {project} -> {queried['current_deadline']}")
    assert queried["current_deadline"] == new_deadline, "旧轮覆盖了后轮结果"

    runs = _api_get(api, "/api/collections?limit=3")
    seq = [(r["id"], r["seq"], r["status"], r["published"]) for r in runs]
    print(f"运行记录 (id, seq, status, published): {seq}")
    assert any(r["status"] == "unpublished" for r in runs)
    _control(base, "reset")
    print("场景一通过：后发起且先成功的轮次立即可查，旧轮未发布。")


def scenario_older_round_still_publishes(api, site: str, base: str) -> None:
    print("\n=== 场景二：后轮失败，先发起的完整成功轮次仍可发布 ===")
    project = "滨河路市政改造工程"
    newer_deadline = "2026-12-20 14:30"

    _control(base, "slow", {"path": "/SZ-2026-001-G1.html", "delay": 2.5, "times": 1})
    old_result: dict = {}

    def run_old() -> None:
        old_result["resp"] = api.post(
            "/api/collect", json={"list_url": f"{site}/list_1.html"}
        )

    old_thread = threading.Thread(target=run_old, daemon=True)
    old_thread.start()
    _wait_slow_consumed(base, "/SZ-2026-001-G1.html")  # 旧轮已在延迟中

    _control(base, "clear_slow")
    # 页面内容更新为新截止时间；旧轮已取好旧快照，会发布旧值
    _control(
        base,
        "override",
        {"path": "/SZ-2026-001-G1.html",
         "field": "投标截止时间", "value": newer_deadline},
    )
    # 后发起的轮次遇到故障页，整体失败、不发布；故障只注入一次
    _control(base, "fail", {"path": "/SZ-2026-002-G2.html", "status": 500, "times": 1})
    new_resp = api.post("/api/collect", json={"list_url": f"{site}/list_1.html"})
    print(f"后发起轮次（故障页）: HTTP {new_resp.status_code} "
          f"{json.dumps(new_resp.json(), ensure_ascii=False)[:160]}...")
    assert new_resp.status_code == 422

    old_thread.join(timeout=15)
    old_resp = old_result.get("resp")
    assert old_resp is not None
    print(f"先发起轮次结束: HTTP {old_resp.status_code} -> status="
          f"{old_resp.json().get('status')} seq={old_resp.json().get('seq')}")
    assert old_resp.status_code == 200

    # 旧轮读取的是旧快照，因此发布的是旧截止时间；重点是它完整发布成功，
    # 没有被后发起的失败轮次挡住
    queried = _projects(api)[project]
    print(f"查询: {project} -> {queried['current_deadline']} "
          f"(来源 {queried['deadline_source_code']})")
    assert queried["current_deadline"] == "2026-11-25 14:30"
    assert queried["deadline_source_code"] == "SZ-2026-001-G1"

    # 再等故障规则耗尽后补采一轮，新截标时间随之生效
    _control(base, "clear_fail")
    follow = api.post("/api/collect", json={"list_url": f"{site}/list_1.html"})
    assert follow.status_code == 200
    queried = _projects(api)[project]
    print(f"恢复采集后查询: {project} -> {queried['current_deadline']}")
    assert queried["current_deadline"] == newer_deadline

    runs = _api_get(api, "/api/collections?limit=4")
    seq = [(r["id"], r["seq"], r["status"], r["published"]) for r in runs]
    print(f"运行记录 (id, seq, status, published): {seq}")
    assert any(r["status"] == "failed" for r in runs)
    _control(base, "reset")
    print("场景二通过：失败后轮未留下片段，先发起轮次仍完整发布。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default="", help="复用指定 SQLite 文件（默认临时库）")
    parser.add_argument("--keep", action="store_true", help="演示后保持示例站运行")
    args = parser.parse_args()

    import os
    import tempfile

    if args.db:
        os.environ["TENDER_DB_PATH"] = args.db
    else:
        tmpdir = tempfile.mkdtemp(prefix="tender-demo-")
        os.environ["TENDER_DB_PATH"] = str(Path(tmpdir) / "tenders.db")

    from fastapi.testclient import TestClient

    from app import database
    from app.demo_server import create_server
    import app.main as main_module

    database.init_db()
    server = create_server(ROOT / "demo_site", "127.0.0.1", args.port, quiet=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    site = f"http://127.0.0.1:{args.port}"

    try:
        with TestClient(main_module.app) as api:
            scenario_late_old_round(api, site, site)
            scenario_older_round_still_publishes(api, site, site)
            print("\n全部演示通过。当前生效状态：", json.dumps(
                _projects(api), ensure_ascii=False, indent=2
            ))
    finally:
        if args.keep:
            print(f"示例站保持运行：{site}/list_1.html（Ctrl+C 退出）")
            try:
                thread.join()
            except KeyboardInterrupt:
                pass
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
