# Axiom Zeta Sentinel Sovereign

> **License: GNU AGPL-3.0.** This project is free software under the GNU Affero General Public License v3.0 — see [LICENSE](LICENSE).

> **Sept 2026 revision:** the engine now builds, is deterministic, and compares each window against a learned reference baseline; authentication fails closed. See [CHANGES.md](CHANGES.md) for details and open items.

An API gateway middleware that helps reduce LLM/data drift using a full HRM manifold surgery runtime to stabilize noisy metric streams into a bounded 24-D representation.

## Product Shape

### 1) Core value proposition

- Input: model outputs, embeddings, metadata, and metric streams.
- Engine: 24-D mesh axiom-zeta-sentinel with topology-aware stabilization in Rust.
- Output: bounded corrected signal + drift score + risk/action policy.
- Outcome: fewer hallucination cascades, lower retraining frequency, and lower infra spend.

### 2) Commercial API contract

- `POST /v1/stabilize`
- `POST /v1/feedback`
- `POST /v1/baseline/reset`
- `GET /v1/health`
- `GET /v1/telemetry` (JSON or Prometheus text with `?format=prometheus`)

Team Edition collaboration APIs:

- `POST /v1/team/orgs`
- `POST /v1/team/orgs/{org_id}/members`
- `GET /v1/team/orgs/{org_id}/members`
- `POST /v1/team/orgs/{org_id}/workspaces`
- `POST /v1/team/workspaces/{workspace_id}/api-keys`
- `GET /v1/team/workspaces/{workspace_id}/api-keys`
- `DELETE /v1/team/workspaces/{workspace_id}/api-keys/{key_id}`
- `GET /v1/team/orgs/{org_id}/audit`

Team Edition request headers:

- `X-User-Id` + `X-User-Signature`: signed identity for team workflows (see CHANGES.md)
- `X-API-Key`: gateway admin key (`GATEWAY_API_KEY`, required; the gateway refuses requests if it is unset)
- `X-Workspace-Key-Id` + `X-Workspace-Key`: workspace credential for `/v1/stabilize`, `/v1/feedback`, `/v1/baseline/reset`

Team roles and permissions:

- `owner`: full org and workspace control
- `admin`: workspace + member management
- `analyst`: read access for team operations
- `viewer`: read-only access

Stabilize response includes:

- `stabilized_vector[24]`
- `drift_score`
- `risk_level` (`green`, `yellow`, `red`)
- `actions` (`pass`, `clamp`, `retry`, `fallback-model`, `human-review`)
- internal diagnostics over time: energy/stress EMA, drift EMA/trend, topology volatility, surgery rate

### 3) Architecture

- Rust crate (`axiom_zeta_sentinel_core`): deterministic HRM manifold runtime (mesh, Laplacian stress evolution, topology surgery), bounded output, high-throughput hot path.
- Python FastAPI gateway: auth, policy, tenant isolation, observability, integration points.
- PyO3/maturin bridge: Python invokes Rust core with near-C performance.
- Optional stream integration points: Redis/Kafka adapters can be added in gateway layer.

## How Engine Concepts Map To Product Components

- `SimplicialHRM` -> `DriftEngine`
- `state_vector (24)` -> stabilized trust vector
- `energy/genus/euler/betti` -> drift health diagnostics
- topology surgery style behavior -> adaptive correction operators
- symbolic/resonant forcing -> context + policy forcing terms

## Repository Layout

```text
gateway/
  main.py
  api/routes_stabilize.py
  core/{auth.py,config.py,policy.py,replay.py,schemas.py,service.py,telemetry.py}
  integrations/stream.py
sdk/
  pyproject.toml
  axiom_zeta_sentinel_sdk/{__init__.py,client.py,types.py}
examples/
  langchain_llamaindex_integration.py
notebooks/
  rag_benchmark_before_after.ipynb
rust/
  Cargo.toml
  pyproject.toml
  src/{lib.rs,engine.rs}
helm/axiom-zeta-sentinel-sovereign/
  Chart.yaml
  values.yaml
  templates/{deployment.yaml,service.yaml}
Dockerfile
```

## Quickstart

### 1) Build and install Rust extension

```bash
cd rust
python -m pip install -U pip maturin
maturin develop --release
cd ..
```

### 2) Install Python dependencies

```bash
python -m pip install -r requirements-dev.txt
python -m pip install -e .
```

### 3) Run API

```bash
uvicorn gateway.main:app --reload --port 8000
# or
axiom-zeta-sentinel-sovereign
```

Auth (required; the gateway refuses requests without it unless ALLOW_INSECURE_NO_AUTH=true):

```bash
export GATEWAY_API_KEY="replace-me"
```

Create local env file for compose/runtime settings:

