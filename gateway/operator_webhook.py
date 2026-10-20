from __future__ import annotations

from fastapi import FastAPI, HTTPException

app = FastAPI(title="Axiom Zeta Sentinel Operator Webhook", version="0.1.0")


def _extract_review(payload: dict[str, object]) -> tuple[str | None, dict[str, object] | None]:
    review = payload.get("request")
    if not isinstance(review, dict):
        raise HTTPException(status_code=400, detail="AdmissionReview.request is required")
    uid = review.get("uid")
    obj = review.get("object")
    return uid if isinstance(uid, str) else None, obj if isinstance(obj, dict) else None


def _admission_response(*, uid: str | None, allowed: bool, status_message: str = "") -> dict[str, object]:
    out: dict[str, object] = {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "response": {
            "uid": uid or "",
            "allowed": allowed,
        },
    }
    if not allowed:
        out["response"]["status"] = {"message": status_message}
    return out


def _validate_axiomzetasentinel_gateway(obj: dict[str, object]) -> tuple[bool, str]:
    spec = obj.get("spec")
    if not isinstance(spec, dict):
        return False, "spec is required"

    image = spec.get("image")
    if not isinstance(image, str) or not image.strip():
        return False, "spec.image is required"

    replicas = spec.get("replicas")
    if replicas is not None and (not isinstance(replicas, int) or replicas < 1):
        return False, "spec.replicas must be an integer >= 1"

    auth = spec.get("auth")
    if isinstance(auth, dict):
        mode = auth.get("mode")
        if mode is not None and mode not in {"api_key", "oidc"}:
            return False, "spec.auth.mode must be one of: api_key, oidc"
        if mode == "oidc":
            # The gateway fails closed when OIDC endpoints are missing; reject early.
            if not isinstance(auth.get("oidcIssuer"), str) or not auth.get("oidcIssuer").strip():
                return False, "spec.auth.oidcIssuer is required when spec.auth.mode is oidc"
            if not isinstance(auth.get("oidcJwksUrl"), str) or not auth.get("oidcJwksUrl").strip():
                return False, "spec.auth.oidcJwksUrl is required when spec.auth.mode is oidc"

    policy = spec.get("policy")
    if isinstance(policy, dict):
        mode = policy.get("mode")
        if mode is not None and mode not in {"builtin", "opa"}:
            return False, "spec.policy.mode must be one of: builtin, opa"

    return True, ""


@app.post("/validate-platform-axiomzetasentinel-io-v1alpha1-axiomzetasentinelgateway")
def validate_axiomzetasentinel_gateway(review: dict[str, object]) -> dict[str, object]:
    uid, obj = _extract_review(review)
    if obj is None:
        return _admission_response(uid=uid, allowed=False, status_message="request.object is required")

    allowed, message = _validate_axiomzetasentinel_gateway(obj)
    return _admission_response(uid=uid, allowed=allowed, status_message=message)


@app.post("/convert-platform-axiomzetasentinel-io")
def convert_axiomzetasentinel_gateway(review: dict[str, object]) -> dict[str, object]:
    request = review.get("request")
    if not isinstance(request, dict):
        raise HTTPException(status_code=400, detail="ConversionReview.request is required")

    uid = request.get("uid")
    desired = request.get("desiredAPIVersion")
    objects = request.get("objects")
    if not isinstance(objects, list):
        raise HTTPException(status_code=400, detail="ConversionReview.request.objects is required")

    converted: list[dict[str, object]] = []
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        out = dict(obj)
        out["apiVersion"] = desired if isinstance(desired, str) and desired else obj.get("apiVersion")
        converted.append(out)

    return {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "ConversionReview",
        "response": {
            "uid": uid if isinstance(uid, str) else "",
            "result": {"status": "Success"},
            "convertedObjects": converted,
        },
    }


def run_webhook() -> None:  # pragma: no cover
    import os
    import uvicorn

    host = os.getenv("AXIOM_ZETA_SENTINEL_WEBHOOK_HOST", "0.0.0.0")
    port = int(os.getenv("AXIOM_ZETA_SENTINEL_WEBHOOK_PORT", "9443"))
    # Kubernetes admission webhooks MUST be served over TLS. In-cluster this is
    # normally a cert-manager-issued cert mounted into the pod; pass the PEM paths
    # via env. Without them the server runs plain HTTP (local dev/tests only).
    tls_cert = os.getenv("AXIOM_ZETA_SENTINEL_WEBHOOK_TLS_CERT", "")
    tls_key = os.getenv("AXIOM_ZETA_SENTINEL_WEBHOOK_TLS_KEY", "")
    uvicorn.run(
        "gateway.operator_webhook:app",
        host=host,
        port=port,
        ssl_certfile=tls_cert or None,
        ssl_keyfile=tls_key or None,
    )
