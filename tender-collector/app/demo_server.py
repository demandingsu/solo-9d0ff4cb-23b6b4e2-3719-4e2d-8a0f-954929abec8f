"""本地示例公告站 HTTP 服务（离线可用的演示/测试替身）。

除了托管 ``demo_site/`` 静态页面外，还提供可编程的控制接口，用来复现
"慢页 / 故障页 / 页面内容已更新" 等采集场景：

    GET  /__control/status          查看当前生效的慢页/故障页/内容覆盖/并发峰值
    POST /__control/slow            {"path": "/X.html", "delay": 3.0, "times": 1}
    POST /__control/fail            {"path": "/list_2.html", "status": 404, "times": 1}
    POST /__control/override        {"path": "/X.html", "field": "投标截止时间",
                                     "value": "2026-12-31 10:00"}
    POST /__control/attr            {"path": "/X.html", "attribute": "data-corrects",
                                     "value": "X-0"}
    POST /__control/clear_slow      清空慢页规则
    POST /__control/clear_fail      清空故障页规则
    POST /__control/reset           清空全部控制项

``path`` 支持 fnmatch 通配；``times`` 省略表示一直生效。慢页在发送前先
取好页面快照，再按延迟返回，因此延迟期间对页面内容的覆盖不会影响已经在
读取中的旧轮次——正好复现"旧轮还在读旧页面"的场景。

运行：

    python -m app.demo_server --directory demo_site --port 8001
"""
from __future__ import annotations

import argparse
import fnmatch
import html
import json
import mimetypes
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

CONTROL_PREFIX = "/__control"


@dataclass
class _Rule:
    pattern: str
    remaining: int | None = None  # None 表示无限次

    def matches(self, path: str) -> bool:
        return fnmatch.fnmatch(path, self.pattern)


@dataclass
class _Controls:
    lock: threading.Lock = field(default_factory=threading.Lock)
    slow: list[tuple[_Rule, float]] = field(default_factory=list)
    fail: list[tuple[_Rule, int]] = field(default_factory=list)
    overrides: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    attr_overrides: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    active: int = 0
    peak: int = 0

    def reset(self) -> None:
        with self.lock:
            self.slow.clear()
            self.fail.clear()
            self.overrides.clear()
            self.attr_overrides.clear()
            self.active = 0
            self.peak = 0

    def enter(self) -> None:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)

    def leave(self) -> None:
        with self.lock:
            self.active = max(0, self.active - 1)

    def add_slow(self, pattern: str, delay: float, times: int | None) -> None:
        with self.lock:
            self.slow.append((_Rule(pattern, times), delay))

    def add_fail(self, pattern: str, status: int, times: int | None) -> None:
        with self.lock:
            self.fail.append((_Rule(pattern, times), status))

    def set_override(self, path: str, field: str, value: str) -> None:
        with self.lock:
            self.overrides[path] = [
                (f, v) for f, v in self.overrides.get(path, []) if f != field
            ] + [(field, value)]

    def set_attr_override(self, path: str, attr: str, value: str) -> None:
        with self.lock:
            self.attr_overrides[path] = [
                (a, v) for a, v in self.attr_overrides.get(path, []) if a != attr
            ] + [(attr, str(value))]

    def take_slow(self, path: str) -> float:
        with self.lock:
            delay = 0.0
            for rule, seconds in self.slow:
                if rule.remaining == 0:
                    continue
                if rule.matches(path):
                    delay = max(delay, seconds)
                    if rule.remaining is not None:
                        rule.remaining -= 1
                    break
            return delay

    def take_fail(self, path: str) -> int | None:
        with self.lock:
            for rule, status in self.fail:
                if rule.remaining == 0:
                    continue
                if rule.matches(path):
                    if rule.remaining is not None:
                        rule.remaining -= 1
                    return status
            return None

    def apply_overrides(self, path: str, text: str) -> str:
        with self.lock:
            rules = list(self.overrides.get(path, []))
            attr_rules = list(self.attr_overrides.get(path, []))
        for name, value in rules:
            pattern = re.compile(
                r'(<th\b[^>]*data-field="'
                + re.escape(name)
                + r'"[^>]*>.*?</th>\s*<td\b[^>]*>)(.*?)(</td>)',
                re.S,
            )
            text = pattern.sub(
                lambda m: m.group(1) + html.escape(value) + m.group(3),
                text,
                count=1,
            )
        for attr, value in attr_rules:
            # 同时覆盖 data-* 属性和表格字段，便于演示“页面数据变化”
            pattern = re.compile(
                rf'(<article\b[^>]*\b{re.escape(attr)}=")[^"]*(")', re.S
            )
            text = pattern.sub(
                lambda m: m.group(1) + html.escape(value) + m.group(2),
                text,
                count=1,
            )
        return text

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "active_requests": self.active,
                "peak_concurrency": self.peak,
                "slow": [
                    {"path": r.pattern, "remaining": r.remaining, "delay": d}
                    for r, d in self.slow
                ],
                "fail": [
                    {"path": r.pattern, "remaining": r.remaining, "status": s}
                    for r, s in self.fail
                ],
                "overrides": {
                    p: [{"field": f, "value": v} for f, v in rules]
                    for p, rules in self.overrides.items()
                },
                "attr_overrides": {
                    p: [{"attribute": a, "value": v} for a, v in rules]
                    for p, rules in self.attr_overrides.items()
                },
            }


