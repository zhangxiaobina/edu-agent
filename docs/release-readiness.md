# R5.5 Release Readiness

## Binary Decision

**READY. R5.5 and the R5 gate passed for the audited candidate.**

The intended package version is `0.1.0`. The audited candidate commit is
`fb1eeb6073694409f0c2c48ef34916f420e9fdab` on `main`. Before and after both
candidate runs, `HEAD` and `origin/main` matched that commit, divergence was
`0/0`, and the worktree was clean.

Both required publication proofs now exist in the ignored local
`ci-artifacts/` directory:

1. The complete Stage 8 gate passed with `--evidence-mode candidate`.
2. The fixed R5.2 real-model Test passed with three repeats in candidate mode.

Both reports record the same clean commit, `evidence_mode=candidate`, and a
passed provenance gate. The final seven-file data-boundary audit found no
credential or identifiable-data leakage. No image was published, no service
was deployed, and no release or tag was created.

This document update happened after the evidence was produced and is recorded
as a separate documentation-only commit. It records the result; it is not part
of the audited candidate commit, is not a replacement candidate, and must not
be used to rewrite the audited provenance.

## Candidate Identity

| Field | Audited value |
|---|---|
| Package | `edu-agent 0.1.0` |
| Intended release line | R0-R5 public candidate |
| Branch | `main` |
| Candidate commit | `fb1eeb6073694409f0c2c48ef34916f420e9fdab` |
| Upstream | `origin/main`, divergence `0/0` during both runs |
| Evidence mode | `candidate` |
| Git state in candidate reports | available, `dirty=false`, provenance `passed` |
| Release tag/image/deployment | none |

## R0-R5 Audit

| Gate | Result | Evidence boundary |
|---|---|---|
| R0 provenance, frozen dependencies, CI contract, lineage and data boundary | `passed` | Candidate reports use real clean Git provenance; lineage and final artifact audit passed |
| R1 Provider gateway, retry/fallback and route capability checks | `passed` | Offline contract and fault tests; not a multi-vendor production claim |
| R2 RunEvent, stream cancellation, journal/finalizer and crash recovery | `passed` | Process-reopen SQLite recovery and fencing verified |
| R3 manifest, parameter governance, concurrency and transactional writes | `passed` | Frozen ToolManifest, validation, barriers and operation/outbox tests passed |
| R4 context, budget, lifecycle, migration and storage recovery | `passed` | State schema 16, bounded drain, backup/restore and GC verified |
| R5.1 acceptance/report contract | `passed` | Stage 8 candidate exit 0 and coverage checklist passed |
| R5.2 fixed real-model Test | `passed` | Candidate report has 18/18 successful task-runs and 45/45 completed observations |
| R5.3 container/runbook | static `verified`; runtime `not_verified` | Static checks are not represented as container runtime acceptance |
| R5.4 normal/fault demonstration | `passed` | Deterministic local normal and recovery scenarios passed |
| R5.5 final publication decision | `passed` | Both required candidate provenance records match the same clean commit |

## Candidate Evidence Audit

| Report | Schema and binding | Audited result |
|---|---|---|
| `ci-artifacts/system-eval.json` | envelope `edu-agent.system-eval.v4`, report contract v5; config `8e2a020b...a925af2e` | candidate, clean commit, provenance passed; all required offline sections passed |
| `ci-artifacts/trace-scaling.json` | `edu-agent.trace-scaling.v2`; config `4ebdc1cb...ebc06c51` | candidate, same clean commit; 10,000 indexed / 10,001 exported; 3/3 assertions true |
| `ci-artifacts/eval-lineage.json` | `edu-agent.eval-lineage.v1`; manifest `163e5d23...a68ab43` | 73 deterministic samples: Train 55 / Dev 12 / Test 6; all checks passed |
| `ci-artifacts/evidence-checklist.json` | `edu-agent.acceptance-coverage.v1` | Stage 8 and all four mapped regression groups passed |
| `ci-artifacts/r52-real-model-eval.json` | `edu-agent.r52-real-model-eval.v1`; config `381d9b52...014d7fd` | candidate, same clean commit; real model verified |
| `ci-artifacts/r52-real-model-eval.raw.jsonl` | redacted JSONL, 45 records | 45 completed, 0 failed; 6 Test tasks x 3 repeats |
| `ci-artifacts/data-boundary-audit.json` | `edu-agent.data-boundary-audit.v1` | seven files scanned, zero findings |

The system, Trace and real-model config hashes were independently rebuilt and
matched their reports. Twenty-two Stage 8 input hash declarations and seven
R5.2 input hashes matched the candidate source. The shared lineage manifest,
schema and split counts matched across Stage 8 and R5.2. The teacher
ToolManifest rebuilt as schema `edu-agent.tool-manifest.v1`, hash
`d9391fb50910f015ae86c0f754fb46f4623bf3166c5c506b9d5541d8ec5d263f`,
with 14 entries, 11 parallel-safe entries and 3 barriers.

