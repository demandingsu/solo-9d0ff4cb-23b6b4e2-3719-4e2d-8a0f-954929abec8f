from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from . import database
from .models import (
    CollectRequest,
    CollectResponse,
    ProjectOut,
    RunOut,
    SourceStateOut,
)
from .service import NotPublished, ServiceError, query_projects, run_collection


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title="市政工程投标截止时间采集 API",
    description=(
        "提交公告列表页地址，服务抓取全部详情页，将更正公告按引用的原公告编号归并，"
        "按发布时间确定生效的投标截止时间。重复采集不产生重复记录；"
        "任一详情页读取失败时整体报错并保留上次成功采集结果。"
        "同一入口的页面抓取可并行；后发起且先成功的轮次立即生效，"
        "旧轮随后结束不会覆盖它（返回 409 并标记 unpublished）。"
    ),
    version="2.0.0",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"service": "tender-collector", "docs": "/docs"}


@app.post("/api/collect", response_model=CollectResponse, status_code=200)
def collect_endpoint(req: CollectRequest) -> CollectResponse:
    try:
        result = run_collection(req.list_url)
    except NotPublished as exc:
        # 409 语义：本轮采集完整成功，但不是最新发起的轮次，未发布生效
        raise HTTPException(
            status_code=409,
            detail={
                "status": "unpublished",
                "message": str(exc),
                "run_id": exc.run_id,
                "seq": exc.seq,
                "published_run_id": exc.published_run_id,
            },
        ) from exc
    except ServiceError as exc:
        # 422 语义：采集到的源站内容/可用性有问题，本次未发布
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return CollectResponse(**result)


@app.get("/api/projects", response_model=list[ProjectOut])
def list_projects() -> list[ProjectOut]:
    return [ProjectOut(**p) for p in query_projects()]


@app.get("/api/collections", response_model=list[RunOut])
def list_collections(limit: int = Query(20, ge=1, le=200)) -> list[RunOut]:
    with database.get_conn() as conn:
        return [RunOut(**dict(r)) for r in database.fetch_runs(conn, limit)]


@app.get("/api/sources", response_model=list[SourceStateOut])
def list_sources() -> list[SourceStateOut]:
    """每个采集入口当前生效的轮次。"""
    with database.get_conn() as conn:
        return [SourceStateOut(**dict(r)) for r in database.fetch_source_states(conn)]