CONTROLS = _Controls()


class DemoHandler(BaseHTTPRequestHandler):
    """托管静态示例站，并执行 CONTROLS 中的慢页/故障页/内容覆盖规则。"""

    directory: Path = Path("demo_site")
    quiet: bool = True
    server_version = "TenderDemoSite/1.0"

    # -- helpers ---------------------------------------------------------
    def _file_for(self, path: str) -> Path | None:
        rel = unquote(path).lstrip("/")
        if not rel:
            rel = "index.html"
        target = (self.directory / rel).resolve()
        try:
            target.relative_to(self.directory.resolve())
        except ValueError:
            return None
        if target.is_dir():
            target = target / "index.html"
        return target if target.is_file() else None

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON: {exc}") from exc

    def _handle_control(self, method: str, path: str) -> None:
        action = path[len(CONTROL_PREFIX):].strip("/")
        try:
            if method == "GET" and action in ("", "status"):
                self._send_json({"ok": True, **CONTROLS.snapshot()})
                return
            if method != "POST":
                self._send_json({"ok": False, "error": "method not allowed"}, 405)
                return
            if action == "reset":
                CONTROLS.reset()
                self._send_json({"ok": True, "reset": True})
                return
            if action == "clear_slow":
                with CONTROLS.lock:
                    CONTROLS.slow.clear()
                self._send_json({"ok": True, **CONTROLS.snapshot()})
                return
            if action == "clear_fail":
                with CONTROLS.lock:
                    CONTROLS.fail.clear()
                self._send_json({"ok": True, **CONTROLS.snapshot()})
                return
            payload = self._read_json()
            if action == "slow":
                pattern = str(payload["path"])
                CONTROLS.add_slow(pattern, float(payload.get("delay", 2.0)),
                                  payload.get("times"))
            elif action == "fail":
                pattern = str(payload["path"])
                CONTROLS.add_fail(pattern, int(payload.get("status", 404)),
                                  payload.get("times"))
            elif action == "override":
                CONTROLS.set_override(
                    str(payload["path"]),
                    str(payload.get("field", payload.get("attribute", ""))),
                    str(payload["value"]),
                )
            elif action == "attr":
                CONTROLS.set_attr_override(
                    str(payload["path"]),
                    str(payload["attribute"]),
                    str(payload["value"]),
                )
            else:
                self._send_json({"ok": False, "error": "unknown control"}, 404)
                return
        except (KeyError, TypeError, ValueError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
            return
        self._send_json({"ok": True, **CONTROLS.snapshot()})

    # -- HTTP ------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith(CONTROL_PREFIX):
            self._handle_control("GET", parsed.path)
            return

        CONTROLS.enter()
        try:
            self._serve_static(parsed.path)
        finally:
            CONTROLS.leave()

    def _serve_static(self, path: str) -> None:
        status = CONTROLS.take_fail(path)
        if status is not None:
            self.send_error(status, "injected failure")
            return

        target = self._file_for(path)
        if target is None:
            self.send_error(404, "File not found")
            return

        # 先取页面快照，再延迟返回：延迟期间的内容更新不影响"已在读取"的旧请求
        text = target.read_text(encoding="utf-8")
        text = CONTROLS.apply_overrides(path, text)
        delay = CONTROLS.take_slow(path)
        if delay:
            time.sleep(delay)

        body = text.encode("utf-8")
        content_type = mimetypes.guess_type(target.name)[0] or "text/plain"
        if content_type.startswith("text/"):
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith(CONTROL_PREFIX):
            self._handle_control("POST", parsed.path)
            return
        self.send_error(405, "method not allowed")

    def log_message(self, fmt, *args):  # noqa: D401
        if not self.quiet:
            super().log_message(fmt, *args)


def create_server(
    directory: str | Path,
    host: str = "127.0.0.1",
    port: int = 8001,
    quiet: bool = True,
) -> ThreadingHTTPServer:
    handler = type(
        "BoundDemoHandler",
        (DemoHandler,),
        {"directory": Path(directory).resolve(), "quiet": quiet},
    )
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default="demo_site")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--base-url", default="", help="仅用于启动日志提示")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    server = create_server(args.directory, args.host, args.port, not args.verbose)
    base = args.base_url or f"http://{args.host}:{args.port}"
    print(f"demo site: {base}/list_1.html  (control: {base}/__control/status)",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
