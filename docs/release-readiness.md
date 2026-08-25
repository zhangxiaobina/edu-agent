# R5.5 Release Readiness

## Binary Decision

**NOT READY. Remain at R5.5.**

The intended package version is `0.1.0`. There is no candidate commit or tag.
This audit was performed on a dirty working tree based on
`a661e3669ca6bce7af6727eb90e18a0698feaade`; no commit, push, image publication,
deployment, release, or tag was created.

Two required publication proofs are missing:

1. The complete Stage 8 gate has passed in `development` mode, but has not run
   against a clean commit with `--evidence-mode candidate`.
2. The saved real-model report is a completed R5.2 run on commit
   `d3d3ea7c2b3da237ee4d510bfea1215483fb12fa` with
   `evidence_mode=development` and `git.dirty=true`. It is not real-model
   provenance for the current candidate source.

Neither failure can be waived by documentation. Optional Docker/private
platform checks remain honestly `not_verified` and are not the reason for the
binary decision.

## Candidate Identity

| Field | Audited value |
|---|---|
| Package | `edu-agent 0.1.0` |
| Intended release line | R0-R5 public candidate |
| Base Git commit | `a661e3669ca6bce7af6727eb90e18a0698feaade` |
| Candidate commit | unavailable; working tree contains the R5.5 fixes |
| Evidence mode | `development` |
| Git state in current system/Trace reports | `dirty=true`, publication gate not enforced |
| Release tag/image/deployment | none |

## R0-R5 Audit

| Gate | Result | Reproducible evidence | Release interpretation |
|---|---|---|---|
| R0 provenance, frozen dependencies, CI contract, lineage and data boundary | `passed` for implementation; current candidate proof `blocked` | [CI workflow](../.github/workflows/ci.yml), [system report](../artifacts/system-eval.json), [lineage](../artifacts/eval-lineage.json), [data audit](../artifacts/data-boundary-audit.json) | Local development gate passes; no clean current candidate artifact or hosted CI run exists |
| R1 Provider gateway, 429/retry/fallback and route capability checks | `passed` | [architecture](architecture.md), [provider tests](../tests/test_provider_resilience.py), [stream tests](../tests/test_provider_streaming.py) | Fixture and local contract evidence; not a multi-vendor production claim |
| R2 RunEvent, stream cancellation, journal/finalizer and five crash windows | `passed` | [journal design](run-journal.md), [recovery tests](../tests/test_r2_recovery.py), [socket boundary tests](../tests/test_api_sse_cancellation.py) | Process-reopen SQLite recovery is verified; arbitrary third-party process killing is not claimed |
| R3 manifest, parameter governance, concurrency barriers and transactional writes | `passed` | [argument contract](tool-argument-normalization.md), [manifest drift tests](../tests/test_r36_boundaries.py), [batch tests](../tests/test_tool_batch.py), [transaction tests](../tests/test_transactional_tools.py) | Synthetic teaching storage and contract fakes do not prove a private platform integration |
| R4 context overflow, full-tree budget, lifecycle, migration and storage recovery | `passed` | [runtime](production-runtime.md), [context tests](../tests/test_r43_context_recovery.py), [budget tests](../tests/test_run_budget_ledger.py), [lifecycle tests](../tests/test_lifecycle.py), [storage tests](../tests/test_r46_storage_maintenance.py) | Single-host SQLite boundary only |
| R5.1 acceptance/report contract | `passed` in development; candidate run `blocked` | [acceptance script](../scripts/accept_stage8.sh), [evidence checklist](evidence-checklist.md) | Candidate/release reports now write to ignored `ci-artifacts/` so they do not dirty their source commit |
| R5.2 fixed real-model Test | completed development evidence; candidate proof `blocked` | [R5.2 report](../artifacts/r52-real-model-eval.json), [evaluation method](eval.md) | Old dirty evidence cannot establish current candidate model behavior |
| R5.3 container/runbook | static `verified`; runtime `not_verified` | [deployment runbook](production-deployment.md), [container tests](../tests/test_container_deployment.py) | Docker daemon was unavailable; runtime checks remain optional and unverified |
| R5.4 normal/fault demonstration | `passed` | [normal report](../artifacts/r54-demo-normal.json), [fault report](../artifacts/r54-demo-fault.json), [demo script](demo-script.md) | Deterministic local model fixture; separate old R5.2 evidence is not candidate provenance |
| R5.5 final publication decision | `blocked` | this document and [progress ledger](optimization-progress.md) | R5 gate must not be changed to `passed` |

## Report Contract Audit

