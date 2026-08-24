# SQLite State Backup, Restore, And Retention/GC

This page defines the R4.6 deletion rules, reference graph, and operator commands. Every command operates only on explicit files or directories. There is no `--force` option, no database overwrite, and no broad recursive-delete interface.

## Consistency Boundary

`StateStore` WAL transactions, leases, and fencing coordinate only local workers that open the same SQLite file. They do not provide cross-host consensus, network-partition safety, regional replication, or correctness on a shared network filesystem. A multi-host deployment must replace this coordination layer with a store that has those guarantees.

Online backup uses Python's official `sqlite3.Connection.backup()` API. The database payload therefore contains a consistent view of committed transactions; it does not copy live `-wal/-shm` files or uncommitted pages. Artifact bodies live outside SQLite. The backup reads their index from the database snapshot, copies only those exact files, and verifies managed-root containment, size, and SHA-256. A missing, moved, or changing Artifact aborts the unpublished staging bundle.

## References And Deletion Order

| Owner/truth | Direct or logical references | Deletion rule |
|---|---|---|
| `sessions` | `messages`, `runs`, checkpoints, leases | Delete last; every run in the session must qualify together |
| `runs` | journal, finalizer/hooks, tool envelope/calls, tool/provider events, Plan/Evidence, budget, delegation, API requests | Delete referenced rows first and leave no recoverable cursor |
| `run_journals` | checkpoint, Plan/Evidence, operation ref, Artifact, last tool event | May leave retention only in `terminal/cancelled/failed` phase |
| `tool_operation_refs` | external or same-file `tool_operations/tool_outbox` state | `manual_review` is never auto-deleted; nonterminal operation or unpublished outbox blocks the session |
| `context_checkpoints` | archived messages, parent checkpoint, Artifact/operation/citation manifests | A checkpoint referenced by a nonterminal journal is active and cannot be deleted |
| `artifacts` | typed message ref, checkpoint, Evidence, journal, delegation result | Only an eligible owner cohort may remove parents; then require expiry, zero references, no hold, and matching path/hash |
| `trace_event_index` | rebuildable projection | Removed with a session only after its configured audit window |
| `audit_events` | security, lifecycle, backup/restore/GC decisions | R4.6 GC does not delete these; a dry-run writes an audit record |
| `memories` | `source_session_id` | An existing source reference blocks session deletion |
| `retention_holds` | exact session/run/Artifact id | An unexpired hold blocks GC and is released only by exact hold id |

GC operates on a complete session cohort, rather than deleting an isolated old run. A session qualifies only when all of these are true:

- `sessions.updated_at` is older than policy; every run is `completed/failed/interrupted`, with an old enough `finished_at`.
- Every root has explicit finalizer, journal, and budget truth; finalizer reached `cleanup_done` (cursor 7), journal is terminal, and the root budget is finalized with no persisted or operation reservation. Missing truth fails closed.
- There is no active lease, retained API replay, recovery recommendation, active delegation, memory reference, or retention hold.
- The root and every delegated child, finalizer, journal, budget, operation, and published outbox terminal timestamp have all exceeded the same terminal retention window; delegated worker ownership/lease is clear.
- Operations agree with their refs and are only `committed/failed/compensated`. `manual_review`, `prepared`, `approved`, `executing`, and `compensating` block GC.
- When operation refs exist, `--operation-db` proves that every corresponding outbox row is `published` and old enough. Omitting it fails closed as `operation_outbox_unverified`.
- No nonterminal journal references a checkpoint. Artifacts have independently expired and become unreferenced after parent deletion.

`terminal-age-seconds` is also the minimum retention window for run Trace projections. Deployments needing a longer legal or audit window must increase it or add an exact retention hold. `abandoned`, `uncertain`, `manual_review`, and replayable API requests are not terminal GC candidates.

Artifact removal has two durable phases. The first SQLite transaction removes parents in reference order and sets `gc_pending_at`. An Artifact in a blocked or active owner session is never selected merely because it has no current logical reference. After commit, GC takes a bounded SQLite write lock, rechecks zero references and exact Artifact holds, unlinks only the indexed, verified file, and removes its metadata. A later GC resumes any interrupted `gc_pending_at` work. Unknown files are never scanned or deleted.

