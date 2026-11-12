"""采集编排服务：分配轮次 -> 抓取 -> 归并校验 -> 单事务发布。

发布顺序约束（同一入口 list_url 内）：
* 后发起且先成功的轮次立即发布、立即可查；
* 旧轮随后完成时若发现已有更晚发起且已发布的轮次，则本轮标记为
  unpublished，公告、来源与生效轮次均不回写，并明确告知调用方；
* 后发起但失败的轮次不发布；先发起且完整成功的轮次仍会正常发布；
* 失败或未发布的轮次不留下任何公告片段。
"""
from __future__ import annotations

from datetime import datetime

from . import database
from .merge import EffectiveDeadline, MergeError, merge_projects
from .scraper import CollectionError, collect

TIME_FORMAT = "%Y-%m-%d %H:%M"


class ServiceError(Exception):
    """对外统一的采集错误（带可读原因）。"""


class NotPublished(ServiceError):
    """本轮采集完整成功，但因已有更晚发起且已发布的轮次而不回写。"""

    def __init__(
        self,
        message: str,
        *,
        run_id: int,
        seq: int,
        published_run_id: int,
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.seq = seq
        self.published_run_id = published_run_id


def _is_expired(deadline_text: str, now: datetime | None = None) -> bool:
    try:
        deadline = datetime.strptime(deadline_text, TIME_FORMAT)
    except ValueError:
        # 容错：支持带秒或 ISO 形式
        deadline = datetime.fromisoformat(deadline_text)
    return deadline <= (now or datetime.now())


def _to_project(d: EffectiveDeadline, now: datetime | None = None) -> dict:
    return {
        "project_name": d.project_name,
        "current_deadline": d.deadline,
        "deadline_source_code": d.source_code,
        "deadline_source_url": d.source_url,
        "is_expired": _is_expired(d.deadline, now),
    }


def build_projects(rows: list[dict], now: datetime | None = None) -> list[dict]:
    return [_to_project(d, now) for d in merge_projects(rows)]


def run_collection(list_url: str) -> dict:
    """执行一轮采集并尝试发布，返回可直接序列化的结果字典。

    成功发布的轮次返回 ``status="success"``、``published=True``；
    若轮次完整成功但已有更晚发起且已发布的轮次，抛 NotPublished（对应
    HTTP 409），本轮运行记录为 ``unpublished``。抓取/归并失败抛 ServiceError
    （对应 HTTP 422）。任何失败或未发布轮次都不会写入公告数据。
    """
    # 先登记轮次，拿到同一入口内单调递增的 seq。之后所有抓取/归并都在
    # 内存中完成，只有成功结果才会进入发布事务。
    started_at = database.utcnow_iso()
    run_id, seq = database.begin_run(list_url, started_at)

    try:
        raw = collect(list_url)
        # 先在内存完成归并校验：数据有问题时不发布任何内容
        projects = build_projects(raw)
    except (CollectionError, MergeError) as exc:
        database.mark_run_failed(run_id, error=str(exc))
        raise ServiceError(str(exc)) from exc
    except Exception as exc:  # pragma: no cover - 兜底，避免轮次停在 running
        database.mark_run_failed(run_id, error=f"采集异常: {exc}")
        raise

    try:
        published = database.publish_run(run_id, seq, list_url, raw)
    except Exception as exc:
        database.mark_run_failed(
            run_id, error=f"写入数据库失败: {exc}", items_collected=len(raw)
        )
        raise ServiceError(f"写入数据库失败: {exc}") from exc

    if not published.published:
        raise NotPublished(
            "本轮采集已完整完成，但同一入口已有后发起且已成功的轮次生效，"
            f"本轮未发布（run_id={run_id}, seq={seq}）",
            run_id=run_id,
            seq=seq,
            published_run_id=published.published_run_id,
        )

    return {
        "status": "success",
        "source_url": list_url,
        "run_id": run_id,
        "seq": seq,
        "published": True,
        "published_run_id": published.published_run_id,
        "collected": len(raw),
        "projects": projects,
        "started_at": started_at,
    }


def query_projects(now: datetime | None = None) -> list[dict]:
    with database.get_conn() as conn:
        rows = [dict(r) for r in database.fetch_all_announcements(conn)]
    if not rows:
        return []
    return build_projects(rows, now)