| Report | Schema and binding | Audited result |
|---|---|---|
| `system-eval.json` | envelope `edu-agent.system-eval.v4`, report contract v5; commit `a661e366...`; config `8e2a020b...a925af2e`; lineage `163e5d23...a68ab43`; development/dirty | offline sections passed; real model `not_run`; sandbox `not_verified`; not candidate provenance |
| `trace-scaling.json` | `edu-agent.trace-scaling.v2`; same commit; config `4ebdc1cb...ebc06c51`; development/dirty | 10,000 indexed and 10,001 exported; 3/3 assertions true; local bounded-read benchmark, not throughput or SLA |
| `eval-lineage.json` | `edu-agent.eval-lineage.v1`; manifest `163e5d23...a68ab43` | 73 deterministic samples: Train 55 / Dev 12 / Test 6; all isolation and provenance checks passed |
| `r52-real-model-eval.json` | `edu-agent.r52-real-model-eval.v1`; commit `d3d3ea7c...`; config `c1fd6550...ccf55`; same lineage; development/dirty | 44 completed provider observations across 6 Test tasks and 3 repeats; valid historical run, invalid current candidate provenance |
| R5.4 reports | `edu-agent.r54-candidate-demo.v1`; fixed seed 314; offline fixture | normal 17/17 and fault 18/18 assertions true; reports explicitly classify R5.2 as development evidence |

R5.2 measured trajectory success `1.0`, tool precision `0.888889`, recall
`1.0`, F1 `0.925926`, and parameter accuracy `0.666667`. It recorded 259,739
tokens and an estimated `$0.107637` using an unverified example price. Provider
billing remains unknown, and the live run did not inject crash/replay faults.

## Verification Results

The final local audit used macOS 26.5.2 / Darwin 25.5.0 arm64, CPython 3.12.13,
SQLite 3.50.4, uv 0.11.16, and the frozen `uv.lock`.

| Check | Result |
|---|---|
| Ruff | full repository, 0 diagnostics |
| Full pytest | 689 passed, 1 skipped |
| Public Stage 8 development gate | exit 0; boundary 34 passed, R4 group 106 passed, R2 group 148 passed, final 689 passed / 1 skipped |
| Focused failure matrix | 73 passed; three loopback tests required execution outside the restricted socket sandbox |
| Normal/fault demo | 17/17 and 18/18 assertions; one exam, operation and approval in each path; no duplicate write |
| Non-empty backup/restore | schema 16; 1 session, run, journal, operation, checkpoint and managed Artifact; backup, verify, restore and verify-state passed; 0 foreign-key violations |
| Trace benchmark | 10,000 indexed / 10,001 exported; 3/3 assertions true |
| Artifact data-boundary audit | 25 files, 0 findings |
| Repository publication scan | 258 intended files: 256 indexed plus this new document and its runner test; no database, key/certificate, cookie, dump or file larger than 1 MiB; secret-pattern hits were synthetic test canaries/placeholders |
| Local Markdown links | all repository-relative targets resolved |
| Container smoke | 11/11 static checks true; all 8 runtime checks `not_verified` because no Docker daemon was present |

The single pytest skip is
`tests/test_container_deployment.py::test_container_smoke_reports_runtime_matrix_without_docker_claims`
with the explicit reason `not_verified: Docker daemon is not available for
container E2E`. The ignored local `edu_agent/data/edu.db` and `dpo_dumps/` are
not tracked and were not modified or deleted by this audit.

The focused matrix covered 429/`Retry-After` and compatible fallback, failure
before/after visible stream delta, socket disconnect and late primary data, all
five process-reopen crash windows, write replay and concurrent idempotency,
manifest drift, bad argument corpus, read/write barriers, provider context
overflow and its one-retry limit, root/child ledger exhaustion, SIGTERM drain,
disk full/read-only/corrupt state, occupied backup targets, and corrupt backup
checksums.

## Reproduction

From the repository root:

```bash
export UV_CACHE_DIR=/tmp/edu-agent-uv-cache
uv lock --check
uv sync --frozen --python 3.12 --managed-python --extra dev --extra mcp
uv pip check
uv run --frozen --offline ruff check .
uv run --frozen --offline python -m pytest -p no:cacheprovider tests -q
zsh scripts/accept_stage8.sh
```

The publishable offline gate must be run only after the fixes are committed and
the tree is clean. Its dynamic reports stay outside the Git index:

```bash
zsh scripts/accept_stage8.sh --evidence-mode candidate
```

Normal and fault demonstrations:

```bash
demo_root=$(mktemp -d /tmp/edu-agent-r54-audit.XXXXXX)
uv run --frozen --offline python scripts/r54_candidate_demo.py \
  --scenario normal --work-dir "$demo_root/normal" --report "$demo_root/normal.json"
uv run --frozen --offline python scripts/r54_candidate_demo.py \
  --scenario fault --work-dir "$demo_root/fault" --report "$demo_root/fault.json"
```

