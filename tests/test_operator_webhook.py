from fastapi.testclient import TestClient

from gateway.operator_webhook import app


def _admission_request(obj):
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "request": {
            "uid": "uid-1",
            "object": obj,
        },
    }


def test_validating_webhook_accepts_valid_resource():
    client = TestClient(app)
    payload = _admission_request({"spec": {"image": "ghcr.io/acme/axiom-zeta-sentinel:latest", "replicas": 2}})
    res = client.post("/validate-platform-axiomzetasentinel-io-v1alpha1-axiomzetasentinelgateway", json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["response"]["allowed"] is True


def test_validating_webhook_rejects_invalid_resource():
    client = TestClient(app)
    payload = _admission_request({"spec": {"replicas": 0}})
    res = client.post("/validate-platform-axiomzetasentinel-io-v1alpha1-axiomzetasentinelgateway", json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["response"]["allowed"] is False


def test_conversion_webhook_converts_api_version():
    client = TestClient(app)
    payload = {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "ConversionReview",
        "request": {
            "uid": "uid-2",
            "desiredAPIVersion": "platform.axiomzetasentinel.io/v1alpha1",
            "objects": [
                {
                    "apiVersion": "platform.axiomzetasentinel.io/v1alpha1",
                    "kind": "AxiomZetaSentinelGateway",
                    "metadata": {"name": "plg"},
                    "spec": {"image": "ghcr.io/acme/axiom-zeta-sentinel:latest"},
                }
            ],
        },
    }
    res = client.post("/convert-platform-axiomzetasentinel-io", json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["response"]["result"]["status"] == "Success"
    assert body["response"]["convertedObjects"][0]["apiVersion"] == "platform.axiomzetasentinel.io/v1alpha1"


def test_validating_webhook_rejects_oidc_without_endpoints():
    client = TestClient(app)
    payload = _admission_request({"spec": {"image": "ghcr.io/acme/x:latest", "auth": {"mode": "oidc"}}})
    res = client.post("/validate-platform-axiomzetasentinel-io-v1alpha1-axiomzetasentinelgateway", json=payload)
    assert res.status_code == 200
    assert res.json()["response"]["allowed"] is False


def test_validating_webhook_accepts_oidc_with_endpoints():
    client = TestClient(app)
    payload = _admission_request(
        {
            "spec": {
                "image": "ghcr.io/acme/x:latest",
                "auth": {
                    "mode": "oidc",
                    "oidcIssuer": "https://idp.example.com",
                    "oidcJwksUrl": "https://idp.example.com/jwks",
                },
            }
        }
    )
    res = client.post("/validate-platform-axiomzetasentinel-io-v1alpha1-axiomzetasentinelgateway", json=payload)
    assert res.status_code == 200
    assert res.json()["response"]["allowed"] is True
