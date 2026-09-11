"""Pydantic models shared by the API and the frontend events."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

EventType = Literal[
    "task_started",
    "agent_thinking",
    "agent_action",
    "browser_connected",
    "browser_disconnected",
    "task_completed",
    "task_failed",
    "task_stopped",
    "task_busy",
    "file",
    "log",
    "error",
]


class Event(BaseModel):
    """A single WebSocket event sent to the browser."""

    type: EventType
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class LoginRequest(BaseModel):
    password: str


class TaskRequest(BaseModel):
    task: str = Field(min_length=1, max_length=4000)
    files: list[str] = Field(default_factory=list)


class TaskResponse(BaseModel):
    ok: bool
    message: str = ""
    task_id: str | None = None


class UploadedFile(BaseModel):
    id: str
    name: str
    size: int


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    browser: Literal["connected", "disconnected", "starting", "unknown"]
    llm: Literal["configured", "missing"]
    auth: Literal["configured", "missing"]


class BrowserStatus(BaseModel):
    status: str
    cdp_url: str
