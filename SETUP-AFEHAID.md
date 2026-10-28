# Setup notes

Last verified: 2026-10-29.

- Fork: https://github.com/AFehaid/herdr-t3-import
- Version: 0.1.1; herdr plugin ID: `t3-import`.
- Already linked and enabled with `herdr plugin link ~/projects/herdr-t3-import --enabled`.
- Fix commit `3b5ec45` successfully pushed to `origin/main` using `git push origin main`; no force-push or upstream PR.
- Token: `~/.config/t3-import/token`, verified mode `600`, configured TTL 30 days. Token contents were not printed.
- Existing backup: `~/backups/t3-herdr-20261029-010306/` (verified present).

## Import verification

Ran `python3 t3_import.py sync` twice; both runs exited successfully.

| Project | Run 1: sessions in T3 / skipped | Run 2: sessions in T3 / skipped | Threads before / after run 1 / after run 2 |
| --- | --- | --- | --- |
| aflak | 8 / 1 | 8 / 1 | 8 / 8 / 8 |
| models-tests | 2 / 0 | 2 / 0 | 2 / 2 / 2 |
| gpu | 4 / 0 | 4 / 0 | 5 / 5 / 5 |
| Vision 360 | 4 / 0 | 4 / 0 | 4 / 4 / 4 |
| mirsad | 10 / 0 | 10 / 0 | 10 / 10 / 10 |
| Agentic-Lab | 25 / 0 | 25 / 0 | 25 / 25 / 25 |
| **Total** | **53 / 1** | **53 / 1** | **54 / 54 / 54** |

The command's reported session counts are not counts of newly created threads:
both runs created **zero new threads**. Read-only SQLite snapshots of
`~/.t3/userdata/statev2.sqlite` confirmed identical thread IDs, project assignments,
and deletion state across both runs in `orchestration_v2_projection_threads`.
No provider-session mapping pointed to multiple distinct threads. These two runs
were idempotent: no duplicate threads were created.

All five existing unit tests passed. The rejected-token test emitted a non-failing
Python ResourceWarning while cleaning up its simulated HTTP 401 response.

Desktop verification was initially blocked by Computer Use access approval, then
explicitly waived by the user in favor of the database comparison. No screenshot
was captured. No thread messages were sent, no other herdr panes or agents were
touched, and neither herdr nor T3 was restarted. No deletes or resets were run.

## Recommended schedule — not enabled

Recommend one daily run of `python3 t3_import.py sync` from this project directory
while T3 is running, with overlapping runs prevented. No timer, watch process,
or scheduled task was enabled. On-demand imports remain available.
The stored token has a 30-day TTL and will need renewal before expiry for
continued scheduled use. Imports are one-time session copies, not ongoing
synchronization of later terminal messages.
