from __future__ import annotations

from pydantic import BaseModel, Field


class CollectRequest(BaseModel):
    list_url: str = Field(
        ..., min_length=1, description="公告列表页地址（服务会自动跟随分页链接）"
    )


class ProjectOut(BaseModel):
    project_name: str
    current_deadline: str
    deadline_source_code: str
    deadline_source_url: str
    is_expired: bool


class CollectResponse(BaseModel):
    status: str
    source_url: str
    run_id: int
    seq: int
    published: bool
    published_run_id: int
    collected: int
    projects: list[ProjectOut]
    started_at: str
    finished_at: str | None = None


class RunOut(BaseModel):
    id: int
    seq: int
    source_url: str
    status: str
    published: bool
    items_collected: int
    error: str | None
    started_at: str
    finished_at: str | None


class SourceStateOut(BaseModel):
    source_url: str
    published_run_id: int
    published_seq: int
    published_at: str
