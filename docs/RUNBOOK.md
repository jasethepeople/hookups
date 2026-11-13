# Runbook

## Add a source

1. Add `src/sources/<name>.py` with an explicit profile-driven URL builder, a 30-minute response cache, and a 30-minute minimum poll interval. Its CLI must accept `--db`; the scheduler passes the project database path to every collector.
2. Record every poll in `runs` with one of `succeeded`, `partial`, `blocked`, or `failed`; keep error summaries short and free of response bodies, credentials, and listing text.
3. Add the source to `SOURCE_SCRIPTS` and its interval to `build_scheduler()` in `src/scheduler.py`. If the source value is stored in SQLite, update the schema and migration together.
4. Add parser, retry, cache, circuit-breaker, and scheduler tests. Run the full `pytest` suite before restarting the scheduler.
5. Do not infer location or preferences from profile gaps. Do not defeat access controls; leave unresolved challenges in `docs/GAPS.md`.

## Rotate the proxy URL

1. Edit only `PROXY_URL` in `~/hookups/.env`; never paste it into chat, source files, logs, or a commit. Keep the file mode `600`.
2. Use a quoted value if the URL contains `#`, spaces, or shell-sensitive characters, for example `PROXY_URL="http://user:password@host:port"`.
3. Run one permitted source poll and check its result at the private dashboard’s `/status` endpoint. Source processes read `.env` at launch; the next poll sees the rotated value.
4. If the provider issued a replacement credential, revoke the old one with that provider after verifying the new URL works.

## Restore a backup

Backups are SQLite online-backup snapshots at `~/backups/hookups-<UTC timestamp>.sqlite3`. The nightly job retains the newest seven and gives the directory and files owner-only permissions.

1. Stop the scheduler with `pkill -f '[p]ython -B -m src.scheduler' || true`. Then pause the dashboard service (`hookups-dashboard-demo`) from [Websites → Services](/?t=sites&s=services) so neither writes during replacement.
2. Select the desired backup and verify it before touching the live database:

   ```bash
   candidate="$HOME/backups/hookups-YYYYMMDDTHHMMSSZ.sqlite3"
   python -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute("PRAGMA integrity_check").fetchone()[0]); c.close()' "$candidate"
   ```

   Continue only if the result is `ok`.
3. Preserve the current database, install the verified snapshot atomically, and restore private permissions:

   ```bash
   cp "$HOME/hookups/db/hookups.sqlite3" "$HOME/hookups/db/hookups.sqlite3.pre-restore"
   chmod 600 "$HOME/hookups/db/hookups.sqlite3.pre-restore"
   cp "$candidate" "$HOME/hookups/db/hookups.sqlite3.restore"
   chmod 600 "$HOME/hookups/db/hookups.sqlite3.restore"
   mv "$HOME/hookups/db/hookups.sqlite3.restore" "$HOME/hookups/db/hookups.sqlite3"
   ```
4. Resume the dashboard service and start the scheduler with `cd "$HOME/hookups" && nohup .venv/bin/python -B -m src.scheduler --database db/hookups.sqlite3 --output out/digest.txt > logs/scheduler.log 2>&1 < /dev/null &`.
5. Check `/status`, the scheduler log, and the database integrity result. Keep the `.pre-restore` copy until the restored system has been verified.
