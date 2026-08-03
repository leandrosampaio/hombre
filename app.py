import os
import re
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from routes.settings import router as settings_router
from routes.settings import sync_router as sync_router
from routes.deletes import router as deletes_router
from routes.export import router as export_router
from routes.export import workspace_router as workspace_router
from routes.export import trash_router as trash_router
from routes.security import (
    RateLimitMiddleware,
    RequestLoggingMiddleware,
    RoleBasedAuthMiddleware,
    EnhancedSecurityHeadersMiddleware,
    SupabaseAuthMiddleware,
)
from routes.supabase import is_configured as supabase_configured

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hombre")

HONCHO_URL = os.environ.get("HONCHO_URL", "http://localhost:8000")
HONCHO_API_KEY = os.environ.get("HONCHO_API_KEY", "")
ALLOWED_REQUEST_HEADERS = {"content-type", "accept", "accept-encoding", "user-agent"}
ALLOWED_RESPONSE_HEADERS = {"content-type", "content-length", "location"}
VALID_ID = re.compile(r"^[a-zA-Z0-9_-]+$")

static_dir = Path(__file__).parent / "static"
_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    from routes.security import _parse_users
    users = _parse_users()
    if not users:
        log.warning("No auth configured — open access mode")
    else:
        log.info("Auth enabled: %d user(s) configured", len(users))

    if supabase_configured():
        log.info("Supabase integration enabled — using Supabase for storage and auth")
    else:
        log.info("Supabase not configured — using file-based storage")

    default_headers = {}
    if HONCHO_API_KEY:
        default_headers["Authorization"] = f"Bearer {HONCHO_API_KEY}"
    _client = httpx.AsyncClient(
        base_url=HONCHO_URL,
        timeout=httpx.Timeout(30.0, connect=5.0),
        headers=default_headers,
    )
    yield
    await _client.aclose()
    _client = None


