# Candidate Evidence Checklist

This checklist is the publication map for README claims. A claim is only
publishable when its source, focused regression tests, and the corresponding
field in `artifacts/system-eval.json` are all present. `passed` means the
offline contract was exercised; it does not turn an oracle, fake, or planned
integration into real-provider evidence.

| README claim | Source | Focused tests | Report field | Boundary |
| --- | --- | --- | --- | --- |
| Agent and Plan enforce ordered tool-backed completion | [`edu_agent/agent/graph.py`](../edu_agent/agent/graph.py), [`edu_agent/planning`](../edu_agent/planning) | [`tests/test_agent.py`](../tests/test_agent.py), [`tests/test_plan_runtime.py`](../tests/test_plan_runtime.py), [`tests/test_eval.py`](../tests/test_eval.py) | `sections.agent_plan` | Oracle is `harness_only`; Stage 8 does not call a real model and keeps its own `evaluation.real_model.status=not_run` |
| Provider routes, adapters, retry and fallback are explicit | [`edu_agent/engine/gateway.py`](../edu_agent/engine/gateway.py), [`edu_agent/engine/resilient.py`](../edu_agent/engine/resilient.py) | [`tests/test_provider_gateway.py`](../tests/test_provider_gateway.py), [`tests/test_provider_adapter_contract.py`](../tests/test_provider_adapter_contract.py), [`tests/test_provider_resilience.py`](../tests/test_provider_resilience.py), [`tests/test_r1_fake_provider_acceptance.py`](../tests/test_r1_fake_provider_acceptance.py) | `sections.provider_route_retry` | Fixtures/fault injection do not prove a live vendor route |
| Stream and cancellation boundaries reject late work | [`edu_agent/engine/streaming.py`](../edu_agent/engine/streaming.py), [`edu_agent/runtime/cancellation.py`](../edu_agent/runtime/cancellation.py), [`edu_agent/api.py`](../edu_agent/api.py) | [`tests/test_provider_streaming.py`](../tests/test_provider_streaming.py), [`tests/test_cancellation.py`](../tests/test_cancellation.py), [`tests/test_api_sse_cancellation.py`](../tests/test_api_sse_cancellation.py) | `sections.stream_cancel` | Socket tests are local contract evidence, not public network capacity evidence |
| Journal, finalizer, lease and crash recovery are durable | [`edu_agent/state/journal.py`](../edu_agent/state/journal.py), [`edu_agent/state/turn_finalizer.py`](../edu_agent/state/turn_finalizer.py), [`edu_agent/runtime/recovery.py`](../edu_agent/runtime/recovery.py) | [`tests/test_run_journal.py`](../tests/test_run_journal.py), [`tests/test_turn_finalizer.py`](../tests/test_turn_finalizer.py), [`tests/test_r2_recovery.py`](../tests/test_r2_recovery.py), [`tests/test_stage8_boundaries_recovery_trace.py`](../tests/test_stage8_boundaries_recovery_trace.py) | `sections.journal_recovery` | SQLite/shared-file recovery is not cross-host consensus |
| ToolManifest freezes metadata and safe concurrency barriers | [`edu_agent/tools/manifest.py`](../edu_agent/tools/manifest.py), [`edu_agent/runtime/tool_batch.py`](../edu_agent/runtime/tool_batch.py) | [`tests/test_tool_manifest.py`](../tests/test_tool_manifest.py), [`tests/test_r36_boundaries.py`](../tests/test_r36_boundaries.py), [`tests/test_tool_batch.py`](../tests/test_tool_batch.py), [`tests/test_multi_agent_delegation.py`](../tests/test_multi_agent_delegation.py) | `sections.tool_manifest_concurrency` | Parallel execution is limited to declared read-only segments |
| Teaching provider and argument contracts are fail-closed | [`edu_agent/teaching`](../edu_agent/teaching), [`edu_agent/runtime/tool_arguments.py`](../edu_agent/runtime/tool_arguments.py) | [`tests/test_teaching_provider_contract.py`](../tests/test_teaching_provider_contract.py), [`tests/test_tool_arguments.py`](../tests/test_tool_arguments.py), [`tests/test_builtin_tool_contract_matrix.py`](../tests/test_builtin_tool_contract_matrix.py), [`tests/test_mcp.py`](../tests/test_mcp.py) | `sections.tool_manifest_concurrency`, `config.tool_manifest_schema_version` | Synthetic/fake provider contracts are not a private platform integration |
| Context compression preserves scope and citations | [`edu_agent/runtime/context_engine.py`](../edu_agent/runtime/context_engine.py), [`edu_agent/eval/context_fidelity.py`](../edu_agent/eval/context_fidelity.py) | [`tests/test_context_accounting.py`](../tests/test_context_accounting.py), [`tests/test_context_checkpoint.py`](../tests/test_context_checkpoint.py), [`tests/test_context_fidelity.py`](../tests/test_context_fidelity.py), [`tests/test_r43_context_recovery.py`](../tests/test_r43_context_recovery.py) | `sections.context` | Deterministic estimator is a proxy, not a vendor tokenizer or bill |
| Budget and cost ledgers are durable and replay-safe | [`edu_agent/runtime/budget.py`](../edu_agent/runtime/budget.py) | [`tests/test_run_budget_ledger.py`](../tests/test_run_budget_ledger.py) | `sections.budget` | Offline pricing is `unpriced@...`; real cost remains unknown until a real run |
| Transactional writes, outbox and compensation are idempotent | [`edu_agent/runtime/transactions.py`](../edu_agent/runtime/transactions.py), [`edu_agent/teaching`](../edu_agent/teaching) | [`tests/test_transactional_tools.py`](../tests/test_transactional_tools.py) | `sections.transaction` | Synthetic SQLite command provider is not a production teaching platform |
| Code execution is isolated when Docker/Jobe is available | [`edu_agent/code_execution`](../edu_agent/code_execution), [`scripts/code_sandbox_demo.py`](../scripts/code_sandbox_demo.py) | [`tests/test_code_execution.py`](../tests/test_code_execution.py), `scripts/code_sandbox_demo.py --provider docker --e2e --require-all` | `sections.sandbox` | Docker unavailable is `not_verified`; no fake result may be promoted |
| Runtime and 10k Trace paging are bounded | [`edu_agent/observability/trace.py`](../edu_agent/observability/trace.py), [`scripts/benchmark_trace_scaling.py`](../scripts/benchmark_trace_scaling.py) | [`tests/test_stage8_boundaries_recovery_trace.py`](../tests/test_stage8_boundaries_recovery_trace.py) | `sections.performance`, `trace_scaling` | Local synthetic benchmark is not a production capacity claim |
| Provenance and data boundaries are publication gates | [`edu_agent/eval/provenance.py`](../edu_agent/eval/provenance.py), [`edu_agent/data_audit.py`](../edu_agent/data_audit.py), [`scripts/audit_acceptance_coverage.py`](../scripts/audit_acceptance_coverage.py) | [`tests/test_ci_provenance.py`](../tests/test_ci_provenance.py), [`tests/test_eval_lineage.py`](../tests/test_eval_lineage.py), [`tests/test_acceptance_scripts.py`](../tests/test_acceptance_scripts.py) | `sections.provenance`, `sections.data_boundary`, `report_schema`, `acceptance` | Candidate/release rejects unavailable or dirty Git, lineage leakage, and required offline `not_run` |

The only public aggregate entrypoint is `zsh scripts/accept_stage8.sh`. It
produces the redacted report and `evidence-checklist.json`; Stage 7 and R2 are
internal calls on that path. A missing test file is never sufficient evidence:
the coverage audit also checks that the highest-stage call graph reaches the
full suite and that expensive lineage, Trace, system-eval, and audit steps are
not duplicated.

## Independent Verification Boundaries

- Docker/Jobe requires the pinned image, daemon, and backend health checks. A
  local machine without them records `not_verified`.
- Real model evaluation requires a separately authorized R5.2 route, frozen
  Test lineage, cost limit, redacted raw evidence, and clean candidate/release
  provenance. The saved R5.2 run proves requests completed, but its
  `development/dirty` provenance does not prove the current candidate commit.
- A private teaching platform requires an independently implemented and
  authenticated provider contract. Synthetic SQLite and contract fakes only
  verify the boundary and transaction policy.
