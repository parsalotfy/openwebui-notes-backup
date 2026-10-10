from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo, available_timezones

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

try:
    from open_webui.config import DATA_DIR
except ImportError:
    DATA_DIR = "/app/backend/data"

DB_PATH = os.path.join(DATA_DIR, "webui.db")
STATE_DB_PATH = os.path.join(DATA_DIR, "note_exporter_state.db")

logger = logging.getLogger("openwebui.plugins.note_exporter")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


class Filter:
    class Valves(BaseModel):
        pass

    class UserValves(BaseModel):
        ENABLED: bool = Field(
            default=False,
            description="Master opt-in toggle. Must be true for backup to run."
        )
        GITHUB_TOKEN: str = Field(
            default="",
            description="Fine-grained Personal Access Token scoped to your private repo (Contents: Read & Write)."
        )
        GITHUB_REPO_OWNER: str = Field(
            default="",
            description="GitHub repository owner (username or org). Example: 'octocat'"
        )
        GITHUB_REPO_NAME: str = Field(
            default="",
            description="GitHub repository name. Example: 'my-notes-backup'"
        )
        TIMEZONE: str = Field(
            default="UTC",
            description="Your timezone (select from dropdown). SCHEDULE times are interpreted in this timezone.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": "get_timezones"
                }
            }
        )
        SCHEDULE: str = Field(
            default="",
            description="Comma-separated times in your timezone. Example: 09:00,12:00,15:00"
        )
        DELETE_REMOVED_NOTES: bool = Field(
            default=True,
            description="If true, deleting a note in OpenWebUI also deletes its file from GitHub."
        )

        @classmethod
        def get_timezones(cls) -> list[str]:
            """Return all available IANA timezone names for the dropdown."""
            return sorted(available_timezones())

        @field_validator("GITHUB_TOKEN", "GITHUB_REPO_OWNER", "GITHUB_REPO_NAME", mode="before")
        @classmethod
        def _strip_strings(cls, v):
            # Valve text fields may pick up stray whitespace/newlines from the
            # settings UI (e.g. a pasted repo name). Normalise at the source so
            # API calls never depend on URL scrubbing to stay valid.
            return v.strip() if isinstance(v, str) else v

    def __init__(self):
        self.valves: Filter.Valves = self.Valves()
        self._busy: dict[str, bool] = {} # user_id -> True

    # called before the LLM sees the message
    async def inlet(self, body: dict, __user__: dict | None = None) -> dict:
        if not __user__:
            return body

        try:
            user = User.model_validate(__user__)
        except ValidationError:
            return body

        user_id = user.id
        user_valves = user.valves
        logger.info(f"Note Exporter: inlet user_id={user_id}")

        if not user_valves:
            return body

        enabled = user_valves.ENABLED
        if not enabled:
            return body

        token = user_valves.GITHUB_TOKEN
        owner = user_valves.GITHUB_REPO_OWNER
        repo = user_valves.GITHUB_REPO_NAME
        schedule = user_valves.SCHEDULE
        timezone_name = user_valves.TIMEZONE or "UTC"

        if not token or not owner or not repo or not schedule:
            return body

        if not user_id:
            return body

        try:
            tz = ZoneInfo(timezone_name)
        except Exception as e:
            logger.warning(f"Note Exporter: invalid TIMEZONE for user {user_id}: {e}")
            return body

        try:
            slots = self._parse_schedule(schedule)
            if not slots:
                return body

            now = datetime.now(tz)

            state = self._load_last_run(user_id)
            if state is None:
                today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
                last_run = today
            else:
                last_run = state.last_run

            next_slot = self._find_pending_slot(slots, last_run, tz)
            if not next_slot:
                return body

            if now < next_slot:
                return body

            if self._busy.get(user_id):
                return body

            self._busy[user_id] = True
            asyncio.create_task(self._run_backup(user_id, user_valves, tz))
        except Exception as e:
            logger.warning(f"Note Exporter: failed to schedule backup for user {user_id}: {e}")

        return body

    def _parse_schedule(self, schedule: str) -> list[time]:
            slots = []
            for s in schedule.split(","):
                s = s.strip()
                if not s:
                    continue
                try:
                    slots.append(time.fromisoformat(s))
                except ValueError:
                    continue
            return sorted(slots)

    def _init_state_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(STATE_DB_PATH)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS backup_state ("
                "user_id TEXT PRIMARY KEY, last_run TEXT"
                ")"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS note_hashes ("
                "user_id TEXT NOT NULL, short_id TEXT NOT NULL, sha256 TEXT NOT NULL, "
                "PRIMARY KEY (user_id, short_id)"
                ")"
            )
            conn.commit()
        except Exception as e:
            logger.warning(f"Note Exporter: failed to init state DB: {e}")
        return conn
    
    def _load_last_run(self, user_id: str) -> BackupState | None:
        try:
            conn = self._init_state_db()
            try:
                row = conn.execute(
                    "SELECT last_run FROM backup_state WHERE user_id = ?", (user_id,)
                ).fetchone()
                if not row:
                    return None
                return BackupState.model_validate({"user_id": user_id, "last_run": row[0]})
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Note Exporter: failed to load last_run for {user_id}: {e}")
            return None
    
    async def _run_backup(self, user_id: str, user_valves: "Filter.UserValves", tz: ZoneInfo) -> None:
        logger.info(f"Note Exporter: backup starting user_id={user_id}")
        try:
            # Belt and braces next to the UserValves strip-validator: valve text
            # fields may carry stray whitespace from the settings UI.
            token = (user_valves.GITHUB_TOKEN or "").strip()
            owner = (user_valves.GITHUB_REPO_OWNER or "").strip()
            repo = (user_valves.GITHUB_REPO_NAME or "").strip()
            delete_removed = user_valves.DELETE_REMOVED_NOTES

            client = httpx.AsyncClient()
            try:
                old_hashes = self._load_hashes(user_id)

                notes = self._get_notes_from_db(user_id)
                if notes is None:
                    return

                current_hashes: dict[str, str] = {}
                current_short_ids: set[str] = set()
                # Every note id present in the DB, parseable or not. The
                # delete-removed pass must only delete files whose note is
                # genuinely gone — an unparseable note still owns its file.
                db_short_ids: set[str] = set()
                for note in notes:
                    md_content = self._extract_markdown(note.data)
                    if md_content is None:
                        continue
                    short_id = note.id[:8]
                    current_hashes[short_id] = hashlib.sha256(md_content.encode("utf-8")).hexdigest()
                    current_short_ids.add(short_id)
                for note in notes:
                    db_short_ids.add(note.id[:8])

                listing = await self._github_list_dir(client, token, owner, repo, "")
                if listing is None:
                    # A failed directory listing is indistinguishable from an
                    # empty repo if we proceed with {}: every note would look
                    # new and be PUT-created (422 on existing files). Abort.
                    logger.error(f"Note Exporter: backup aborted user_id={user_id}: directory listing failed")
                    return
                repo_files: dict[str, GitHubContent] = {}
                for entry in listing:
                    if entry.type != "file":
                        continue
                    short_id = self._extract_note_id_from_filename(entry.name)
                    if short_id is None:
                        continue
                    repo_files[short_id] = entry

                failures: list[str] = []

                for note in notes:
                    note_id = note.id
                    title = note.title or "untitled"
                    md_content = self._extract_markdown(note.data)
                    if md_content is None:
                        logger.warning(
                            f"Note Exporter: skipping unparseable note id={note_id} title={title!r}"
                        )
                        continue

                    short_id = note_id[:8]
                    content_hash = current_hashes[short_id]

                    existing = old_hashes.get(short_id)
                    if existing is not None and existing.sha256 == content_hash and short_id in repo_files:
                        continue

                    expected_filename = self._make_filename(note_id, title)
                    repo_entry = repo_files.get(short_id)
                    file_sha = None
                    old_path: str | None = None
                    if repo_entry:
                        actual_path = repo_entry.path
                        if actual_path != expected_filename:
                            # Rename: write the new file FIRST and delete the
                            # old path only after the PUT succeeds. The old
                            # delete-then-create order left a gap where a
                            # failed create lost the note until the next run.
                            old_path = actual_path
                            file_sha = None
                        else:
                            file_sha = repo_entry.sha

                    if old_hashes.get(short_id) is None:
                        commit_msg = f"notes: add {title}"
                    else:
                        commit_msg = f"notes: update {title} ({short_id})"

                    success = await self._github_put(
                        client, token, owner, repo, expected_filename,
                        md_content, commit_msg, file_sha
                    )
                    if not success:
                        failures.append(expected_filename)
                        continue

                    if old_path is not None:
                        old_sha = await self._github_get_sha(client, token, owner, repo, old_path)
                        if old_sha:
                            deleted = await self._github_delete(
                                client, token, owner, repo, old_path,
                                f"notes: rename {title}", old_sha
                            )
                            if not deleted:
                                failures.append(old_path)
                                continue
                        repo_files.pop(short_id, None)

                    # Record progress per note so a later failure does not
                    # force the next run to re-push notes that already landed.
                    old_hashes[short_id] = NoteHash(
                        user_id=user_id, short_id=short_id, sha256=content_hash
                    )
                    self._save_hashes(user_id, old_hashes)

                if delete_removed:
                    for short_id, entry in repo_files.items():
                        if short_id in db_short_ids:
                            continue
                        path = entry.path
                        file_sha = entry.sha
                        if file_sha:
                            deleted = await self._github_delete(
                                client, token, owner, repo, path,
                                "notes: delete removed note", file_sha
                            )
                            if not deleted:
                                failures.append(path)

                if not failures:
                    note_hashes = [
                        NoteHash(user_id=user_id, short_id=sid, sha256=h)
                        for sid, h in current_hashes.items()
                    ]
                    self._save_state(
                        BackupState(user_id=user_id, last_run=datetime.now(tz)),
                        note_hashes,
                    )
                    logger.info(f"Note Exporter: backup completed user_id={user_id} notes={len(notes)}")
                else:
                    # State intentionally keeps the OLD last_run so the next
                    # chat activity retries the failed paths (per-note hashes
                    # above already exclude them from re-push).
                    logger.warning(
                        f"Note Exporter: backup finished with failures "
                        f"user_id={user_id} failed={len(failures)} paths={failures}"
                    )

            finally:
                await client.aclose()

        except Exception:
            logger.exception(f"Note Exporter: backup failed user_id={user_id}")
        finally:
            self._busy[user_id] = False

    def _load_hashes(self, user_id: str) -> dict[str, NoteHash]:
        try:
            conn = self._init_state_db()
            try:
                rows = conn.execute(
                    "SELECT short_id, sha256 FROM note_hashes WHERE user_id = ?", (user_id,)
                ).fetchall()
                return {
                    short_id: NoteHash(user_id=user_id, short_id=short_id, sha256=sha256)
                    for short_id, sha256 in rows
                }
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Note Exporter: failed to load hashes for {user_id}: {e}")
            return {}

    def _save_state(self, state: BackupState, hashes: list[NoteHash]) -> None:
        try:
            conn = self._init_state_db()
            try:
                conn.execute(
                    "INSERT INTO backup_state (user_id, last_run) VALUES (?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET last_run = excluded.last_run",
                    (state.user_id, state.last_run.isoformat()),
                )
                conn.execute("DELETE FROM note_hashes WHERE user_id = ?", (state.user_id,))
                if hashes:
                    conn.executemany(
                        "INSERT INTO note_hashes (user_id, short_id, sha256) VALUES (?, ?, ?)",
                        [(h.user_id, h.short_id, h.sha256) for h in hashes],
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Note Exporter: failed to save state for {state.user_id}: {e}")

    def _save_hashes(self, user_id: str, hashes: dict[str, NoteHash]) -> None:
        """Persist per-note hashes without touching last_run.

        Called after each successful PUT so a later failure in the same run
        does not force the next run to re-push notes that already landed.
        """
        try:
            conn = self._init_state_db()
            try:
                conn.execute("DELETE FROM note_hashes WHERE user_id = ?", (user_id,))
                if hashes:
                    conn.executemany(
                        "INSERT INTO note_hashes (user_id, short_id, sha256) VALUES (?, ?, ?)",
                        [(h.user_id, h.short_id, h.sha256) for h in hashes.values()],
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Note Exporter: failed to save hashes for {user_id}: {e}")

    def _extract_note_id_from_filename(self, name: str) -> str | None:
        m = re.search(r"-([0-9a-f]{8})\.md$", name)
        return m.group(1) if m else None

    def _get_notes_from_db(self, user_id: str) -> list[Note] | None:
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            query = "SELECT id, title, data FROM note WHERE user_id = ?"
            cursor.execute(query, (user_id,))
            rows = cursor.fetchall()
            conn.close()

            return [Note(id=row["id"], title=row["title"], data=row["data"]) for row in rows]

        except Exception as e:
            logger.warning(f"Note Exporter: failed to read notes from DB for {user_id}: {e}")
            return None

    def _extract_markdown(self, data) -> str | None:
        if not data:
            return None
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except:
                return None

        content = data.get("content")
        if not content:
            return None

        md = content.get("md")
        return md

    def _make_filename(self, note_id: str, title: str) -> str:
        slug = self._slugify(title)
        short_id = note_id[:8]
        filename = f"{slug}-{short_id}.md"
        return re.sub(r"[\x00-\x1f\x7f]", "", filename)

    def _slugify(self, text: str) -> str:
        text = unicodedata.normalize("NFKD", text)
        text = text.encode("ascii", "ignore").decode("ascii")
        text = text.lower()
        text = re.sub(r"[^\w\s-]", "", text)
        text = re.sub(r"[-\s]+", "-", text).strip("-_")
        return text[:50] or "untitled"
 
    def _find_pending_slot(self, slots, last_run: datetime, tz: ZoneInfo) -> datetime | None:
        if not slots:
            return None

        for slot in slots:
            candidate = datetime.combine(last_run.date(), slot, tzinfo=tz)
            if candidate > last_run:
                return candidate

        return datetime.combine(last_run.date() + timedelta(days=1), slots[0], tzinfo=tz)

    @staticmethod
    def _clean_url(url: str) -> str:
        """Strip ASCII control characters that httpx rejects in URLs."""
        return re.sub(r"[\x00-\x1f\x7f]", "", url)

    async def _github_get(self, client, token, owner, repo, path) -> GitHubContent | None:
        try:
            url = self._clean_url(f"https://api.github.com/repos/{owner}/{repo}/contents/{path}")
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json"
            }
            response = await client.get(url, headers=headers)

            if response.status_code != 200:
                return None
            return GitHubContent.model_validate(response.json())
        except ValidationError as e:
            logger.warning(f"Note Exporter: GitHub GET {path} returned invalid content: {e}")
            return None
        except Exception as e:
            logger.warning(f"Note Exporter: GitHub GET {path} failed: {e}")
            return None

    async def _github_get_sha(self, client, token, owner, repo, path) -> str | None:
        result = await self._github_get(client, token, owner, repo, path)
        if result is None:
            return None
        return result.sha

    async def _github_list_dir(self, client, token, owner, repo, path) -> list[GitHubContent] | None:
        """List directory contents. Returns None (not []) on request failure
        so callers can abort instead of mistaking failure for an empty repo."""
        try:
            url = self._clean_url(f"https://api.github.com/repos/{owner}/{repo}/contents/{path}")
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json"
            }
            response = await client.get(url, headers=headers)
            if response.status_code != 200:
                logger.warning(
                    f"Note Exporter: GitHub LIST {path} failed: "
                    f"status={response.status_code} body={response.text[:500]}"
                )
                return None
            data = response.json()
            if not isinstance(data, list):
                return []
            result: list[GitHubContent] = []
            for entry in data:
                try:
                    result.append(GitHubContent.model_validate(entry))
                except ValidationError as e:
                    logger.warning(f"Note Exporter: GitHub LIST {path} skipping invalid entry: {e}")
                    continue
            return result
        except Exception as e:
            logger.warning(f"Note Exporter: GitHub LIST {path} failed: {e}")
            return None

    async def _github_put(self, client, token, owner, repo, path, content, message, sha=None) -> bool:
        try:
            url = self._clean_url(f"https://api.github.com/repos/{owner}/{repo}/contents/{path}")
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json"
            }

            content_b64 = base64.b64encode(content.encode("utf-8")).decode("utf-8")
            data = {
                "message": message,
                "content": content_b64
            }
            if sha:
                data["sha"] = sha

            response = await client.put(url, headers=headers, json=data)

            if response.status_code in [200, 201]:
                return True
            elif response.status_code in (409, 422):
                # 409: sha conflict. 422: often "sha wasn't supplied" because
                # the file appeared (or the ref settled) between our listing
                # and this call — e.g. right after deleting a renamed path.
                # Re-fetch the sha and retry once with it.
                new_sha = await self._github_get_sha(client, token, owner, repo, path)
                if new_sha and new_sha != sha:
                    data["sha"] = new_sha
                    response = await client.put(url, headers=headers, json=data)
                    if response.status_code in [200, 201]:
                        return True
                logger.warning(
                    f"Note Exporter: GitHub PUT {path} failed: "
                    f"status={response.status_code} body={response.text[:500]}"
                )
                return False
            else:
                logger.warning(
                    f"Note Exporter: GitHub PUT {path} failed: "
                    f"status={response.status_code} body={response.text[:500]}"
                )
                return False
        except Exception as e:
            logger.warning(f"Note Exporter: GitHub PUT {path} failed: {e}")
            return False

    async def _github_delete(self, client, token, owner, repo, path, message, sha) -> bool:
        try:
            url = self._clean_url(f"https://api.github.com/repos/{owner}/{repo}/contents/{path}")
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json"
            }
            data = {
                "message": message,
                "sha": sha
            }

            response = await client.request("DELETE", url, headers=headers, json=data)

            if response.status_code == 200:
                return True
            elif response.status_code in (409, 422):
                new_sha = await self._github_get_sha(client, token, owner, repo, path)
                if new_sha and new_sha != sha:
                    data["sha"] = new_sha
                    response = await client.request("DELETE", url, headers=headers, json=data)
                    if response.status_code == 200:
                        return True
                logger.warning(
                    f"Note Exporter: GitHub DELETE {path} failed: "
                    f"status={response.status_code} body={response.text[:500]}"
                )
                return False
            else:
                logger.warning(
                    f"Note Exporter: GitHub DELETE {path} failed: "
                    f"status={response.status_code} body={response.text[:500]}"
                )
                return False
        except Exception as e:
            logger.warning(f"Note Exporter: GitHub DELETE {path} failed: {e}")
            return False


class User(BaseModel):
    id: str
    email: str | None = None
    name: str | None = None
    role: str | None = None
    valves: Filter.UserValves | None = None


class Note(BaseModel):
    id: str
    title: str | None = None
    data: str | None = None


class BackupState(BaseModel):
    user_id: str
    last_run: datetime


class NoteHash(BaseModel):
    user_id: str
    short_id: str
    sha256: str


class GitHubContent(BaseModel):
    """A single file/dir entry from the GitHub Contents API."""
    name: str
    path: str
    sha: str
    type: str
