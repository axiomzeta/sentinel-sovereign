# Sovereign OIDC + Operator Verification — 2026-10-19

Audit pass on `axiomzeta/sentinel-sovereign` (OIDC auth + Kubernetes operator),
performed from a sandbox with no cluster. This document is blunt about what was
verified, what was fixed, and what remains cluster-only.

## Verified locally (test suite)

| Suite | Before fixes | After fixes |
|---|---|---|
| tests/ (full, `REQUIRE_REAL_ENGINE=1`) | 64 passed, 1 skipped | **69 passed, 1 skipped** |
| test_oidc_auth.py | 4 passed | 5 passed (+audience-fallback test) |
| test_oidc_login_flow.py | 3 failed* | 3 passed |
| test_operator_controller.py | 7 passed | 9 passed (+status-subresource, +secrets/securityContext tests) |
| test_operator_webhook.py | 3 passed | 5 passed (+OIDC-endpoint validation tests) |

\* The 3 login-flow failures were an environment artifact, not a code bug:
PyJWT is a declared dependency (`PyJWT[crypto]` in pyproject.toml and
requirements-dev.txt) and the audit venv initially lacked it. The gateway fails
closed with `500 "OIDC verification is unavailable without PyJWT"` — correct
behavior, unverifiable OIDC without the library.

## Fixed in this pass

1. **Rust engine could not import (real bug).** `rust/src/lib.rs` declared
   `#[pymodule] fn phase_lock_core` while the crate/module is named
   `axiom_zeta_sentinel_core` — Python looked for `PyInit_axiom_zeta_sentinel_core`
   and failed with ImportError. Renamed the module fn. `test_real_engine.py`
   (with `REQUIRE_REAL_ENGINE=1`) now passes against the real maturin build;
   previously the suite only ever exercised the fake engine.
2. **Bearer/id_token audience check was opt-in.** If `OIDC_AUDIENCE` was unset,
   audience was not verified at all, so a token minted by the same IdP for a
   *different* client would be accepted. Now: `audience = OIDC_AUDIENCE or
   OIDC_CLIENT_ID` (OIDC requires id_token `aud` == client_id). Explicit
   `OIDC_AUDIENCE` still overrides for access-token deployments. New unit test
   signs real RS256 tokens and proves wrong-aud rejection / right-aud acceptance.
3. **Operator status writes used the wrong endpoint (real cluster bug).** The
   CRD enables the status subresource, and with it the API server **silently
   drops** `status` on main-resource patches — the reconciler's Ready/Progressing/
   Degraded conditions would never appear in a real cluster (the unit tests
   passed because the fake adapter records patches without API-server semantics).
   `_set_status` now goes through a new `patch_custom_resource_status` adapter
   method that calls `patch_namespaced_custom_object_status`. Finalizer patches
   stay on the main endpoint, as they must.
4. **Operator never injected secrets.** The generated Deployment env set
   AUTH_MODE/OIDC URLs but no `GATEWAY_API_KEY`, `OIDC_CLIENT_SECRET`, or
   `OIDC_SESSION_SECRET` — a spawned gateway fails closed (503) in api_key mode
   and cannot complete OIDC login in oidc mode. Added `spec.auth.secretName`
   (CRD schema + sample): when set, the operator wires those three env vars via
   `secretKeyRef` (optional keys, so partial secrets are allowed).
5. **Webhook validated too little.** `auth.mode: oidc` with no issuer/JWKS was
   admitted, deferring failure to gateway startup. The validating webhook now
   rejects OIDC mode without `oidcIssuer` and `oidcJwksUrl`.
6. **Webhook server had no TLS path.** Kubernetes admission webhooks require
   HTTPS; `run_webhook` ran plain HTTP only. It now honors
   `AXIOM_ZETA_SENTINEL_WEBHOOK_TLS_CERT` / `AXIOM_ZETA_SENTINEL_WEBHOOK_TLS_KEY`
   (PEM paths, e.g. cert-manager mounts). Plain HTTP remains for local dev/tests.
7. **No RBAC manifests existed.** The operator could not run under a service
   account at all. Added `operator/config/rbac/` (ServiceAccount + ClusterRole +
   ClusterRoleBinding): CR get/list/watch/patch, CR `/status` patch, and
   Deployment/Service CRUD the reconciler calls. (Secrets get/list included
   because the gateway pods consume them; the operator itself never reads
   secret values.)
