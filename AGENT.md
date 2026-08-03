# Agent Configuration

## Project

Hombre — web-based GUI for a self-hosted Honcho AI memory server.

- **Backend**: Python FastAPI, runs on port 5000
- **Frontend**: Vanilla HTML/CSS/JS (no build tools, no frameworks)
- **Honcho server**: Expected at `localhost:8000`

## Commands

```bash
# Run dashboard locally
python3 -m uvicorn app:app --host 0.0.0.0 --port 5000 --reload

# Run in Docker
docker compose up -d

# Check syntax
python3 -m py_compile app.py
python3 -m py_compile routes/settings.py
python3 -m py_compile routes/security.py
python3 -m py_compile routes/deletes.py
python3 -m py_compile routes/export.py
python3 -m py_compile routes/supabase.py
node --check static/app.js
```

## File Locations

- `app.py` — FastAPI backend (auth, proxy, routes, pagination); mounts sync_router and trash_router; event loop safe (to_thread for static file reads)
- `routes/__init__.py` — Package marker
- `routes/supabase.py` — Supabase client initialization (optional integration); lazy-init with anon and admin clients
- `routes/security.py` — Security middleware (rate limiting, RBAC auth, Supabase JWT auth, request logging, shared user cache)
- `routes/settings.py` — Settings API (read/write `.env`, restart containers, audit logging, user management); also contains `sync_router` (`/api/sync/*`) for manual sync trigger and queue status
- `routes/deletes.py` — Soft-delete registry (Supabase or JSON file storage)
- `routes/export.py` — Export/Import API, workspace merge, trash endpoints (`trash_router`), hard-delete for conclusions/messages
- `schema/supabase.sql` — SQL schema for Supabase tables (soft_deletes, notifications, audit_logs)
- `data/deleted.json` — Soft-deleted resource IDs (auto-created, used when Supabase not configured)
- `data/notifications.json` — Recent notifications (auto-created, used when Supabase not configured)
- `data/trash/conclusions.json` — Trashed conclusions (auto-created)
- `static/app.js` — All frontend logic (7 tab modules, Modal, App, notifications, sync indicator, 429 handling, credentials UI, trash UI)
- `static/style.css` — Dark theme CSS (colorblind-friendly sync indicator, sidebar flex layout; sections expand to fill space, no internal scrollbars)
- `static/index.html` — SPA shell with sidebar nav (Logs renamed to Honcho Logs), sync indicator pinned to bottom, and auth section
- `Dockerfile` — Python 3.12-slim, EXPOSE 5000 (built from dev repo, pushed to ghcr.io/lovethatbrandx/hombre/hombre:latest)
- `docker-compose.yml` — Port 5000:5000, healthcheck (also exists in `~/docker/hombre/` deployment folder)
- `docs/API.md` — Complete API reference (all endpoints, request/response formats)
- `docs/FEATURES.md` — Feature documentation (workspace, peers, sessions, chat, conclusions, export/import)
- `docs/DEPLOYMENT.md` — Deployment guide (Docker, local dev, env vars, troubleshooting)

## Conventions

- All API calls go through `/api/{path}` proxy to Honcho `/v3/{path}`
- `App.api()` is the centralized fetch helper (no body on GET/HEAD/DELETE, error parsing, `cache: 'no-store'` on all GET requests to prevent browser caching)
- XSS prevention: always use `App.escapeHtml()` / `App.escapeAttr()` in templates
- Event delegation pattern for click handlers (no inline onclick)
- Modal utility: `Modal.show()`, `Modal.confirm()`, `Modal.close()`
- Toast notifications: `App.toast(message, type)` for user feedback
- Tabs: `OverviewTab`, `PeersTab`, `SessionsTab`, `ChatTab`, `ConclusionsTab`, `MessagesTab`, `SettingsTab`
- Each tab fetches its own data directly from the API (no shared state dependency)
- OverviewTab fetches peers/sessions/conclusions independently on render
- All synchronous I/O (filesystem, supabase-py, httpx) wrapped in `asyncio.to_thread()` to prevent event loop blocking
- Fire-and-forget pattern (`_audit_fire_and_forget`) for non-critical background tasks like audit logging on frequently-polled endpoints
- Sidebar sync indicator polls `/api/sync/status/{wid}` every 30s with adaptive backoff on failures (up to 120s)
- Colorblind-friendly UI: blue dot = synced, amber/orange dot = syncing, gray = offline; icons (checkmark, spinner, X) accompany every color state

## Honcho API Notes