app = FastAPI(
    title="Hombre",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# Security middleware stack — order matters.
# Outermost first: headers → logging → rate limiting → supabase auth → role-based auth
app.add_middleware(EnhancedSecurityHeadersMiddleware)
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(SupabaseAuthMiddleware)
app.add_middleware(RoleBasedAuthMiddleware)


@app.get("/api/health")
async def health():
    """Lightweight health check — must NOT depend on Honcho.

    The Docker health check hits this endpoint with a short timeout (5 s).
    Calling Honcho here would make the container appear unhealthy any time
    the upstream is slow, even though Hombre itself is fine.
    """
    if _client is None:
        return JSONResponse(
            {"status": "error", "reason": "client_not_ready"},
            status_code=503,
        )
    return {"status": "ok"}


app.include_router(settings_router)
app.include_router(sync_router)
app.include_router(deletes_router)
app.include_router(export_router)
app.include_router(trash_router)


@app.get("/api/auth/status")
async def auth_status(request: Request):
    """Check if Supabase auth is configured and return current user.

    This is a Hombre-local endpoint (not proxied to Honcho).
    Must be accessible without authentication so the frontend can
    determine auth configuration during initialization.
    """
    from routes.security import _users_cache
    configured = supabase_configured() or bool(_users_cache)
    user = getattr(request.state, "user", None) or None
    return {"configured": configured, "user": user}


@app.post("/api/workspaces/{wid}/peers/{pid}/chat")
async def chat_stream(wid: str, pid: str, request: Request):
    if not VALID_ID.match(wid) or not VALID_ID.match(pid):
        return JSONResponse({"error": "invalid_id"}, status_code=400)

    try:
        body = await request.json()

        async def event_gen():
            async with _client.stream(
                "POST",
                f"/v3/workspaces/{wid}/peers/{pid}/chat",
                json=body,
                timeout=httpx.Timeout(None, connect=5.0, read=120.0),
            ) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk

        return StreamingResponse(
            event_gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    except httpx.ConnectError:
        return JSONResponse({"error": "upstream_unreachable"}, status_code=502)
    except Exception as e:
        log.error("Chat stream error: %s", e)
        return JSONResponse({"error": "proxy_error"}, status_code=502)


async def _honcho_post(path: str, body: dict | None = None) -> dict | list | None:
    """Helper to POST to Honcho and return parsed JSON."""
    try:
        resp = await _client.post(f"/v3/{path}", json=body)
        if resp.status_code >= 400:
            log.warning("Honcho error %d on POST %s", resp.status_code, path)
            return None
        return resp.json()
    except Exception as e:
        log.error("Honcho request failed POST %s: %s", path, e)
        return None


@app.post("/api/workspaces/{wid}/conclusions/list/all")
async def list_all_conclusions(wid: str):
    """Fetch ALL conclusions for a workspace by paginating through them.

    Honcho uses fastapi_pagination which reads page/size from QUERY
    parameters, not the POST body.
    """
    if not VALID_ID.match(wid):
        return JSONResponse({"error": "invalid_id"}, status_code=400)

    all_conclusions = []
    page = 1
    size = 100
    max_pages = 50  # safety limit: 50 pages * 100 = 5000 conclusions max

    for _ in range(max_pages):
        try:
            resp = await _client.post(
                f"/v3/workspaces/{wid}/conclusions/list",
                json={},
                params={"page": page, "size": size},
            )
            if resp.status_code >= 400:
                log.warning("Honcho error %d on conclusions list page %d", resp.status_code, page)
                break
            result = resp.json()
        except Exception as e:
            log.error("Conclusions list failed page %d: %s", page, e)
            break

        if isinstance(result, list):
            all_conclusions.extend(result)
            if len(result) < size:
                break
        elif isinstance(result, dict):
            items = result.get("items", result.get("conclusions", result.get("results", [])))
            all_conclusions.extend(items)
            total_pages = result.get("pages", 1)
            if page >= total_pages or len(items) < size:
                break
        else:
            break

        page += 1

    return {"conclusions": all_conclusions, "count": len(all_conclusions)}


@app.post("/api/workspaces/{wid}/sessions/{sid}/messages/list/all")
async def list_all_messages(wid: str, sid: str):
    """Fetch ALL messages for a session by paginating through them.

    Honcho uses fastapi_pagination which reads page/size from QUERY
    parameters, not the POST body.
    """
    if not VALID_ID.match(wid) or not VALID_ID.match(sid):
        return JSONResponse({"error": "invalid_id"}, status_code=400)

    all_messages = []
    page = 1
    size = 100
    max_pages = 50

    for _ in range(max_pages):
        try:
            resp = await _client.post(
                f"/v3/workspaces/{wid}/sessions/{sid}/messages/list",
                json={},
                params={"page": page, "size": size},
            )
            if resp.status_code >= 400:
                log.warning("Honcho error %d on messages list page %d", resp.status_code, page)
                break
            result = resp.json()
        except Exception as e:
            log.error("Messages list failed page %d: %s", page, e)
            break

        if isinstance(result, list):
            all_messages.extend(result)
            if len(result) < size:
                break
        elif isinstance(result, dict):
            items = result.get("items", result.get("messages", result.get("results", [])))
            all_messages.extend(items)
            total_pages = result.get("pages", 1)
            if page >= total_pages or len(items) < size:
                break
        else:
            break

        page += 1

    return {"messages": all_messages, "count": len(all_messages)}


@app.post("/api/workspaces/{wid}/sessions/list/all")
async def list_all_sessions(wid: str):
    """Fetch ALL sessions for a workspace by paginating through them.

    Honcho uses fastapi_pagination which reads page/size from QUERY
    parameters, not the POST body. The body carries filter options.
    """
    if not VALID_ID.match(wid):
        return JSONResponse({"error": "invalid_id"}, status_code=400)

    all_sessions = []
    page = 1
    size = 100
    max_pages = 50  # safety limit: 50 pages * 100 = 5000 sessions max

    for _ in range(max_pages):
        try:
            resp = await _client.post(
                f"/v3/workspaces/{wid}/sessions/list",
                json={},
                params={"page": page, "size": size},
            )
            if resp.status_code >= 400:
                log.warning("Honcho error %d on sessions list page %d", resp.status_code, page)
                break
            result = resp.json()
        except Exception as e:
            log.error("Sessions list failed page %d: %s", page, e)
            break

        if isinstance(result, list):
            all_sessions.extend(result)
            if len(result) < size:
                break
        elif isinstance(result, dict):
            items = result.get("items", result.get("sessions", result.get("results", [])))
            all_sessions.extend(items)
            total_pages = result.get("pages", 1)
            if page >= total_pages or len(items) < size:
                break
        else:
            break

        page += 1

    return {"sessions": all_sessions, "count": len(all_sessions)}


# ---------------------------------------------------------------------------
# Honcho container log streaming & status endpoints
# ---------------------------------------------------------------------------

_HONCHO_CONTAINERS = {"deriver": "honcho-deriver-1", "api": "honcho-api-1"}


@app.get("/api/honcho/containers")
async def honcho_containers():
    """Return available Honcho Docker containers with their status."""
    result = []
    for short, full in _HONCHO_CONTAINERS.items():
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "inspect", "--format", "{{.State.Status}}", full,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await proc.communicate()
            status = stdout.decode().strip() if proc.returncode == 0 else "unknown"
        except Exception:
            status = "unreachable"
        result.append({"name": short, "full_name": full, "status": status})
    return {"containers": result}


@app.get("/api/honcho/logs/{container}")
async def honcho_logs(container: str, tail: int = Query(default=200, ge=1, le=5000)):
    """Stream live Docker container logs via SSE.

    Only ``deriver`` and ``api`` are accepted — anything else is rejected
    before it can ever touch a subprocess call.
    """
    if container not in _HONCHO_CONTAINERS:
        return JSONResponse(
            {"error": "invalid_container", "allowed": list(_HONCHO_CONTAINERS)},
            status_code=400,
        )

    full_name = _HONCHO_CONTAINERS[container]

    async def event_stream():
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "logs", "--follow", "--tail", str(tail),
                "--timestamps", full_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=30.0)
                except asyncio.TimeoutError:
                    # No output for 30 s — send SSE keepalive comment
                    yield ": keepalive\n\n"
                    continue

                if not line:
                    # Process exited or stream ended
                    break

                text = line.decode(errors="replace")
                yield f"data: {text}\n\n"

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("Honcho log stream error (%s): %s", container, e)
            yield f"data: [error] {e}\n\n"
        finally:
            if proc and proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except (ProcessLookupError, asyncio.TimeoutError):
                    proc.kill()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Keep this generic router after the dedicated workspace endpoints above.
# Its catch-all POST route would otherwise intercept the chat SSE endpoint.
app.include_router(workspace_router)


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(path: str, request: Request):
    decoded_path = unquote(path)
    prev = None
    while prev != decoded_path:
        prev = decoded_path
        decoded_path = unquote(decoded_path)
    if ".." in decoded_path or "\x00" in decoded_path or decoded_path.startswith("/"):
        return JSONResponse({"error": "invalid_path"}, status_code=400)

    try:
        body = await request.body()
        headers = {k: v for k, v in request.headers.items() if k.lower() in ALLOWED_REQUEST_HEADERS}

        req = _client.build_request(
            method=request.method,
            url=f"/v3/{decoded_path}",
            headers=headers,
            content=body or None,
        )
        resp = await _client.send(req)
        status = resp.status_code
        resp_headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() in ALLOWED_RESPONSE_HEADERS
        }

        if status >= 500:
            log.warning("Upstream error %d on %s %s", status, request.method, decoded_path)
            await resp.aclose()
            return JSONResponse({"error": "upstream_error"}, status_code=status)

        async def stream_gen():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()

        return StreamingResponse(stream_gen(), status_code=status, headers=resp_headers)
    except httpx.ConnectError:
        return JSONResponse({"error": "upstream_unreachable"}, status_code=502)
    except Exception as e:
        log.error("Proxy error: %s", e)
        return JSONResponse({"error": "proxy_error"}, status_code=502)


@app.get("/")
async def index():
    # CRITICAL FIX: Path.read_text() is synchronous I/O that blocks the
    # event loop. Move to a thread pool.
    html = await asyncio.to_thread((static_dir / "index.html").read_text)
    return HTMLResponse(html)


app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