```bash
cp .env.example .env
```

## Example request

```bash
curl -X POST "http://127.0.0.1:8000/v1/stabilize" \
  -H "X-API-Key: replace-me" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id":"acme",
    "request_id":"req-001",
    "metric_window":[0.1,0.2,0.12,0.25,0.4,0.33,0.29,0.31,0.28,0.35,0.37,0.34,0.3,0.27,0.26,0.24,0.22,0.2,0.19,0.18,0.21,0.23,0.25,0.2],
    "embedding":[0.01,-0.02,0.03,0.04],
    "metadata":{"source":"mvp-test"}
  }'
```

Expected response shape:

```json
{
  "request_id": "req-001",
  "stabilized_vector": [0.0, 0.0, 0.0],
  "drift_score": 0.0,
  "risk_level": "green",
  "actions": ["pass"],
  "diagnostics": {
    "euler_chi": 0.0,
    "genus": 0.0,
    "beta0": 1.0,
    "beta1": 0.0,
    "energy_level": 0.0,
    "stress_level": 0.0,
    "drift_ema": 0.0,
    "drift_trend": 0.0,
    "topology_volatility": 0.0,
    "surgery_rate": 0.0,
    "surgery_total": 0.0,
    "breaker_open": 0.0,
    "spike_streak": 0.0,
    "effective_window": 24.0
  }
}
```

## Feedback and telemetry

```bash
curl -X POST "http://127.0.0.1:8000/v1/feedback" \
  -H "X-API-Key: replace-me" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id":"acme",
    "request_id":"req-001",
    "corrected":true,
    "correction_score":0.92,
    "observed_risk_level":"green"
  }'
```

```bash
curl -H "X-API-Key: replace-me" "http://127.0.0.1:8000/v1/telemetry"
curl -H "X-API-Key: replace-me" "http://127.0.0.1:8000/v1/telemetry?format=prometheus"
```

## Python SDK

Install locally:

```bash
python -m pip install -e ./sdk
```

Use in code:

```python
from axiom_zeta_sentinel_sdk import AxiomZetaSentinelClient

client = AxiomZetaSentinelClient(base_url="http://127.0.0.1:8000", api_key="replace-me")
out = client.stabilize(
  tenant_id="acme",
  request_id="sdk-001",
  text="summarize drift",
  embedding=[0.01, -0.02, 0.04],
  metric_window=[0.2] * 24,
)
print(out["risk_level"], out["actions"])
```

## LangChain/LlamaIndex Integration

- End-to-end example script: `examples/langchain_llamaindex_integration.py`
- Integration point: post-retrieval, pre-generation middleware.
- This keeps the framework dependency optional while demonstrating action routing.

## Local Runtime With Compose

```bash
cp .env.example .env
docker compose up --build
```

Included files:

- `.env.example` for runtime configuration
- `docker-compose.yml` for local gateway orchestration

## Benchmark Notebook (RAG Before/After)

- Notebook: `notebooks/rag_benchmark_before_after.ipynb`
- Measures baseline vs stabilized hallucination proxy over synthetic drift bursts.
- Visualizes drift score and internal HRM diagnostics over time.

Run:

```bash
jupyter notebook notebooks/rag_benchmark_before_after.ipynb
```

## MVP Plan Coverage

- Refactored engine boundary: no plotting/runtime script globals in gateway runtime.
- Stable I/O schema: strict bounded Pydantic models.
- Rust hot path: stabilization dynamics and diagnostics in `axiom_zeta_sentinel_core`.
- Gateway policy layer: risk thresholds + fallback hooks + circuit-breaker escalation.
- Telemetry and SLOs: p95 latency, incidents prevented, false positives.
- Packaging: Docker image and Helm chart for drop-in deployment.

## Product Hardening Implemented

- Determinism mode and seed controls via env settings.
- Per-tenant engine isolation in service runtime.

## Enterprise Rollout Artifacts

