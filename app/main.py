"""FastAPI application: chat UI, WebSocket event bus, authenticated noVNC proxy.

The process is the single entry point used by Render. All graphical services
(Xvfb/XFCE/Chromium/x11vnc/websockify) are started by ``scripts/start.sh``
before uvicorn, so the browser the agent controls is the *visible* one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from websockets.asyncio.client import connect as ws_connect

from app.agent import TaskBusyError, runner
from app.auth import (
    COOKIE_NAME,
    check_password,
    client_key,
    is_authenticated,
    is_authenticated_ws,
    login_limiter,
    sessions,
)
from app.browser import BrowserUnavailableError, browser_manager
from app.config import BASE_DIR, settings
from app.llm import llm_status
from app.models import (
    BrowserStatus,
    HealthResponse,
    LoginRequest,
    TaskRequest,
    TaskResponse,
    UploadedFile,
)
from app.utils import (
    human_size,
    is_unsafe_task,
    safe_error_message,
    safe_join,
    sanitize_filename,
    setup_logging,
)
from app.websocket import emit, manager

logger = logging.getLogger("browser_agent.main")

STATIC_DIR = BASE_DIR / "static"
PROTECTED_PREFIXES = ("/api", "/novnc")
PUBLIC_API_PATHS = {"/api/login"}


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
async def _warm_up_browser() -> None:
    """Try to connect to the visible Chromium in the background at startup."""
    for _ in range(30):
        try:
            await browser_manager.ensure_connected()
            await emit("browser_connected", "Browser connected.")
            logger.info("Browser connected during startup")
            return
        except (BrowserUnavailableError, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Browser warm-up failed: %s", safe_error_message(exc))
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings.ensure_directories()
    if not settings.auth_configured:
        logger.warning("APP_PASSWORD is not set - the web UI will reject all logins")
    if not settings.llm_configured:
        logger.warning("DEEPSEEK_API_KEY is not set - the agent cannot run tasks")
    logger.info(
        "Starting AI Browser Agent (display=%s, cdp=%s)",
        settings.display,
        browser_manager.cdp_url or settings.cdp_url,
    )

    browser_manager.start_monitor()
    warm_up = asyncio.create_task(_warm_up_browser(), name="browser-warmup")
    try:
        yield
    finally:
        warm_up.cancel()
        with contextlib.suppress(BaseException):
            await warm_up
        with contextlib.suppress(Exception):
            await runner.stop()
        with contextlib.suppress(Exception):
            await browser_manager.shutdown()
        logger.info("AI Browser Agent stopped")


app = FastAPI(title="AI Browser Agent", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Auth middleware (protects /api and /novnc)
# ---------------------------------------------------------------------------
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith(PROTECTED_PREFIXES) and path not in PUBLIC_API_PATHS:
        if not is_authenticated(request):
            return JSONResponse({"detail": "Not authenticated"}, status_code=status.HTTP_401_UNAUTHORIZED)
    return await call_next(request)


def _cookie_secure(request: Request) -> bool:
    if settings.cookie_secure:
        return True
    proto = request.headers.get("x-forwarded-proto", request.url.scheme or "")
    return proto.split(",")[0].strip().lower() == "https"


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def index(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/login", include_in_schema=False)
async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    return FileResponse(STATIC_DIR / "login.html", headers={"Cache-Control": "no-store"})


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return JSONResponse({}, status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Authentication API
# ---------------------------------------------------------------------------
@app.post("/api/login")
async def api_login(payload: LoginRequest, request: Request):
    key = client_key(request)
    locked = login_limiter.locked_for(key)
    if locked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many attempts. Try again in {locked} seconds.",
        )
    if not settings.auth_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="APP_PASSWORD is not configured on the server.",
        )
    if not check_password(payload.password):
        login_limiter.register_failure(key)
        logger.warning("Failed login attempt from %s", key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid password.")

    login_limiter.register_success(key)
    token, max_age = sessions.create()
    response = JSONResponse({"ok": True})
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
    )
    logger.info("Successful login from %s", key)
    return response


@app.post("/api/logout")
async def api_logout(request: Request):
    sessions.destroy(request.cookies.get(COOKIE_NAME))
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


# ---------------------------------------------------------------------------
# Status / health
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict[str, Any]:
    browser = await browser_manager.status()
    llm = llm_status()
    auth = "configured" if settings.auth_configured else "missing"
    ok = browser == "connected" and llm == "configured" and auth == "configured"
    return HealthResponse(
        status="ok" if ok else "degraded",
        browser=browser,
        llm=llm,
        auth=auth,
    ).model_dump()


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    return {
        "browser": await browser_manager.status(),
        "llm": llm_status(),
        "auth": "configured" if settings.auth_configured else "missing",
        "busy": runner.busy,
        "task_id": runner.task_id,
        "novnc": (settings.novnc_path / "vnc_lite.html").exists(),
        "screen": {"width": settings.screen_width, "height": settings.screen_height},
        "max_upload_mb": settings.max_upload_mb,
    }


@app.post("/api/browser/restart")
async def browser_restart() -> dict[str, Any]:
    await emit("log", "Reconnecting browser...")
    try:
        await browser_manager.restart()
        await emit("browser_connected", "Browser reconnected.")
        return {"ok": True, "browser": "connected"}
    except BrowserUnavailableError as exc:
        await emit("browser_disconnected", safe_error_message(exc))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=safe_error_message(exc))


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
@app.post("/api/task", response_model=TaskResponse)
async def api_task(payload: TaskRequest) -> TaskResponse:
    task_text = (payload.task or "").strip()
    if not task_text:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Task is empty.")
    if len(task_text) > settings.max_task_chars:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Task is too long (max {settings.max_task_chars} characters).",
        )
    if is_unsafe_task(task_text):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This request is not supported. The agent cannot be used for abusive activity.",
        )
    if runner.busy:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent is currently busy.")

    file_paths: list[str] = []
    for stored_name in payload.files[:20]:
        try:
            candidate = safe_join(settings.uploads_path, stored_name)
        except ValueError:
            continue
        if candidate.is_file():
            file_paths.append(str(candidate))

    try:
        task_id = await runner.start(task_text, file_paths)
    except TaskBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return TaskResponse(ok=True, message="Task started.", task_id=task_id)


@app.post("/api/task/stop")
async def api_task_stop() -> dict[str, Any]:
    stopped = await runner.stop()
    if not stopped:
        return {"ok": False, "message": "No task is running."}
    return {"ok": True, "message": "Stop requested."}


# ---------------------------------------------------------------------------
# Files (upload / download)
# ---------------------------------------------------------------------------
@app.post("/api/upload", response_model=UploadedFile)
async def api_upload(file: UploadFile = File(...)) -> UploadedFile:
    original = sanitize_filename(file.filename or "upload")
    stored = f"{uuid.uuid4().hex[:8]}-{original}"
    destination = safe_join(settings.uploads_path, stored)
    max_bytes = settings.max_upload_mb * 1024 * 1024
    size = 0
    try:
        with destination.open("wb") as handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"File is too large (max {settings.max_upload_mb} MB).",
                    )
                handle.write(chunk)
    except HTTPException:
        with contextlib.suppress(OSError):
            destination.unlink(missing_ok=True)
        raise
    except OSError as exc:
        with contextlib.suppress(OSError):
            destination.unlink(missing_ok=True)
        logger.error("Upload failed: %s", safe_error_message(exc))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not store the file.")
    finally:
        with contextlib.suppress(Exception):
            await file.close()

    logger.info("Uploaded file stored as %s (%d bytes)", stored, size)
    return UploadedFile(id=stored, name=original, size=size)


@app.get("/api/files")
async def api_files() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for base, source in ((settings.downloads_path, "download"), (settings.uploads_path, "upload")):
        if not base.exists():
            continue
        try:
            paths = sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            continue
        for path in paths:
            try:
                if not path.is_file():
                    continue
                info = path.stat()
            except OSError:
                continue
            entries.append(
                {
                    "name": path.name,
                    "size": info.st_size,
                    "size_human": human_size(info.st_size),
                    "url": f"/api/download/{quote(path.name)}",
                    "source": source,
                }
            )
    return entries


@app.get("/api/download/{name}")
async def api_download(name: str):
    for base in (settings.downloads_path, settings.uploads_path):
        try:
            path = safe_join(base, name)
        except ValueError:
            continue
        if path.is_file():
            return FileResponse(path, filename=path.name)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found.")


# ---------------------------------------------------------------------------
# WebSocket event stream
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_events(websocket: WebSocket) -> None:
    if not is_authenticated_ws(websocket):
        await websocket.close(code=4401)
        return
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await manager.disconnect(websocket)


# ---------------------------------------------------------------------------
# noVNC WebSocket proxy (authenticated, keeps VNC internal)
# ---------------------------------------------------------------------------
@app.websocket("/websockify")
async def websockify_proxy(websocket: WebSocket) -> None:
    if not is_authenticated_ws(websocket):
        await websocket.close(code=4401)
        return

    requested = [
        part.strip()
        for part in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if part.strip()
    ]
    chosen = "binary" if "binary" in requested else (requested[0] if requested else None)
    await websocket.accept(subprotocol=chosen)

    target = f"ws://127.0.0.1:{settings.novnc_port}/"
    try:
        async with ws_connect(
            target,
            subprotocols=[chosen] if chosen else None,
            max_size=None,
            open_timeout=10,
            close_timeout=3,
        ) as backend:

            async def client_to_backend() -> None:
                while True:
                    message = await websocket.receive()
                    if message.get("type") == "websocket.disconnect":
                        return
                    if message.get("bytes") is not None:
                        await backend.send(message["bytes"])
                    elif message.get("text") is not None:
                        await backend.send(message["text"])

            async def backend_to_client() -> None:
                async for message in backend:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            tasks = {
                asyncio.create_task(client_to_backend()),
                asyncio.create_task(backend_to_client()),
            }
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as exc:
        logger.debug("noVNC proxy connection ended: %s", safe_error_message(exc))
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()


# ---------------------------------------------------------------------------
# Static assets (public: the login page needs CSS/JS)
# ---------------------------------------------------------------------------
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# noVNC assets live inside the Docker image. The mount is covered by the auth
# middleware above; websockify itself is only reachable through /websockify.
if settings.novnc_path.exists():
    app.mount("/novnc", StaticFiles(directory=settings.novnc_path, html=True), name="novnc")


@app.get("/api/browser", response_model=BrowserStatus)
async def api_browser() -> BrowserStatus:
    return BrowserStatus(status=await browser_manager.status(), cdp_url=browser_manager.cdp_url)