- Peer card endpoint: `GET /v3/workspaces/{wid}/peers/{pid}/card` (GET only, not POST)
- Chat endpoint: `POST /v3/workspaces/{wid}/peers/{pid}/chat` — queries a peer's representation using natural language; supports `reasoning_level` (minimal/low/medium/high/max) and `stream: true` for SSE
- Summaries endpoint: `GET /v3/workspaces/{wid}/sessions/{sid}/summaries` (GET only)
- Schedule dream: `POST /v3/workspaces/{wid}/schedule_dream` — triggers a sync/dream cycle; requires `observer` (peer ID) and `dream_type` in body
- Queue status: `GET /v3/workspaces/{wid}/queue/status` — returns work unit progress (pending/done/total)
- Workspace delete: `DELETE /v3/workspaces/{wid}` (requires deleting all sessions first)
- Session delete: `DELETE /v3/workspaces/{wid}/sessions/{sid}`
- Conclusion delete: `DELETE /v3/workspaces/{wid}/conclusions/{cid}` (moves to trash locally first)
- Message delete: `DELETE /v3/workspaces/{wid}/sessions/{sid}/messages/{mid}`
- No peer delete endpoint exists in Honcho API

## Honcho API Limitations — DELETE Operations

| Resource     | DELETE Supported? | Workaround              |
|-------------|-------------------|-------------------------|
| Workspace   | Yes               | Must delete sessions first |
| Session     | Yes               | None needed             |
| Peer        | No                | Soft-delete locally (`routes/deletes.py`) |
| Message     | Yes               | Hard-delete via proxy; also soft-delete locally |
| Conclusion  | Yes               | Hard-delete via proxy; also trash locally (`routes/export.py`) |

## New API Endpoints (Hombre-specific, not proxied to Honcho)

### Sync (`routes/settings.py` — sync_router)
- `POST /api/sync/trigger` — Trigger manual sync (body: `{workspace_id, observer?, dream_type?}`)
- `GET /api/sync/status/{wid}` — Get queue status for a workspace

### Soft Delete (`routes/deletes.py`)
- `POST /api/soft-delete` — Mark resource as deleted (body: `{type, id, workspace_id}`)
- `POST /api/soft-delete/check` — Check if resources are deleted (body: `{type, ids, workspace_id}`)
- `GET /api/soft-delete/list` — List deleted resources (optional `?type=` filter)
- `POST /api/soft-delete/restore` — Restore a deleted resource

### Pagination Helpers (`app.py`)
- `POST /api/workspaces/{wid}/conclusions/list/all` — Fetch ALL conclusions (paginated, up to 5000)
- `POST /api/workspaces/{wid}/sessions/{sid}/messages/list/all` — Fetch ALL messages (paginated, up to 5000)

### Notifications (`routes/notifications.py`)
- `GET /api/notifications` — Get active notifications (optional `?type=`, `?workspace_id=`)
- `POST /api/notifications/dismiss` — Dismiss a notification (body: `{id}`)

### Export/Import (`routes/export.py`)
- `POST /api/export/workspace/{wid}` — Export entire workspace (peers, sessions, conclusions, messages)
- `POST /api/export/peer/{wid}/{pid}` — Export single peer's data (info, representation, card, conclusions)
- `POST /api/export/conclusions/{wid}` — Export all conclusions for workspace
- `POST /api/export/import/workspace` — Upload JSON export file for preview and conflict detection (multipart form)
- `POST /api/export/import/confirm` — Confirm import with conflict resolution (body: `{workspace_id, data, id_mapping, conflict_strategy}`)

### Trash (`routes/export.py` — trash_router)
- `GET /api/trash/conclusions` — List trashed conclusions
- `POST /api/trash/conclusions/{cid}/restore` — Restore a trashed conclusion back to Honcho
- `DELETE /api/trash/conclusions/{cid}` — Permanently delete a conclusion from trash

### Hard Delete (`routes/export.py` — workspace_router)
- `DELETE /api/workspaces/{wid}/conclusions/{cid}` — Move conclusion to trash and delete from Honcho
- `DELETE /api/workspaces/{wid}/sessions/{sid}/messages/{mid}` — Delete message from Honcho permanently

### Workspace Merge (`routes/export.py`)
- `POST /api/workspaces/merge/preview` — Preview merge conflicts (body: `{source_workspace_id, target_workspace_id, conflict_strategy}`)
- `POST /api/workspaces/merge` — Execute merge with conflict resolution (body: `{source_workspace_id, target_workspace_id, conflict_strategy}`)

### Supabase Auth (`routes/security.py` — when Supabase configured)
- `GET /api/auth/status` — Check if Supabase is configured and get current user
- `POST /api/auth/login` — Login with email/password (body: `{email, password}`)
- `POST /api/auth/magic-link` — Send magic link (body: `{email}`)
- `POST /api/auth/logout` — Logout current user