- Execution tracker: `docs/enterprise-execution-plan.md`
- Architecture/trust boundaries: `docs/architecture-target-and-trust-boundaries.md`
- NFR/governance baseline: `docs/non-functional-requirements-and-governance.md`
- Dependency map/phased backlog: `docs/dependency-map-and-phased-backlog.md`
- Incident runbook: `docs/operations-incident-runbook.md`
- Change approval matrix: `docs/operations-change-approval-matrix.md`
- Release sign-off template: `docs/operations-release-signoff-template.md`
- Sign-off workflow: `docs/operations-signoff-workflow.md`
- Release-ready operations packet: `docs/operations-packet/`
- Filled staging sign-off example: `docs/operations-packet/staging-signoff-example-2026-07-09.md`
- Filled production sign-off example: `docs/operations-packet/production-signoff-example-2026-07-09.md`
- Operator scaffold: `operator/`
- New runtime controls in `.env.example`:
  - `AUTH_MODE` (`api_key` or `oidc`)
  - `OIDC_*` settings for upstream SSO integration
  - `OIDC_GROUP_ROLE_MAP` for IdP group to app-role mapping
  - `OIDC_AUTHORIZATION_URL`, `OIDC_TOKEN_URL`, `OIDC_CLIENT_ID`, `OIDC_REDIRECT_URI`
  - `OIDC_SESSION_SECRET`, `OIDC_SESSION_TTL_SECONDS`, `OIDC_COOKIE_NAME`
  - `POLICY_MODE` (`builtin` or `opa`)
  - `OPA_URL` and `POLICY_BUNDLE_PATH`
  - `HA_MODE_ENABLED`
  - `AIRGAPPED_MODE` and `ALLOW_OUTBOUND_NETWORK`

OIDC auth endpoints:

- `GET /v1/auth/oidc/login`
- `GET /v1/auth/oidc/callback`
- `GET /v1/auth/session`
- `POST /v1/auth/session/refresh`
- `POST /v1/auth/logout`

Operator and HA automation entrypoints:

- `axiom-zeta-sentinel-operator` (controller runtime)
- `axiom-zeta-sentinel-operator-webhook` (validating + conversion webhook server)
- `bash scripts/ha/run_disruption_suite.sh` (HA disruption suite)
- `bash scripts/airgap/build_offline_bundle.sh` (offline artifact + SBOM bundle)
- `bash scripts/airgap/verify_offline_bundle.sh <bundle_dir>` (provenance verification)
- `python scripts/release/validate_rollout_waves.py docs/release-rollout-waves.json` (staged rollout plan validation)
- `bash scripts/release_governance_checks.sh` (release-governance baseline gate)
- `make governance` / `make governance-dry` (CI/local governance runner)

Release governance reports:

- JSON summary at `dist/release-governance/<timestamp>/summary.json`
- Captured logs and rendered helm manifests in the same report directory

HA state backend controls:

- `STATE_BACKEND` (`memory` or `redis`)
- `STATE_REDIS_URLS` (comma-separated for failover)

Policy hardening controls:

- `POLICY_DEFAULT_DECISION` (`deny` or `allow`)
- `POLICY_DECISION_LOG_PATH` (JSONL decision audit log)
- `POLICY_DECISION_LOG_MAX_BYTES` (log file rotation threshold)
- `POLICY_DECISION_LOG_BACKUP_COUNT` (number of retained rotated logs)

Session/auth hardening controls:

- `OIDC_SESSION_MAX_LIFETIME_SECONDS` (absolute upper bound across refresh cycles)
- `AUTH_AUDIT_LOG_PATH` (JSONL auth audit stream)
- `AUTH_AUDIT_LOG_MAX_BYTES` / `AUTH_AUDIT_LOG_BACKUP_COUNT` (audit log retention)

HA/runtime hardening controls:

- `TENANT_RUNTIME_CACHE_ENABLED` (`false` by default to avoid sticky process-local runtime)

Air-gapped runtime controls:

- `AIRGAP_ALLOWED_HOSTS` for explicit egress exceptions when outbound is disabled

Health endpoint now includes shared state backend health metadata (`state_backend`) to surface dependency failover state.
- Circuit-breakers on repeated drift spikes.
- Replay test harness in `gateway/core/replay.py`.
- Cost controls via step caps and compute budget metadata.

## Deployment

Build container:

```bash
docker build -t axiom-zeta-sentinel-sovereign:latest .
```

Install Helm chart:

```bash
helm install axiom-zeta-sentinel-sovereign ./helm/axiom-zeta-sentinel-sovereign
```

## Notes

- `metric_window` must contain at least 24 values.
- Output vector is always bounded in `[-1, 1]` and length 24.
- Drift score is bounded in `[0, 1]`.
- Diagnostics now include stronger runtime metrics: `energy_level`, `stress_level`, `drift_ema`, `drift_trend`, `topology_volatility`, `surgery_rate`, `surgery_total`.

## GUI

A full single-page GUI is bundled at `gateway/static/index.html` and served by the gateway itself:

- Open `/` or `/ui` after starting the API (e.g. http://127.0.0.1:8000/ui)
- Enter your gateway API key (and workspace/user credentials on Teams/Sovereign), generate a synthetic metric window (stable / mean shift / noise / rescale), run **Stabilize**, and inspect drift score, risk, actions, 24-D stabilized vector and diagnostics.
- Teams adds org/workspace/API-key/audit/analytics panels. Sovereign adds health/OIDC/session/policy status.

No separate frontend build is required.