## Verification Results

The candidate audit used macOS 26.5.2 / Darwin 25.5.0 arm64, CPython 3.12.13,
SQLite 3.50.4, uv 0.11.16, and the frozen `uv.lock`.

| Check | Result |
|---|---|
| Public Stage 8 candidate gate | exit 0 |
| Ruff | all scoped invocations passed, 0 diagnostics |
| Stage 8 boundary tests | 34 passed |
| R4 context/storage group | 106 passed |
| R2 recovery group | 148 passed |
| Stage 7 observability group | 12 passed |
| Full pytest | 689 passed, 1 skipped |
| Context fidelity | 12 cases; thresholds passed; scope leak 0 |
| Trace benchmark | 10,000 indexed / 10,001 exported; 3/3 assertions true |
| State maintenance | schema 16; backup, verify, restore and verify-state passed |
| Real-model candidate | exit 0; 18/18 task-runs; 45/45 observations; 0 failed traces |
| Final data-boundary audit | 7 files, 0 findings |
| Exact credential and generic secret scan | 0 matches |
| Container smoke | 11/11 static checks true; 8/8 runtime checks remain `not_verified` |

The one pytest skip is the Docker runtime matrix test, with the explicit reason
that the Docker daemon is unavailable. It does not become a runtime pass.

## Real-Model Result

The fixed route was DashScope `qwen-plus` in `chat_completions` mode, Test split
only, temperature `0.0`, no transmitted seed, maximum output 8,192 tokens and
three repeats. The report records:

- trajectory success: `1.0` in all three repeats;
- tool precision mean: `0.916667`;
- tool recall mean: `1.0`;
- tool F1 mean: `0.944444`;
- parameter accuracy mean: `0.703704`;
- step completion mean: `1.0` and early termination mean: `0.0`;
- 267,355 input, 4,812 output and 272,167 total tokens;
- estimated cost `$0.112739` using an unverified example price.

Provider billing remains unknown. The normal Test run did not inject
crash/replay faults, so `recovery_safety=not_exercised`; recovery continues to
be supported by separate offline fault tests. The model result is not evidence
for other providers, production traffic or real student data.

## Configuration, Migration, And Compatibility

- Writes require approval by default. Local code execution, Scheduler,
  knowledge retrieval and OTLP export remain opt-in or disabled by default.
- State schema is 16 with idempotent `016_run_replay_scope`; older snapshot
  migration, interruption recovery and future-schema rejection are covered.
- Runtime schemas remain consistent: checkpoint 2, budget ledger 1, run journal
  1, runtime event v1 and run event v2.
- Restore validates manifest and payload hashes, schema, SQLite integrity,
  foreign keys and managed Artifact paths before publishing to a new target.

## Residual Risks And `not_verified`

There are no remaining required R5 blockers. These optional, external or
out-of-contract items remain explicit:

- Docker image runtime and all eight container checks: non-root inspection,
  private-file exclusion, read-only rootfs, volume persistence, restart,
  SIGTERM drain, in-container backup/restore and HTTP smoke;
- Docker/Jobe code-execution E2E;
- GitHub-hosted execution of this exact candidate;
- private `TeachingPlatformProvider`, private authentication and private data;
- actual provider billing, live-model recovery fault injection and other model
  routes;
- production gateway/TLS/identity integration;
- cross-host/NFS/network-partition SQLite consensus and force-killing arbitrary
  blocking third-party SDK calls, which are outside the supported contract.

## Explicit Non-goals

R5.5 does not implement L1 real-platform access, L2 Memory/Skill, L3 Curator,
frontend workbench, Kubernetes, multi-host storage, a credential pool, external
data downloads or a broad provider matrix. A later stage must select exactly
one of L1/L2/L3 for a concrete need; it must not start all three by default.

## Publication Boundary

Safe to say:

- R0-R5 and the R5.5 candidate evidence gate passed for commit `fb1eeb6...`;
- the fixed real-model Test completed with the exact metrics above;
- container hardening is statically verified while runtime remains
  `not_verified`.

Do not say:

- an image, deployment, release or tag exists;
- the model result proves production quality, throughput, SLA, verified billing
  or private-platform integration;
- container runtime acceptance, cross-host exactly-once or SQLite consensus was
  verified;
- R5.4 latency or the 10k Trace observation is a production-capacity claim.

## Next Step

Revoke the one-time evaluation credential and retain the ignored candidate
evidence in an approved secure evidence store if it must survive local cleanup.
Do not enter L1/L2/L3 until a concrete requirement selects exactly one path.

If this documentation commit or any later commit is selected as a replacement
release candidate, rerun both required candidate provenance gates on that new
clean commit before promoting it.
