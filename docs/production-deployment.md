# R5.3 API Container And Minimal Operations

## Threat And Runtime Boundary

This deployment is a single EduAgent API process with one local SQLite state
file and one Artifact directory. The SQLite lease/fencing model coordinates
workers that share that file on one host only; it is not a multi-host, NFS,
replication, or network-partition protocol. The API image does not contain a
database snapshot, student/private data, DPO dump, test cache, Git metadata,
environment file, or credential.

The container trust boundary is the operator's host, the mounted configuration,
the two secret files, the state/artifact/backup mounts, and the configured
external model endpoint. The API is bound to loopback by Compose. Exposing it
through a gateway, TLS terminator, or an authenticated network is an operator
decision; the repository's `DemoTokenAuth` is a local/demo authenticator and is
not a substitute for a production identity gateway.

Code execution is disabled in the supplied configuration. The API service has
no Docker socket, host PID/network namespace, privileged flag, or arbitrary
mount. Jobe/Docker execution is a separate, opt-in deployment surface under
`deploy/code-execution/`; enabling it requires a reviewed isolated backend and
the existing provider security attestation. No Kubernetes, multi-host database,
or platform Provider is included in R5.3.

External model outages, timeouts, rate limits, and breaker transitions are
request/provider failures handled by the existing retry/fallback runtime. They
do not make the process die or make liveness fail. Readiness is fail-closed for
startup/migration, SQLite integrity/write access, audit writes, and any enabled
local isolated code-execution Provider. During drain, liveness stays successful,
readiness fails, and new chat/scheduler claims receive `503 PROCESS_NOT_READY`.

## Files And Image Contract

- `deploy/api/Dockerfile` is a multi-stage Python 3.12 build with pinned Python and UV registry digests. `uv sync --frozen --no-dev` installs only lockfile-resolved runtime dependencies; the final stage runs as UID/GID `10001`.
- `.dockerignore` excludes `.git`, `.env*`, key/certificate files, databases/WAL/SHM, `dpo_dumps`, generated artifacts, tests, and caches. The Dockerfile copies only the package and API/preflight scripts.
- `deploy/api/entrypoint.sh` reads operator-mounted secret files, runs the no-model `container_preflight.py`, then `exec`s `scripts/api_server.py` so SIGTERM reaches the existing lifecycle shutdown path.
- `deploy/docker-compose.yml` contains only `api`. It uses a named state/artifact volume, an explicit backup bind mount, a read-only root filesystem, a 64 MiB `tmpfs` for transient files, dropped capabilities, `no-new-privileges`, loopback port binding, restart policy, healthcheck, and a 40 second stop grace period.

The default Python input is
`python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7`,
and the UV input is
`ghcr.io/astral-sh/uv:0.11.16@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d`.
The lockfile remains the dependency source of truth. Docker is not available
in the current environment, so image build and container E2E evidence for this
handoff are `not_verified`.

## Host Preparation

Use explicit operator paths; do not use the repository checkout as a state or
secret directory:

```bash
sudo install -d -m 0750 /srv/edu-agent/config /srv/edu-agent/secrets /srv/edu-agent/backups
sudo install -d -m 0700 /srv/edu-agent/secrets
sudo install -m 0644 /srv/edu-agent/repo/deploy/api/config.container.example.toml \
  /srv/edu-agent/config/edu-agent.toml
sudo install -m 0600 /dev/null /srv/edu-agent/secrets/demo-token
sudo install -m 0600 /dev/null /srv/edu-agent/secrets/provider-api-key
sudo chown -R 10001:10001 /srv/edu-agent/backups
```

Populate the two secret files through the host secret manager or a protected
operator session. Keep the files non-empty, mode `0600`, and outside Git. The
config file may contain an endpoint/model route and runtime policy, but never a
credential value. Set `storage.state_path` to
`/var/lib/edu-agent/state/state.db` and `storage.artifact_path` to
`/var/lib/edu-agent/artifacts` as in the example.

## Build And Start

From the checked-out release tree, set the absolute paths consumed by Compose:

```bash
export EDU_AGENT_CONFIG_FILE=/srv/edu-agent/config/edu-agent.toml
export EDU_AGENT_DEMO_TOKEN_FILE=/srv/edu-agent/secrets/demo-token
export EDU_AGENT_API_KEY_FILE=/srv/edu-agent/secrets/provider-api-key
export EDU_AGENT_BACKUP_DIR=/srv/edu-agent/backups
export EDU_AGENT_API_PORT=8080
export EDU_AGENT_PYTHON_IMAGE='python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7'
export EDU_AGENT_UV_IMAGE='ghcr.io/astral-sh/uv:0.11.16@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d'

docker compose -f /srv/edu-agent/repo/deploy/docker-compose.yml build api
docker compose -f /srv/edu-agent/repo/deploy/docker-compose.yml up -d api
```

These are the reviewed public multi-architecture manifest digests used by the
example. Replace them only as part of a dependency/image review; they are not
credentials.
The first start runs migrations and a rollback-only write probe before the API
can become ready. A migration, integrity, read-only, or volume permission
failure exits the container and is visible as an unhealthy/restarting service.

## Health, Logs, And Trace

The probes require no authentication and return only aggregate lifecycle/check
state:

