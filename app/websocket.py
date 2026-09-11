"""WebSocket connection manager and application event bus."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

from fastapi import WebSocket

from app.models import Event, EventType
from app.utils import redact

logger = logging.getLogger("browser_agent.ws")


class ConnectionManager:
    """Broadcasts application events to every connected WebSocket client."""

    def __init__(self, history_size: int = 60) -> None:
        self._connections: set[WebSocket] = set()
        self._history: deque[dict[str, Any]] = deque(maxlen=history_size)
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
        logger.info("WebSocket client connected (%d total)", len(self._connections))
        # Replay a short history so a refreshed/reconnected UI is not empty.
        for payload in list(self._history):
            try:
                await websocket.send_json(payload)
            except Exception:
                break

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)
        logger.info("WebSocket client disconnected (%d remaining)", len(self._connections))

    @property
    def count(self) -> int:
        return len(self._connections)

    async def broadcast(self, event: Event | dict[str, Any]) -> None:
        payload = event.model_dump() if isinstance(event, Event) else dict(event)
        if payload.get("message"):
            payload["message"] = redact(str(payload["message"]))
        async with self._lock:
            self._history.append(payload)
            targets = list(self._connections)
        if not targets:
            return
        dead: list[WebSocket] = []
        for websocket in targets:
            try:
                await websocket.send_json(payload)
            except Exception:
                dead.append(websocket)
        if dead:
            async with self._lock:
                for websocket in dead:
                    self._connections.discard(websocket)

    async def emit(self, event_type: EventType, message: str = "", **data: Any) -> None:
        await self.broadcast(Event(type=event_type, message=message, data=data))


manager = ConnectionManager()


async def emit(event_type: EventType, message: str = "", **data: Any) -> None:
    """Module-level convenience wrapper around the global manager."""
    await manager.emit(event_type, message, **data)