8. **Resource-change detection used `repr(spec)`** (insertion-order sensitive):
   semantically identical specs with reordered keys triggered spurious
   re-applies. Now canonical JSON (`sort_keys=True`).
9. **Generated pods had no securityContext.** The operator's Deployment now sets
   `runAsNonRoot`, run/fs group 10001, and the RuntimeDefault seccomp profile.

## Verified sound (no change needed)

- **JWT verification** (`gateway/core/auth.py::_verify_oidc_token`): real
  signature verification via PyJWKClient, strict algorithm allowlist
  (RS256/384/512, ES256/384/512 — no `none`, no HS confusion), `iss` verified
  when `OIDC_ISSUER` is set, `exp` verified by PyJWT, `nonce` checked on the
  callback path, required-groups enforcement.
- **Login state** is single-use (consume-once via the state backend) with a
  10-minute cap; state replay is rejected (test proves 401 "already used").
- **Sessions**: HS256, server-side secret required (fails closed if unset),
  `kind` checks, absolute max-lifetime cap on top of TTL, server-side
  revocation list checked on every parse, refresh rotates + revokes the old token.
- **Gateway key / identity model**: constant-time key compare, fail-closed when
  `GATEWAY_API_KEY` unset, signed `X-User-Id` identity (HMAC) unless an explicit
  trusted-proxy opt-in, proxy identity headers honored only when
  `OIDC_TRUST_PROXY_HEADERS=true`.
- **Reconciler logic**: finalizer add/remove, deletion cleanup, last-known-good
  rollback on apply failure, status conditions — all unit-tested and passing.

## Explicitly UNVERIFIED — cluster-only (no claim made)

This sandbox has no kubectl and no cluster. None of the following has been, or
can be, proven here:

- **CRD apply + schema acceptance** against a real API server, including the
  new `auth.secretName` property.
- **Status subresource behavior** end-to-end (fix #3 is standard Kubernetes
  semantics, unit-tested only through the adapter contract).
- **Admission webhook wiring**: both manifests in
  `operator/config/webhook/validating-webhook.yaml` ship `caBundle: ""`.
  With `failurePolicy: Fail` and an empty bundle the API server cannot verify
  the webhook's TLS and will **reject all CREATE/UPDATE** on the CR — the
  bundle must be populated (cert-manager `cert-manager.io/inject-ca-from`
  annotation or manual) before applying, and the webhook Deployment/Service
  itself is not included in the repo manifests.
- **Leader election**: the operator's `run_forever` is a single poll loop with
  no leader election. Running >1 replica will make replicas fight over status
  patches. Run exactly one replica until leader election is added.
- **RBAC correctness** of the new manifests against a real API server
  (they follow the adapter's calls, but no `kubectl auth can-i` was possible).
- **Conversion webhook**: the handler only re-stamps `apiVersion`; with a single
  served version (v1alpha1) there is nothing to convert. It will silently
  "convert" between future versions without field mapping — acceptable today,
  a stub if a v1beta1 ever ships.
- **HA state sharing**: with the memory state backend, sessions/state do not
  survive pod restarts and are not shared across gateway replicas; the
  postgres/redis backends exist but were not exercised in-cluster here.

## Pre-existing divergences (not changed in this pass)

- Local tree `gateway/main.py` registers a `routes_billing` router that the
  pushed GitHub copy does **not** have (and the pushed copy still carries an
  AGPL-3.0 SPDX header while the repo is proprietary-licensed). The local file
  was left untouched in this pass; reconciling it is a separate decision.
- CRD base file is still named
  `operator/config/crd/bases/platform.phaselock.io_phaselockgateways.yaml`
  (old Phase-Lock name); its contents are the current
  `platform.axiomzetasentinel.io` group. Cosmetic; nothing references the path.
- `OIDC_SESSION_SECRET` has no minimum-length enforcement (PyJWT warns below
  32 bytes for HS256). Operators should use ≥32 random bytes; noted, not enforced,
  to avoid breaking existing deployments silently.

## Bottom line

OIDC token verification, the login flow, and the reconciler's control logic are
real and tested. The pass fixed one engine import bug, one auth hardening gap,
and three operator defects (status endpoint, missing secrets, missing RBAC)
that would each have broken a first real-cluster deployment. Nothing here
claims in-cluster proof: the first deployment still needs the caBundle filled,
a single operator replica, and a supervised CR apply.