```bash
curl --fail --silent http://127.0.0.1:8080/health/live
curl --fail --silent http://127.0.0.1:8080/health/ready
curl --fail --silent http://127.0.0.1:8080/openapi.json >/tmp/edu-agent-openapi.json
docker compose -f /srv/edu-agent/repo/deploy/docker-compose.yml ps
docker compose -f /srv/edu-agent/repo/deploy/docker-compose.yml logs --since=10m api
```

`/health/live` is `200` for `starting`, `running`, and `draining`, and fails
only after `stopped`. `/health/ready` is `200` only for `running` with current
migrations, writable SQLite, audit writes, and required local providers. A
temporary external model failure does not change those process probes.

Trace data is owner/tenant scoped and centrally redacted before export. Use the
authenticated API (`/v1/traces` or `/v1/traces/export`) through the deployment's
trusted gateway; do not scrape SQLite or copy Artifact paths into logs. Provider
responses and credentials are not included in health responses.

## Backup And Restore

The backup target must be a new directory below the explicit backup mount. The
SQLite backup uses the official backup API and copies only indexed, hash-checked
Artifacts:

```bash
backup_id=2026-08-25T120000Z
docker compose -f /srv/edu-agent/repo/deploy/docker-compose.yml exec -T api \
  python /app/scripts/state_maintenance.py backup \
  --state /var/lib/edu-agent/state/state.db \
  --artifacts /var/lib/edu-agent/artifacts \
  --target "/var/lib/edu-agent-backups/${backup_id}"
docker compose -f /srv/edu-agent/repo/deploy/docker-compose.yml exec -T api \
  python /app/scripts/state_maintenance.py verify-backup \
  --backup "/var/lib/edu-agent-backups/${backup_id}"
```

For a restore, drain/stop the API, restore into a new empty directory, verify
it, and cut over by changing the state volume/config only after the verification
passes. Never overwrite the live database:

```bash
docker compose -f /srv/edu-agent/repo/deploy/docker-compose.yml stop -t 40 api
docker compose -f /srv/edu-agent/repo/deploy/docker-compose.yml run --rm --no-deps \
  --entrypoint python api /app/scripts/state_maintenance.py restore \
  --backup "/var/lib/edu-agent-backups/${backup_id}" \
  --target-dir /var/lib/edu-agent/restore-2026-08-25T120000Z
docker compose -f /srv/edu-agent/repo/deploy/docker-compose.yml run --rm --no-deps \
  --entrypoint python api /app/scripts/state_maintenance.py verify-state \
  --state /var/lib/edu-agent/restore-2026-08-25T120000Z/state.db \
  --artifacts /var/lib/edu-agent/restore-2026-08-25T120000Z/artifacts
```

The restore command refuses a newer schema, an occupied target, bad hashes,
integrity failures, or unsafe Artifact paths. Keep the original volume until
the restored service has passed health and API smoke.

## Upgrade, Rollback, And Drain

Build the new image beside the current one, drain/stop the old process, then
run the exact preflight against the persistent volume before starting the new
image. This avoids concurrent migration writers:

```bash
docker compose -f /srv/edu-agent/repo/deploy/docker-compose.yml build api
docker compose -f /srv/edu-agent/repo/deploy/docker-compose.yml stop -t 40 api
docker compose -f /srv/edu-agent/repo/deploy/docker-compose.yml run --rm --no-deps \
  --entrypoint python api /app/scripts/container_preflight.py
docker compose -f /srv/edu-agent/repo/deploy/docker-compose.yml up -d api
```

Migrations are forward and idempotent. A rollback may use an older image only
when its supported schema is at least the live schema; the application refuses
to downgrade a newer database. For a schema rollback, restore a verified backup
to a new volume/directory and cut over after verification. Do not delete the
old volume as part of an image rollback.

`docker compose stop -t 40 api` (or a normal container SIGTERM) first moves the
process to `draining`, rejects new chat/scheduler claims, requests cooperative
cancellation, flushes state, persists recovery advice for unfinished runs, and
then reaches `stopped` within the configured deadline. The 40 second Compose
grace period leaves headroom above the default 30 second lifecycle deadline.

## Capacity And Provider Faults

Check both the host backup filesystem and the mounted state volume; alert well
before exhaustion because SQLite maps full/read-only faults to `503` and fails
readiness closed:

```bash
df -P /srv/edu-agent/backups
docker compose -f /srv/edu-agent/repo/deploy/docker-compose.yml exec -T api \
  df -P /var/lib/edu-agent
docker system df
```

If the external model returns rate limits/timeouts, inspect redacted API errors
and Provider Trace events, verify route/credential configuration, and allow the
existing retry/breaker/fallback policy to work. Do not restart the process as a
health workaround. Authentication or endpoint configuration errors require a
config/secret correction and a controlled restart. If the optional local code
execution backend is enabled and unhealthy, readiness correctly remains false;
repair or disable that backend rather than mounting a Docker socket into the
API container.

## Verification Status

Static checks cover the Dockerfile stages, lockfile install, UID/GID, ignored
inputs, Compose hardening, explicit volumes, secret-file contract, preflight,
healthcheck, restart, and SIGTERM grace settings. The repository environment
does not provide a Docker daemon, so build, non-root inspection, read-only
rootfs, real volume persistence, restart, SIGTERM drain, backup/restore inside
Docker, and HTTP container smoke remain `not_verified`; the handoff must not
claim deployment acceptance for those items.
