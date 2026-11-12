"""共享测试夹具：临时数据库 + 可控示例站。"""
from __future__ import annotations

import importlib
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


def _load_generator():
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "generate_demo_site", ROOT / "scripts" / "generate_demo_site.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["generate_demo_site"] = mod
    spec.loader.exec_module(mod)
    return mod


gen = _load_generator()


class DemoControl:
    """示例站控制接口的薄封装。"""

    def __init__(self, base: str) -> None:
        self.base = base

    def _post(self, action: str, payload: dict | None = None) -> dict:
        return httpx.post(
            f"{self.base}/__control/{action}", json=payload or {}, timeout=10
        ).json()

    def slow(self, path: str, delay: float, times: int | None = None) -> dict:
        return self._post("slow", {"path": path, "delay": delay, "times": times})

    def fail(self, path: str, status: int = 404, times: int | None = None) -> dict:
        return self._post("fail", {"path": path, "status": status, "times": times})

    def override(self, path: str, field: str, value: str) -> dict:
        return self._post(
            "override", {"path": path, "field": field, "value": value}
        )

    def attr(self, path: str, attribute: str, value: str) -> dict:
        return self._post(
            "attr", {"path": path, "attribute": attribute, "value": value}
        )

    def clear_slow(self) -> dict:
        return self._post("clear_slow")

    def status(self) -> dict:
        return httpx.get(f"{self.base}/__control/status", timeout=10).json()

    def wait_slow_consumed(self, path: str, timeout: float = 10.0) -> None:
        """等到慢页规则已命中（请求已取到页面快照并进入延迟）。"""
        import fnmatch
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.status()
            for rule in status["slow"]:
                if not fnmatch.fnmatch(path, rule["path"]):
                    continue
                if rule["remaining"] == 0 or status["active_requests"] > 0:
                    return
            time.sleep(0.05)
        raise AssertionError(f"慢页未被采集命中: {path}")

    def reset(self) -> None:
        self._post("reset")


@pytest.fixture()
def demo_server():
    from app.demo_server import CONTROLS, create_server
    import threading

    CONTROLS.reset()
    server = create_server(ROOT / "demo_site", "127.0.0.1", 0, quiet=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    control = DemoControl(f"http://{host}:{port}")
    try:
        yield control.base, control
    finally:
        control.reset()
        server.shutdown()
        server.server_close()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 每个测试使用独立临时数据库
    monkeypatch.setenv("TENDER_DB_PATH", str(tmp_path / "test.db"))
    from app import config, database

    importlib.reload(config)
    importlib.reload(database)
    import app.main as main

    importlib.reload(main)
    database.init_db()
    with TestClient(main.app) as c:
        yield c, main, database