### User Management (`routes/settings.py`)
- `GET /api/settings/users` — List dashboard users (cached in memory)
- `POST /api/settings/users` — Update dashboard users (body: `{users: [{username, password, role}, ...]}`)

### Supabase Config (`routes/settings.py`)
- `GET /api/settings/supabase` — Read Supabase config from Hombre's own `.env`
- `POST /api/settings/supabase` — Write Supabase config to Hombre's `.env` (body: `{SUPABASE_URL, SUPABASE_KEY, SUPABASE_SERVICE_KEY}`)

## Environment

- `HONCHO_URL` — Honcho server URL (default: `http://localhost:8000`)
- `HONCHO_API_KEY` — API key for Honcho auth
- `HONCHO_ENV_PATH` — Path to `.env` file (optional, settings tab returns 403 if unset)
- `HONCHO_COMPOSE_DIR` — Docker Compose dir (optional, settings tab returns 403 if unset)
- `DASHBOARD_USER` / `DASHBOARD_PASSWORD` — Single-user basic auth (backward compat)
- `DASHBOARD_ROLE` — Role for single user (default: `admin`)
- `DASHBOARD_USERS` — Multi-user config: `user1:pass1:admin,user2:pass2:viewer`
- `HOMBRE_LOG_DIR` — Log directory (default: `logs`)
- `SUPABASE_URL` — Supabase project URL (optional, enables Supabase integration)
- `SUPABASE_KEY` — Supabase anon/public key (required with SUPABASE_URL)
- `SUPABASE_SERVICE_KEY` — Supabase service role key (optional, for admin operations)
- `HOMBRE_ENV_PATH` — Path to Hombre's own `.env` file (default: `.env` in project root)
- `HOMBRE_DATA_DIR` — Data directory for trash and other runtime data (default: `data`)

## Security Notes

### Authentication & RBAC

- **Roles**: `admin` (full), `editor` (create/edit/read), `viewer` (read-only)
- Single-user mode: `DASHBOARD_USER` + `DASHBOARD_PASSWORD` + `DASHBOARD_ROLE`
- Multi-user mode: `DASHBOARD_USERS=user1:pass1:admin,user2:pass2:viewer`
- No auth configured → open access (with startup warning)
- Basic Auth uses `hmac.compare_digest` for timing-safe comparison
- Role checked per-request: settings/write endpoints require `settings` permission, DELETE requires `delete` permission

### Rate Limiting (in-memory, resets on restart)

| Endpoint | Limit |
|---|---|
| `/api/settings/*` | 30 req/min |
| `/api/workspaces/*` | 120 req/min |
| `/api/peers/*` | 30 req/min |
| `/api/sessions/*` | 30 req/min |
| `/api/messages/*` | 30 req/min |
| `/api/chat` | 5 req/min |
| Everything else | 60 req/min |

- Returns `429 Too Many Requests` with `Retry-After` header
- Rate limit key: client IP + auth token prefix
- Frontend gracefully handles 429 responses (preserves existing data, shows toast notification)
- The `/` index route does not require authentication (no Basic auth prompt on page load)

### Request Logging

- All API requests logged to `logs/access.log`
- Format: `ISO_TIMESTAMP METHOD STATUS DURATION path user=NAME detail`
- Rotates at 5 MB, keeps last 5 rotated files
- Static asset requests are not logged
- AccessLogger.log() is wrapped in `asyncio.to_thread()` to prevent event loop blocking

### Audit Logging

- Settings operations logged to `logs/audit.log`
- Tracks: `settings.read`, `settings.write`, `settings.backup`, `settings.restore`, `settings.restart`, `settings.users.read`, `settings.users.write`, `settings.supabase.read`, `settings.supabase.write`, `sync.trigger`, `sync.status`
- Includes username and changed keys
- Supabase audit writes use fire-and-forget pattern (`_audit_fire_and_forget`) — non-blocking for frequently-polled endpoints like sync status

### Security Headers

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `X-XSS-Protection: 1; mode=block`
- `Strict-Transport-Security` (HTTPS only)
- `Content-Security-Policy`: `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' fonts.googleapis.com; font-src self fonts.googleapis.com fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: camera=(), microphone=(), geolocation=()`

### Path Traversal Protection

- Iterative URL-decoding + `..`/`\x00`/leading-`/` checks

### Settings Write Protection

- `WRITABLE_KEYS` allowlist (only known env keys)
- Newline injection prevention (`sanitize_value`)
- Backup created on every write
- Audit log records who changed what

### Frontend

- `App.escapeHtml()` escapes `'` to `&#39;` (prevents attribute injection)
- All GET requests use `cache: 'no-store'` to prevent browser caching stale data
- Sidebar sync indicator polls with adaptive backoff (30s → 60s → 120s on failures, resets on success)
