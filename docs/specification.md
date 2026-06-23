# OpenWebUI Notes Backup to GitHub — Specification

## 1. Goals

- **Cloud backup:** OpenWebUI Notes are backed up to a private GitHub repository so they are never lost if the OpenWebUI instance fails.
- **Version history:** Each note is stored as a separate Markdown file in Git, giving full per-note version history viewable on GitHub (History → diff → restore).
- **Zero server changes:** The solution is a single self-contained OpenWebUI Function. No cron jobs, no git CLI, no SSH keys, no Linux server configuration.
- **Per-user, self-service:** Each user configures their own GitHub repository and schedule. The admin installs the Function once and does nothing else.
- **Publishable:** The Function is designed to be shared with the OpenWebUI community. Any instance can install it and users opt in individually.

## 2. Problem Context

OpenWebUI stores Notes as rows in the SQLite database (`webui.db`), not as individual files. There is no built-in bulk export, auto-sync, or Git integration. The built-in manual per-note export supports multiple formats (.md, .txt, .pdf) but is one-at-a-time. This solution exports only to **Markdown (.md)** for clean Git diffs and easy copy-paste restore.

## 3. Key Decisions

| Decision | Choice | Rationale |
|---|---|---|
| **Trigger mechanism** | Global Filter Function (`is_global=True`, `is_active=True`) | Runs on every chat completion request for every model, for every user. Most reliable periodic trigger without server-side scheduler. |
| **Git interaction** | GitHub REST Contents API (`PUT/GET/DELETE …/contents/{path}`) | Eliminates dependency on `git` CLI, SSH keys, or local clones. Works inside any OpenWebUI container. `httpx` is already bundled. |
| **State storage** | Separate SQLite file in `DATA_DIR` (`note_exporter_state.db`) | Local, durable, restart-safe. Two tables: `backup_state(user_id, last_run)` and `note_hashes(user_id, short_id, sha256)`. Replaces the previous in-repo JSON state file. |
| **Type safety** | Pydantic models for all data | `User`, `Note`, `BackupState`, `NoteHash`, `GitHubContent`. Validation at boundaries, no dict sprawl, IDE-friendly. |
| **Notes access** | Direct SQLite read (`sqlite3`, `mode=ro`) | Follows the same pattern as the built-in memory function. No HTTP API calls, no API keys needed. Reads only the current user's notes. |
| **Secrets storage** | `UserValves` (per-user) | Each user enters their own GitHub PAT. No shared secrets. Admin sees nothing. |
| **Scheduling** | Per-user timezone time slots (comma-separated) | More intuitive than "every N hours." Example: `09:00,12:00,15:00`. User defines when they want backups. |
| **Admin involvement** | Install once, zero ongoing admin work | No admin Valves. No API key enablement. No permission changes. |
| **Opt-in model** | `ENABLED` toggle in UserValves | User must explicitly set `ENABLED=true` plus fill required fields. Missing/empty required fields = silent skip. |
| **Commit author** | Omitted (GitHub defaults to PAT owner) | Simplifies design. No `COMMITTER_NAME`/`EMAIL` valves needed. |
| **File naming** | `<slug>-<note_id>.md` (repo root) | No namespace folder. ID in filename makes renames stable and trackable. |
| **State consistency** | Self-healing via LIST cross-check | Skip condition requires both a matching local hash AND presence in the GitHub listing. If files are deleted externally, the next backup re-uploads. |
| **Defensive input handling** | Control-char scrub before HTTP/filename | `_clean_url()` strips `[\x00-\x1f\x7f]` from URLs; `_make_filename()` scrubs the final path. Catches trailing newlines in valve values or note titles. |
| **Partitioning** | Per-user (only current user's notes) | Privacy-first. A user's backup never contains another user's notes. |

## 4. Architecture

```
user sends a message (any model, any chat)
   │
   ▼
Global Filter.inlet(body, __user__)
   │  1. Validate __user__ into a Pydantic User model (User.model_validate)
   │  2. Read user.valves (UserValves instance)
   │  3. Skip if ENABLED != true or any required field empty
   │  4. Parse SCHEDULE into sorted time slots
   │  5. Find "next scheduled slot strictly after last_run"
   │  6. If now < next_slot → return body (skip)
   │  7. If _busy[user_id] → return body (skip)
   │  8. asyncio.create_task(_run_backup(user_id, user_valves, tz)) → fire-and-forget
   │  9. return body immediately (chat never waits)
   ▼
_run_backup (background asyncio task)
   │  a. Load hashes from note_exporter_state.db → dict[str, NoteHash]
   │  b. Open webui.db read-only via sqlite3
   │  c. SELECT note rows WHERE user_id = ? → list[Note]
   │  d. Compute sha256 of each note content
   │  e. List repo root in GitHub to detect renames/removals → dict[str, GitHubContent]
   │  f. For each note, skip if (hash matches stored hash) AND (file present in listing)
   │  g. For each changed/new note:
   │       - If repo file exists at the expected path → PUT (update) using its sha
   │       - If repo file exists at a different path → DELETE old, PUT new (rename)
   │       - If no repo file → PUT new
   │       - Commit message: "notes: update <title> (<shortid>)" / "notes: add <title>"
   │  h. If DELETE_REMOVED_NOTES:
   │       - DELETE repo files whose short_id is no longer in the current notes
   │  i. UPSERT state into note_exporter_state.db (last_run + per-note hashes)
   │  j. On any failure → logger.exception, leave last_run unchanged (retries next slot)
```

## 5. Valves & UserValves

### Admin Valves
**None.** The admin installs the Function, sets `is_global=True` and `is_active=True`, and is done.

### UserValves

| Field | Type | Default | Required | Description |
|---|---|---|---|---|
| `ENABLED` | bool | `false` | Yes | Master opt-in toggle. Must be `true` for backup to run. |
| `GITHUB_TOKEN` | str | `""` | Yes | Fine-grained Personal Access Token scoped to the user's private repo, Contents: Read & Write. |
| `GITHUB_REPO_OWNER` | str | `""` | Yes | GitHub repository owner (username or org). Example: `octocat`. |
| `GITHUB_REPO_NAME` | str | `""` | Yes | GitHub repository name. Example: `my-notes-backup`. |
| `TIMEZONE` | str | `"UTC"` | No | IANA timezone name (selected from dropdown). SCHEDULE times are interpreted in this timezone. Example: `America/New_York`. |
| `SCHEDULE` | str | `""` | Yes | Comma-separated times in your timezone. Example: `09:00,12:00,15:00`. |
| `DELETE_REMOVED_NOTES` | bool | `true` | No | If `true`, deleting a note in OpenWebUI also deletes its file from the GitHub repo. |

**Skip rule:** If `ENABLED != true` **OR** any of `GITHUB_TOKEN`, `GITHUB_REPO_OWNER`, `GITHUB_REPO_NAME`, `SCHEDULE` is empty **OR** `TIMEZONE` is not a valid IANA name → the function silently skips that user. No error, no chat delay.

## 6. Scheduling Algorithm

```python
# 1. Parse SCHEDULE
slots = sorted([parse_time(s.strip()) for s in schedule.split(",")])

# 2. Find the earliest scheduled slot that is STRICTLY AFTER last_run
next_slot = None
for slot in slots:
    candidate = datetime.combine(last_run.date(), slot)
    if candidate <= last_run:
        # Same day, but slot is before or exactly at last_run → try next day
        candidate += timedelta(days=1)
    next_slot = candidate
    break

# 3. If all slots are before last_run today → next_slot = first slot tomorrow
if next_slot is None:
    next_slot = datetime.combine(last_run.date() + timedelta(days=1), slots[0])

# 4. Decision
if now >= next_slot:
    run_backup()
```

**Why this handles delays:**
- `last_run` is updated **only on successful backup completion**.
- We compare against the **slot time**, not exact clock matching.
- A backup that finishes at 09:01 sets `last_run = 09:01`. The next slot after that is 12:00. At 09:50, `now < next_slot` → skip correctly.

## 7. GitHub Repo Layout

```
<repo-root>
├── my-first-note-a1b2c3.md
├── meeting-notes-d4e5f6.md
└── ...
```

- All note files live at the **repo root**. No namespace folder.
- Filename pattern: `<slug>-<note_id_first_8_chars>.md`. The `slug` is the title lowercased, ASCII-normalized, with non-word chars stripped. The 8-char ID prefix makes renames stable and lets the diff logic match files even after title changes.
- State (last_run + per-note hashes) is stored in `note_exporter_state.db` alongside OpenWebUI's `webui.db` in `DATA_DIR`. The repo itself contains only the note files.

## 8. Per-Note Version History

Each note file is a separate Markdown file. The GitHub Contents API creates one commit per file update. On GitHub, users can:
1. Navigate to `<file>.md` (at repo root)
2. Click **History**
3. See every past version with diff
4. Copy any past version and paste back into OpenWebUI

This satisfies the "see previous version" + "manual copy-paste restore" requirement.

## 9. Technical Implementation Details

### SQLite Access
```python
import sqlite3
import os

try:
    from open_webui.config import DATA_DIR
except ImportError:
    DATA_DIR = "/app/backend/data"

DB_PATH = os.path.join(DATA_DIR, "webui.db")
STATE_DB_PATH = os.path.join(DATA_DIR, "note_exporter_state.db")
```

Two SQLite files are used:
- **`webui.db`** (read-only): OpenWebUI's own database. Read with `mode=ro` to avoid contention with OpenWebUI writes.
- **`note_exporter_state.db`** (read-write, our own file): stores `last_run` per user and `short_id → sha256` per note. Opened in WAL mode for safe concurrent reads. Never touches `webui.db`.

State DB schema:
```sql
CREATE TABLE IF NOT EXISTS backup_state (
    user_id TEXT PRIMARY KEY,
    last_run TEXT            -- ISO 8601 datetime string
);
CREATE TABLE IF NOT EXISTS note_hashes (
    user_id TEXT NOT NULL,
    short_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,    -- 64-char hex
    PRIMARY KEY (user_id, short_id)
);
```

Renames and removals are detected by listing the **repo root** each backup and matching each file's `short_id` (extracted from the filename via the regex `-([0-9a-f]{8})\.md$`) against the current set of note IDs. Files whose names don't match the expected `<slug>-<short_id>.md` pattern are left alone (e.g., manually-added or manually-renamed files in the repo).

### Pydantic Models

All data crossing a boundary is validated through Pydantic v2 models:

| Model | Purpose | Storage |
|---|---|---|
| `User` | Validated `__user__` context from inlet | None (in-memory only) |
| `Note` | One row from `SELECT … FROM note` | None (in-memory only) |
| `BackupState` | `last_run` per user | `backup_state` table (TEXT) |
| `NoteHash` | One `short_id → sha256` mapping per user | `note_hashes` table (TEXT) |
| `GitHubContent` | One entry from a GitHub Contents listing | None (in-memory only) |

`from __future__ import annotations` is enabled so all annotations are deferred strings — eliminates forward-reference issues between the `Filter` class and these models defined later in the file.

### Self-Healing State Reconciliation

The skip-if-unchanged condition is:
```python
if existing is not None and existing.sha256 == content_hash and short_id in repo_files:
    continue
```

Three conditions, all required to skip:
1. We have a stored hash for this `short_id` (`existing is not None`)
2. The content hasn't changed (`existing.sha256 == content_hash`)
3. The file **actually exists on GitHub right now** (`short_id in repo_files`)

If the user deletes files from GitHub manually (or any external state divergence), condition (3) fails and the note is re-uploaded. The next successful backup rebuilds the local state to match.

This treats the GitHub listing as the source of truth for "does this file exist?", while keeping the local hash table for "did the content change?". The two together make the backup self-healing against external deletions.

### Defensive Scrubbing

Two defensive layers protect against bad input:

1. **`_clean_url(url)`** — strips ASCII control characters (`\x00-\x1f`, `\x7f`) from any URL before passing it to `httpx`. Applied to all 4 GitHub API calls. Catches cases like a `GITHUB_REPO_OWNER` value with a trailing newline.
2. **`_make_filename`** — applies the same control-char scrub to the final filename as the last step. Catches cases where a note title contains characters that somehow survive `_slugify`.

Both are idempotent on clean strings (no-op when no control chars present).

### Logging

Minimal logger setup at module level:
```python
logger = logging.getLogger("openwebui.plugins.note_exporter")
```

Lifecycle events at INFO:
- `inlet user_id=<id>` (per inlet call, after user validation)
- `backup starting user_id=<id>` (per backup start)
- `backup completed user_id=<id> notes=<n>` (per backup success)

Failures at WARNING or EXCEPTION:
- All previously-silent `except Exception: pass` blocks now log a warning with the exception message
- The broad `except` in `_run_backup` uses `logger.exception()` to include the full traceback

The hot path (`inlet`) is **log-free** for skip paths — no chat delay from logging when the user is not due for backup.

### GitHub API
- `GET /repos/{owner}/{repo}/contents/{path}` — read file (get `sha` for updates).
- `GET /repos/{owner}/{repo}/contents/{dir}` — list directory at root (used to detect renames/removals; returns `[]` on 404).
- `PUT /repos/{owner}/{repo}/contents/{path}` — create/update file (base64 content, message, sha).
- `DELETE /repos/{owner}/{repo}/contents/{path}` — delete file (requires sha).
- Auth: `Authorization: Bearer <GITHUB_TOKEN>` header.
- Rate limit: 5000 requests/hour per authenticated user (fine for personal use).

### Error Handling
- **Hot path (`inlet`):** Never throws. All exceptions caught → return `body`.
- **Background task:** A single `except Exception: logger.exception(...)` at the top of `_run_backup` catches everything. The `_busy` flag is reset via `finally`. Silent `except Exception` blocks in DB and GitHub methods log a `WARNING` with the exception message.
- **GitHub 409 (conflict) on PUT** → re-GET sha → retry PUT once.
- **Network/DB errors** → log, leave `last_run` unchanged → retry next eligible slot.

### Concurrency
- Per-user `_busy` flag (dict keyed by `user_id`) prevents duplicate concurrent backups for the same user.
- Different users can back up concurrently (independent repos, independent asyncio tasks).

## 10. Security Considerations

| Concern | Mitigation |
|---|---|
| GitHub PAT in UserValves | Stored in OpenWebUI's DB per-user. Fine-grained, repo-scoped, with expiry. Users rotate their own. |
| Cross-user data leakage | Function reads `WHERE user_id = current_user_id` only. No admin key reads all users. |
| Direct DB access | Read-only (`mode=ro`). No writes to `webui.db`. |
| Network calls from Function | Only to `api.github.com`. No external data exfiltration. |
| Admin trust | The admin who installs the function trusts the code (standard OpenWebUI function model). |

## 11. User Experience

### Admin (one time)
1. Go to Admin Panel → Functions → Create New Function.
2. Paste the Function code.
3. Set `is_global = True`, `is_active = True`.
4. Save. Done.

### User (self-service)
1. Create a **private** GitHub repository.
2. Generate a **fine-grained PAT** scoped to that repo (Contents: Read & Write).
3. In OpenWebUI, open **Controls** (like the memory function screenshot).
4. Find the Function's UserValves.
5. Set:
   - `ENABLED = true`
   - `GITHUB_TOKEN = <your-pat>`
   - `GITHUB_REPO_OWNER = <your-username>` (or org)
   - `GITHUB_REPO_NAME = <your-repo>`
   - `TIMEZONE = America/New_York` (select your timezone from the dropdown; default `UTC`)
   - `SCHEDULE = 09:00,12:00,15:00` (in your local time)
6. Save.
7. Chat normally. The first eligible message after the next scheduled slot triggers the initial backup.

## 12. Future Enhancements (out of scope for v1)

- Bulk/atomic multi-file commits via GitHub Git Data API (trees + blobs) instead of one commit per file.
- Encrypted notes (`age`/`gpg`) before commit for extra-sensitive content.
- Action Function companion for manual "Backup Now" button.
- Support for PostgreSQL backend (currently SQLite-only).
- Admin dashboard showing backup status per user.

## 13. Supported OpenWebUI Versions

Designed for OpenWebUI versions with:
- Global Filter support (`is_global`, `is_active`)
- `UserValves` accessible via Controls panel
- Standard `note` table in `webui.db`

Exact version compatibility to be validated during testing.

---

*This specification is the source of truth for implementation.*
