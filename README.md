# openwebui-notes-backup

An OpenWebUI Function that backs up your Notes to a **private GitHub repository** on a per-user schedule. Fully self-contained, no server changes, no SSH keys, no `git` CLI — just paste a Function into OpenWebUI and configure your GitHub PAT.

Each note becomes a separate Markdown file in your GitHub repo, giving you **full per-note version history** (diff, restore, copy-paste back into OpenWebUI).

---

## Features

- **Cloud backup** of all OpenWebUI Notes to a private GitHub repository.
- **Per-note version history** via GitHub's commit history (one commit per file update).
- **Per-user, self-service**: each user brings their own GitHub PAT and configures their own schedule.
- **Self-healing**: if you delete files from the GitHub repo manually, the next backup re-uploads them automatically.
- **Rename + delete detection**: renames a file in GitHub when you change a note's title, deletes files for notes you remove in OpenWebUI.
- **Type-safe**: built on Pydantic v2 models for all data boundaries.
- **Zero server changes**: single Function, installable from the OpenWebUI Admin panel.
- **OpenWebUI-native scheduling**: no cron, no systemd — runs on the next eligible chat request after your scheduled time.

---

## Installation

### As Admin (one time)

1. Open **Admin Panel → Functions → Create New Function**.
2. Paste the contents of [`function.py`](./function.py).
3. Set:
   - `is_global = True`
   - `is_active = True`
4. Save.

### As User (per account, to start backing up your notes)

1. Create a **private** GitHub repository (e.g. `my-openwebui-notes`).
2. Generate a **fine-grained Personal Access Token** at <https://github.com/settings/tokens?type=beta>:
   - Resource owner: your account
   - Repository access: **Only select repositories** → pick the one you just created
   - Permissions → Repository permissions → **Contents: Read and Write**
3. In OpenWebUI, open **Controls** (⚙️) and find this Function's **UserValves** section.
4. Configure:

   | Field | Value |
   |---|---|
   | `ENABLED` | `true` |
   | `GITHUB_TOKEN` | your fine-grained PAT |
   | `GITHUB_REPO_OWNER` | your GitHub username (or org name) |
   | `GITHUB_REPO_NAME` | your repo name (e.g. `my-openwebui-notes`) |
   | `TIMEZONE` | pick from dropdown (default `UTC`) |
   | `SCHEDULE` | `09:00,12:00,15:00` (in your timezone) |
   | `DELETE_REMOVED_NOTES` | `true` (recommended) |

5. Save. **Chat normally** — the first eligible message after your next scheduled slot will trigger the initial backup.

---

## How it works

```
user sends a chat message
        │
        ▼
Filter.inlet(body, __user__)        ← runs on every message
   │
   │  Skip silently if:
   │  • ENABLED != true
   │  • any required field empty
   │  • not yet time for the next scheduled slot
   │  • already running for this user
   │
   │  Otherwise → asyncio.create_task(_run_backup(...))
   ▼
_run_backup (background, fire-and-forget)
   │
   │  1. Load change-detection hashes from local SQLite
   │  2. Read your notes from webui.db (read-only)
   │  3. Compute SHA-256 of each note's markdown
   │  4. List your GitHub repo root
   │  5. For each note:
   │        skip if (hash unchanged) AND (file present in repo)
   │        else: PUT (new/updated) or DELETE-then-PUT (renamed)
   │  6. If DELETE_REMOVED_NOTES: DELETE repo files for removed notes
   │  7. Save updated hashes to local SQLite
```

The chat response is **never delayed** by a backup — the heavy work runs as a background task.

---

## Repository layout (in your GitHub repo)

```
<repo-root>
├── my-first-note-a1b2c3.md
├── meeting-notes-d4e5f6.md
└── ...
```

Files live at the **repo root**. The 8-character hex prefix in each filename is the first 8 chars of the note's UUID — it makes renames stable (the prefix doesn't change when the title changes).

State (last-run timestamp + per-note hashes) is stored locally in `note_exporter_state.db` next to OpenWebUI's `webui.db`. **The GitHub repo contains only the note files themselves.**

---

## Requirements

- **OpenWebUI** version with:
  - Global Filter support (`is_global`, `is_active`)
  - `UserValves` accessible from the Controls panel
  - Standard `note` table in `webui.db` (default for all OpenWebUI versions)
- **Python packages already bundled** with OpenWebUI: `httpx`, `pydantic` v2, `sqlite3` (stdlib).
- **GitHub account** with a private repository and a fine-grained PAT (Contents: Read & Write).

---

## Troubleshooting

### "I configured everything but nothing happens"

- Check the **schedule**: backups only run on the next chat message after a scheduled time. If you set `SCHEDULE = "09:00,12:00,15:00"` at 09:30, the next run is at 12:00.
- Test with a near-current time, e.g. add the current time to `SCHEDULE`, save, and send a chat message.
- Check OpenWebUI logs for entries containing `Note Exporter` — each backup logs start/complete.

### "Invalid non-printable ASCII character in URL"

Your `GITHUB_REPO_OWNER` or `GITHUB_REPO_NAME` has a trailing newline or other control character (common when copy-pasting from a terminal). Re-enter the value cleanly in the Controls panel.

### "The first run uploaded everything, but subsequent runs don't"

This is normal. After the first run, the function only uploads notes whose content actually changed (compared against a local SHA-256 cache). To force a fresh upload, delete the local state file:

```bash
# Inside the OpenWebUI container
rm -f /app/backend/data/note_exporter_state.db
```

Then trigger a backup (next scheduled slot, or temporarily set a near-current time in `SCHEDULE`).

### "I deleted all files from my GitHub repo, but the backup didn't re-upload them"

This used to happen (local state said "we already backed these up" even though GitHub was empty). The current version is **self-healing**: it cross-checks the local hash against the live GitHub listing, so a missing file triggers a re-upload on the next run. If you see this with the current version, please open an issue.

### "It renamed/deleted a file I didn't expect"

If `DELETE_REMOVED_NOTES = true` and you delete a note in OpenWebUI, the corresponding GitHub file is removed on the next backup. If you rename a note in OpenWebUI, the old GitHub file is deleted and a new one is created (the prefix in the filename is the note's UUID, which doesn't change with the title).

If a file in your GitHub repo doesn't match the `<slug>-<shortid>.md` pattern (e.g. you manually added a `README.md`), the backup **leaves it alone**.

---

## Documentation

- **README** (this file) — user-facing overview, install, troubleshooting.
- **[docs/specification.md](docs/specification.md)** — full design specification: architecture, scheduling algorithm, data model, error handling, security considerations.

---

## License

MIT — see [LICENSE](./LICENSE).