Lineage, Trace, data and container checks:

```bash
uv run --frozen --offline python scripts/audit_eval_lineage.py --quiet
uv run --frozen --offline python scripts/benchmark_trace_scaling.py \
  --events 10000 --page-size 100 --output /tmp/trace-scaling.json
uv run --frozen --offline python scripts/audit_data_boundaries.py \
  --fail-on-findings artifacts
uv run --frozen --offline python scripts/container_smoke.py
```

The real-model candidate command requires a new explicit network/cost/credential
authorization. It fails before reading credentials or making a request when
Git provenance or the output boundary is invalid:

```bash
uv run --frozen python scripts/eval_real_r52.py --repeats 3 \
  --evidence-mode candidate --output ci-artifacts/r52-real-model-eval.json
```

## Configuration, Migration, And Compatibility

- Writes require approval by default. Local code execution is false;
  `code_execution.enabled=false`, provider `disabled`, network policy
  `disabled`, and security attestation false. Knowledge retrieval, Scheduler,
  and OTLP export are opt-in. Credentials are environment references, never
  config values.
- State schema is 16 with idempotent `016_run_replay_scope`. Migration from an
  older v14 snapshot, interruption rollback/restart, retained legacy messages,
  and rejection of future schema versions have regression coverage.
- The legacy `base_url` model configuration and the thin OpenAI-compatible
  facade remain tested. CI is fixed to Python 3.12; package metadata still
  declares `>=3.10` compatibility.
- Restore never overwrites live state. It validates the manifest, hashes,
  schema, SQLite integrity, foreign keys and managed Artifact paths before
  publishing into a new or empty directory.

## Residual Risks And `not_verified`

Required blockers:

- no clean current candidate Stage 8 provenance;
- no real-model candidate report bound to that same clean commit.

Optional or external-state items that remain explicit:

- Docker image build and all 8 container runtime checks: non-root inspect,
  private-file exclusion, read-only rootfs, volume persistence, restart,
  SIGTERM drain, in-container backup/restore and HTTP smoke;
- Docker/Jobe code-execution E2E on this machine;
- GitHub-hosted Actions execution for the current unpushed changes;
- private `TeachingPlatformProvider`, private platform authentication and data;
- actual model-provider billing, live-model recovery fault injection and other
  Provider/model routes;
- production gateway/TLS/identity integration; `DemoTokenAuth` is not a
  production authenticator;
- cross-host/NFS/network-partition SQLite consensus and force-killing arbitrary
  blocking third-party SDK calls, which are outside the supported contract.

## Explicit Non-goals

R5.5 does not implement L1 real-platform access, L2 Memory/Skill, L3 Curator,
frontend workbench, Kubernetes, multi-host storage, a credential pool, external
data downloads, or a broader Provider matrix. After R5 eventually passes,
choose exactly one of L1/L2/L3 in response to a real need; do not start all
three in parallel.

## Rollback

No release was deployed, so the immediate rollback is to stop promotion and
retain the current evidence for review.

For a later trial deployment:

1. Drain and stop the API; create and verify a backup before changing code or
   storage.
2. Start the previous reviewed image only if it supports live schema 16.
3. If older code cannot read schema 16, restore the pre-upgrade verified backup
   into a new directory/volume, run `verify-state`, and cut over only after
   health and API smoke pass.
4. Keep the old state volume and backup. Do not downgrade in place, overwrite
   the live database, force-push history, or delete recovery evidence.

The detailed operator sequence is in
[production-deployment.md](production-deployment.md) and
[storage-operations.md](storage-operations.md).

## Interview Boundary

Safe to say:

- the listed Runtime mechanisms are implemented and verified by offline/local
  tests and the development Stage 8 gate;
- the R5.4 demonstration exercises production Runtime paths with deterministic
  local model and teaching fixtures;
- one old R5.2 DashScope run completed with the exact limited metrics above;
- container hardening is statically verified while runtime remains
  `not_verified`.

Do not say:

- this source is release-ready or has passed candidate/release provenance;
- the current candidate model behavior is verified by the old R5.2 report;
- there are online users, production throughput, production SLA, verified
  provider billing, private-platform integration, container deployment
  acceptance, cross-host exactly-once, or SQLite consensus;
- R5.4 latency or the 10k Trace observation is a production capacity claim.

## Minimum Next Step

Create one reviewed local commit containing the R5.5 fixes, leaving the tree
clean. On that exact commit, run Stage 8 in candidate mode. With separate user
authorization and credentials, run the fixed R5.2 Test in candidate mode to
`ci-artifacts/`, then audit that directory and require both reports to carry the
same clean commit. Only then may R5.5 and the R5 gate be changed to `passed`.
