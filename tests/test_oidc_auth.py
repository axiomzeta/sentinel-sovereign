import importlib
import pathlib
import sys
import types

from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_fake_axiom_zeta_sentinel_core() -> None:
    module = types.ModuleType("axiom_zeta_sentinel_core")

    class DriftEngine:
        def __init__(self, nx: int, ny: int, n_fields: int, seed: int, baseline_windows: int = 5) -> None:
            self.nx = nx
            self.ny = ny
            self.n_fields = n_fields
            self.seed = seed

        def stabilize(self, metric_window: list[float], embedding: list[float] | None = None) -> dict[str, object]:
            clean = [float(x) for x in metric_window]
            avg_abs = sum(abs(x) for x in clean) / max(1, len(clean))
            drift_score = max(0.0, min(1.0, avg_abs))

            anchor = 0.0 if not clean else clean[-1]
            stabilized_vector = [max(-1.0, min(1.0, anchor)) for _ in range(24)]

            euler_chi = max(-2.0, min(2.0, 2.0 - (2.0 * drift_score)))
            genus = max(0.0, min(2.0, (2.0 - euler_chi) / 2.0))

            return {
                "stabilized_vector": stabilized_vector,
                "drift_score": drift_score,
                "euler_chi": euler_chi,
                "genus": genus,
                "beta0": 1.0,
                "beta1": round(3.0 * drift_score),
            }

        def reset_baseline(self) -> None:
            pass

        def export_baseline(self) -> dict[str, object]:
            return {"ready": False}

        def import_baseline(self, state: dict[str, object]) -> None:
            pass

    module.DriftEngine = DriftEngine
    sys.modules["axiom_zeta_sentinel_core"] = module


def _build_oidc_client(
    monkeypatch, *, required_groups: str = "", group_role_map: str = "", trust_proxy_headers: bool = True
) -> TestClient:
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    # These tests model a deployment behind an authenticating proxy (e.g. oauth2-proxy).
    monkeypatch.setenv("OIDC_TRUST_PROXY_HEADERS", "true" if trust_proxy_headers else "false")
    monkeypatch.setenv("TEAM_STORE_BACKEND", "memory")
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_REQUIRED_GROUPS", required_groups)
    monkeypatch.setenv("OIDC_GROUP_ROLE_MAP", group_role_map)

    _install_fake_axiom_zeta_sentinel_core()

    for name in [
        "gateway.core.team",
        "gateway.core.team_schemas",
        "gateway.core.service",
        "gateway.api.routes_team",
        "gateway.api.routes_stabilize",
        "gateway.main",
    ]:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)

    main = sys.modules["gateway.main"]
    telemetry = importlib.import_module("gateway.core.telemetry")
    telemetry.telemetry_store.reset()
    team = importlib.import_module("gateway.core.team")
    team.team_store.reset()
    return TestClient(main.app)


def test_oidc_mode_requires_identity_headers(monkeypatch):
    client = _build_oidc_client(monkeypatch)

    missing_identity = client.post("/v1/team/orgs", json={"name": "No Identity"})
    assert missing_identity.status_code == 401


def test_oidc_group_role_map_grants_org_admin_access(monkeypatch):
    client = _build_oidc_client(monkeypatch, group_role_map="oidc-admin:admin")

    org_res = client.post(
        "/v1/team/orgs",
        headers={"X-Forwarded-User": "owner-oidc", "X-Forwarded-Groups": "oidc-admin"},
        json={"name": "OIDC Org"},
    )
    assert org_res.status_code == 200
    org_id = org_res.json()["org_id"]

    workspace_res = client.post(
        f"/v1/team/orgs/{org_id}/workspaces",
        headers={"X-Forwarded-User": "mapped-admin", "X-Forwarded-Groups": "oidc-admin"},
        json={"name": "prod"},
    )
    assert workspace_res.status_code == 200


def test_oidc_required_group_blocks_stabilize(monkeypatch):
    client = _build_oidc_client(monkeypatch, required_groups="employees")

    denied = client.post(
        "/v1/stabilize",
        headers={"X-Forwarded-User": "user-1", "X-Forwarded-Groups": "contractors"},
        json={
            "tenant_id": "acme",
            "request_id": "req-oidc-1",
            "metric_window": [0.1] * 24,
            "metadata": {},
        },
    )
    assert denied.status_code == 403


def test_oidc_mode_ignores_identity_headers_unless_proxy_trusted(monkeypatch):
    client = _build_oidc_client(monkeypatch, group_role_map="oidc-admin:admin", trust_proxy_headers=False)

    spoofed = client.post(
        "/v1/team/orgs",
        headers={"X-Forwarded-User": "owner-oidc", "X-Forwarded-Groups": "oidc-admin"},
        json={"name": "Spoofed Org"},
    )
    assert spoofed.status_code == 401


def _build_bearer_client(monkeypatch, *, audience=None, client_id="svc-client"):
    from gateway.core import auth as auth_mod
    import importlib

    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("TEAM_STORE_BACKEND", "memory")
    monkeypatch.setenv("OIDC_JWKS_URL", "https://issuer.example.com/jwks")
    monkeypatch.setenv("OIDC_ISSUER", "https://issuer.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", client_id)
    if audience is None:
        monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
    else:
        monkeypatch.setenv("OIDC_AUDIENCE", audience)

    _install_fake_axiom_zeta_sentinel_core()
    for name in [
        "gateway.core.team",
        "gateway.core.team_schemas",
        "gateway.core.service",
        "gateway.api.routes_team",
        "gateway.api.routes_stabilize",
        "gateway.main",
    ]:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)
    main = sys.modules["gateway.main"]
    return TestClient(main.app)


def _signed_token(*, key, kid, aud, iss="https://issuer.example.com"):
    import time
    from cryptography.hazmat.primitives.asymmetric import rsa
    import jwt as pyjwt
    now = int(time.time())
    return pyjwt.encode(
        {"sub": "svc-user", "aud": aud, "iss": iss, "iat": now, "exp": now + 300},
        key,
        algorithm="RS256",
        headers={"kid": kid},
    )


def _mock_jwks(monkeypatch, key):
    from gateway.core import auth as auth_mod

    class FakeKey:
        def __init__(self, k):
            self.key = k

    class FakeClient:
        def get_signing_key_from_jwt(self, token):
            return FakeKey(key)

    monkeypatch.setattr(auth_mod, "_jwk_client_for", lambda url: FakeClient())


def test_bearer_audience_defaults_to_client_id(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verify_key = private.public_key()

    _mock_jwks(monkeypatch, verify_key)
    client = _build_bearer_client(monkeypatch, audience=None, client_id="svc-client")

    # Token for a DIFFERENT audience must be rejected even though OIDC_AUDIENCE is unset:
    # the client_id is the fallback audience (id_tokens are aud-bound to the client).
    bad = _signed_token(key=private, kid="k1", aud="someone-else")
    res = client.get("/v1/auth/session", headers={"Authorization": f"Bearer {bad}"})
    assert res.status_code == 401

    good = _signed_token(key=private, kid="k1", aud="svc-client")
    res = client.get("/v1/auth/session", headers={"Authorization": f"Bearer {good}"})
    assert res.status_code == 200
    assert res.json()["user_id"] == "svc-user"