The operation database is a read/lock proof source for StateStore cohort deletion. R4.6 removes the corresponding StateStore `tool_operation_refs`, but deliberately does not delete external `tool_operations`, outbox, approvals, or consumer receipts; those remain under the business database's own idempotency and retention policy.

## Backup And Restore

Replace these example paths with explicit deployment paths:

```bash
uv run --frozen python scripts/state_maintenance.py backup \
  --state /srv/edu-agent/state.db \
  --artifacts /srv/edu-agent/artifacts \
  --target /srv/edu-agent-backups/2026-08-24T120000Z

uv run --frozen python scripts/state_maintenance.py verify-backup \
  --backup /srv/edu-agent-backups/2026-08-24T120000Z

uv run --frozen python scripts/state_maintenance.py restore \
  --backup /srv/edu-agent-backups/2026-08-24T120000Z \
  --target-dir /srv/edu-agent-restored

uv run --frozen python scripts/state_maintenance.py verify-state \
  --state /srv/edu-agent-restored/state.db \
  --artifacts /srv/edu-agent-restored/artifacts
```

The backup target must not exist. The bundle directory is mode `0700`; its database, manifest, and Artifact payloads are mode `0600`. The `edu-agent-state-backup.v1` manifest records UTC time, numeric schema version, migration ids, safe Python/SQLite/OS categories, page size, each payload size/SHA-256, and aggregate reference counts. It does not contain source absolute paths, hostname, username, endpoint, environment variables, or credentials.

The restore target must be a new path or an empty directory. An existing database, WAL/SHM, or unknown file refuses restore. Restore performs these steps:

1. Verify the manifest hash, every payload hash/size, schema upper bound, SQLite `integrity_check`, and `foreign_key_check`.
2. Restore the database and Artifacts in a private sibling staging directory and remap Artifact paths.
3. Run idempotent forward migrations, then validate session/run/journal/operation/checkpoint/Artifact scope and references.
4. Write a path-free restore audit, checkpoint WAL, and publish the complete staging directory as the target.

A schema newer than the running code returns `STATE_RESTORE_REFUSED`; it is never downgraded. An older snapshot can only be restored to a new directory and migrated forward. Failure removes only the tool-owned, strictly named staging directory and does not modify the backup or other user files.

## Retention/GC

GC defaults to an audited dry-run:

```bash
uv run --frozen python scripts/state_maintenance.py gc \
  --state /srv/edu-agent/state.db \
  --artifacts /srv/edu-agent/artifacts \
  --operation-db /srv/edu-agent/teaching.db \
  --terminal-age-seconds 2592000 \
  --artifact-age-seconds 2592000 \
  --batch-size 100
```

Review `eligible_sessions`, `eligible_artifacts`, and `blocked_reason_counts`, then add `--apply` to the same command. `batch-size` must be 1..1000. Drain API and Scheduler admission before apply so the bounded write transaction does not compete with new work. Do not replace this command with shell globs or broad directory deletion.

## Failure Semantics

SQLite write faults map to stable errors that contain no path or SQL text:

| Condition | API code | Behavior |
|---|---|---|
| Disk/quota full | `STATE_STORAGE_FULL` | 503; roll back the transaction and do not start a second finalizer write path |
| Read-only DB/filesystem | `STATE_STORAGE_READ_ONLY` | 503; readiness fails and new work is not accepted |
| Corrupt/non-SQLite DB | `STATE_STORAGE_CORRUPT` | 503, not automatically retryable; restore a verified backup to a new directory |
| Other I/O/interruption | `STATE_STORAGE_UNAVAILABLE` | 503; preserve committed journal/budget/fence state and resume from its stable cursor |

A failed final-message write cannot create a partial final because the message and finalizer cursor share one transaction. Recovery reuses the persisted candidate, so it neither calls the model again nor refreshes budget. Business write, ToolOperation receipt, and outbox retain their existing transaction and stable idempotency-key contract; backup and GC do not claim cross-system exactly-once.
