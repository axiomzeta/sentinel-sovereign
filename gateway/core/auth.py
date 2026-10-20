import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from threading import Lock
from time import time
from typing import Any
from uuid import uuid4

from fastapi import Depends, Header, HTTPException, Request

from gateway.core.config import load_settings
from gateway.core.egress import assert_url_allowed
from gateway.core.state_backend import get_state_backend

try:
    import jwt
    from jwt import InvalidTokenError, PyJWKClient
except ImportError:  # pragma: no cover - depends on optional runtime package
    jwt = None
    InvalidTokenError = Exception
    PyJWKClient = None


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    groups: tuple[str, ...]
    auth_type: str


_JWK_CLIENT_LOCK = Lock()
_JWK_CLIENTS: dict[str, Any] = {}
_AUTH_AUDIT_LOCK = Lock()


def _csv_header_values(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return tuple()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _identity_from_proxy_headers(
    x_forwarded_user: str | None,
    x_auth_request_user: str | None,
    x_user_id: str | None,
) -> str | None:
    for candidate in (x_forwarded_user, x_auth_request_user, x_user_id):
        if candidate is not None and candidate.strip():
            return candidate.strip()
    return None


def _extract_bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    value = authorization.strip()
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Invalid Authorization header format")
    return token.strip()


def _claims_to_groups(claims: dict[str, Any]) -> tuple[str, ...]:
    raw = claims.get("groups")
    if isinstance(raw, list):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    if isinstance(raw, str):
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    return tuple()


def _claims_to_user_id(claims: dict[str, Any]) -> str | None:
    for field in ("sub", "preferred_username", "email", "upn"):
        value = claims.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _jwk_client_for(url: str) -> Any:
    if PyJWKClient is None:
        raise HTTPException(status_code=500, detail="OIDC verification is unavailable without PyJWT")
    with _JWK_CLIENT_LOCK:
        client = _JWK_CLIENTS.get(url)
        if client is None:
            client = PyJWKClient(url)
            _JWK_CLIENTS[url] = client
        return client


def _assert_egress_allowed(url: str) -> None:
    settings = load_settings()
    assert_url_allowed(url, settings)


def _require_jwt() -> None:
    if jwt is None:
        raise HTTPException(status_code=500, detail="OIDC verification is unavailable without PyJWT")


def _session_secret() -> str:
    settings = load_settings()
    secret = settings.oidc_session_secret
    if not secret:
        raise HTTPException(status_code=500, detail="OIDC_SESSION_SECRET must be configured for OIDC sessions")
    return secret


def _encode_session(payload: dict[str, Any]) -> str:
    _require_jwt()
    secret = _session_secret()
    return jwt.encode(payload, secret, algorithm="HS256")


def _decode_session(token: str) -> dict[str, Any]:
    _require_jwt()
    secret = _session_secret()
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"])
    except InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid OIDC session token")
    if not isinstance(claims, dict):
        raise HTTPException(status_code=401, detail="Invalid OIDC session payload")
    return claims


def _rotate_log_file(path: str, max_bytes: int, backup_count: int) -> None:
    if max_bytes <= 0 or not os.path.exists(path):
        return
    try:
        if os.path.getsize(path) < max_bytes:
            return
    except OSError:
        return

    if backup_count > 0:
        for i in range(backup_count - 1, 0, -1):
            src = f"{path}.{i}"
            dst = f"{path}.{i + 1}"
            if os.path.exists(src):
                try:
                    os.replace(src, dst)
                except OSError:
                    pass
        try:
            os.replace(path, f"{path}.1")
        except OSError:
            return
    else:
        try:
            os.remove(path)
        except OSError:
            return


def log_auth_event(
    *,
    event: str,
    user_id: str | None,
    details: dict[str, Any] | None = None,
) -> None:
    settings = load_settings()
    path = settings.auth_audit_log_path
    if not path:
        return

    record: dict[str, Any] = {
        "event": event,
        "user_id": user_id,
        "timestamp": int(time()),
    }
    if details:
        record["details"] = details

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    line = json.dumps(record, separators=(",", ":")) + "\n"
    with _AUTH_AUDIT_LOCK:
        _rotate_log_file(
            path,
            settings.auth_audit_log_max_bytes,
            settings.auth_audit_log_backup_count,
        )
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)


def create_oidc_login_state(*, nonce: str, return_to: str) -> str:
    settings = load_settings()
    issued_at = int(time())
    state_id = f"st_{uuid4().hex}"
    payload: dict[str, Any] = {
        "kind": "oidc_state",
        "jti": state_id,
        "nonce": nonce,
        "return_to": return_to,
        "iat": issued_at,
        "exp": issued_at + min(600, settings.oidc_session_ttl_seconds),
    }
    return _encode_session(payload)


def parse_oidc_login_state(state_token: str) -> dict[str, str]:
    claims = _decode_session(state_token)
    if claims.get("kind") != "oidc_state":
        raise HTTPException(status_code=401, detail="Invalid OIDC login state")

    nonce = claims.get("nonce")
    return_to = claims.get("return_to", "/")
    if not isinstance(nonce, str) or not nonce.strip():
        raise HTTPException(status_code=401, detail="Invalid OIDC login state nonce")

    state_id = claims.get("jti")
    if not isinstance(state_id, str) or not state_id.strip():
        raise HTTPException(status_code=401, detail="Invalid OIDC login state id")

    ttl = 600
    exp = claims.get("exp")
    now = int(time())
    if isinstance(exp, int):
        ttl = max(1, exp - now)

    backend = get_state_backend(load_settings())
    if not backend.consume_once(f"oidc:state:{state_id.strip()}", ttl_seconds=ttl):
        raise HTTPException(status_code=401, detail="OIDC login state already used")

    if not isinstance(return_to, str) or not return_to.startswith("/"):
        return_to = "/"
    return {"nonce": nonce, "return_to": return_to}


def create_oidc_session_token(ctx: AuthContext, original_issued_at: int | None = None) -> str:
    settings = load_settings()
    issued_at = int(time())
    orig_iat = original_issued_at if isinstance(original_issued_at, int) else issued_at
    absolute_exp = orig_iat + settings.oidc_session_max_lifetime_seconds
    relative_exp = issued_at + settings.oidc_session_ttl_seconds
    exp = min(absolute_exp, relative_exp)
    session_id = f"sid_{uuid4().hex}"
    payload: dict[str, Any] = {
        "kind": "oidc_session",
        "sub": ctx.user_id,
        "groups": list(ctx.groups),
        "auth_type": ctx.auth_type,
        "jti": session_id,
        "orig_iat": orig_iat,
        "iat": issued_at,
        "exp": exp,
    }
    return _encode_session(payload)


def _parse_oidc_session_claims(session_token: str) -> dict[str, Any]:
    claims = _decode_session(session_token)
    if claims.get("kind") != "oidc_session":
        raise HTTPException(status_code=401, detail="Invalid OIDC session kind")

    user_id = claims.get("sub")
    if not isinstance(user_id, str) or not user_id.strip():
        raise HTTPException(status_code=401, detail="Invalid OIDC session subject")

    settings = load_settings()
    now = int(time())
    orig_iat = claims.get("orig_iat")
    if isinstance(orig_iat, int) and now > (orig_iat + settings.oidc_session_max_lifetime_seconds):
        raise HTTPException(status_code=401, detail="OIDC session exceeded maximum lifetime")

    raw_groups = claims.get("groups", [])
    groups: tuple[str, ...]
    if isinstance(raw_groups, list):
        groups = tuple(str(item).strip() for item in raw_groups if str(item).strip())
    else:
        groups = tuple()

    required = set(settings.oidc_required_groups)
    if required and required.isdisjoint(groups):
        raise HTTPException(status_code=403, detail="OIDC user does not satisfy required groups")

    jti = claims.get("jti")
    if isinstance(jti, str) and jti.strip():
        backend = get_state_backend(settings)
        if backend.is_session_revoked(jti.strip()):
            raise HTTPException(status_code=401, detail="OIDC session has been revoked")

    return claims


def parse_oidc_session_token(session_token: str) -> AuthContext:
    claims = _parse_oidc_session_claims(session_token)
    user_id = str(claims.get("sub", "")).strip()

    raw_groups = claims.get("groups", [])
    groups: tuple[str, ...]
    if isinstance(raw_groups, list):
        groups = tuple(str(item).strip() for item in raw_groups if str(item).strip())
    else:
        groups = tuple()

    auth_type = claims.get("auth_type")
    if auth_type != "oidc":
        auth_type = "oidc"

    return AuthContext(user_id=user_id.strip(), groups=groups, auth_type=auth_type)


def rotate_oidc_session_token(session_token: str) -> tuple[AuthContext, str]:
    claims = _parse_oidc_session_claims(session_token)
    user_id = str(claims.get("sub", "")).strip()
    raw_groups = claims.get("groups", [])
    groups: tuple[str, ...]
    if isinstance(raw_groups, list):
        groups = tuple(str(item).strip() for item in raw_groups if str(item).strip())
    else:
        groups = tuple()
    ctx = AuthContext(user_id=user_id, groups=groups, auth_type="oidc")
    orig_iat = claims.get("orig_iat")
    refreshed = create_oidc_session_token(ctx, original_issued_at=orig_iat if isinstance(orig_iat, int) else None)
    return ctx, refreshed


def revoke_oidc_session_token(session_token: str) -> None:
    claims = _decode_session(session_token)
    if claims.get("kind") != "oidc_session":
        return

    jti = claims.get("jti")
    exp = claims.get("exp")
    now = int(time())
    if not isinstance(jti, str) or not jti.strip():
        return
    ttl = 60
    if isinstance(exp, int):
        ttl = max(60, exp - now)

    backend = get_state_backend(load_settings())
    backend.revoke_session(jti.strip(), ttl_seconds=ttl)


def _verify_oidc_token(
    token: str,
    expected_nonce: str | None = None,
    jwks_url_override: str | None = None,
) -> AuthContext:
    settings = load_settings()
    _require_jwt()
    jwks_url = jwks_url_override or settings.oidc_jwks_url
    if jwks_url is None:
        raise HTTPException(status_code=500, detail="OIDC_JWKS_URL is required for bearer token verification")
    _assert_egress_allowed(jwks_url)

    try:
        signing_key = _jwk_client_for(jwks_url).get_signing_key_from_jwt(token).key
        # OIDC id_tokens must carry aud == client_id. If OIDC_AUDIENCE is not set explicitly,
        # fall back to the client_id instead of skipping audience verification entirely.
        audience = settings.oidc_audience or settings.oidc_client_id
        options = {"verify_aud": audience is not None}
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
            audience=audience,
            issuer=settings.oidc_issuer,
            options=options,
        )
    except InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid OIDC bearer token")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Unable to validate OIDC bearer token")

    user_id = _claims_to_user_id(claims)
    if user_id is None:
        raise HTTPException(status_code=401, detail="OIDC token does not include a usable identity claim")

    if expected_nonce is not None:
        nonce = claims.get("nonce")
        if not isinstance(nonce, str) or nonce != expected_nonce:
            raise HTTPException(status_code=401, detail="OIDC token nonce mismatch")

    groups = _claims_to_groups(claims)
    required = set(settings.oidc_required_groups)
    if required and required.isdisjoint(groups):
        raise HTTPException(status_code=403, detail="OIDC user does not satisfy required groups")

    return AuthContext(user_id=user_id, groups=groups, auth_type="oidc")


def verify_oidc_token(
    token: str,
    expected_nonce: str | None = None,
    jwks_url_override: str | None = None,
) -> AuthContext:
    return _verify_oidc_token(token, expected_nonce=expected_nonce, jwks_url_override=jwks_url_override)


def api_key_matches(provided: str | None, expected: str) -> bool:
    if provided is None:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _check_gateway_key(settings: Any, x_api_key: str | None) -> None:
    if settings.api_key is None:
        # Fail closed: an unset key must not silently open the gateway.
        if settings.allow_insecure_no_auth:
            return
        raise HTTPException(
            status_code=503,
            detail="GATEWAY_API_KEY is not configured. Set it, or set ALLOW_INSECURE_NO_AUTH=true for local development only.",
        )
    if not api_key_matches(x_api_key, settings.api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")


def sign_user_identity(user_id: str, groups: tuple[str, ...] | list[str], secret: str) -> str:
    """Signature a trusted front end sends as X-User-Signature for X-User-Id + X-User-Groups."""
    message = user_id + "|" + ",".join(groups)
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def _verify_signed_identity(settings: Any, user_id: str, groups: tuple[str, ...], signature: str | None) -> None:
    # In api_key mode, X-User-Id / X-User-Groups are only an identity if something the caller
    # can't forge vouches for them; the shared gateway key alone lets any holder claim any user.
    if settings.user_id_signing_secret:
        expected = sign_user_identity(user_id, groups, settings.user_id_signing_secret)
        if signature is None or not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=401, detail="Invalid or missing X-User-Signature")
        return
    if settings.allow_unsigned_user_id:
        return
    raise HTTPException(
        status_code=503,
        detail="USER_ID_SIGNING_SECRET is not configured. Set it (recommended), or ALLOW_UNSIGNED_USER_ID=true if identity headers come only from a trusted proxy.",
    )


def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    x_forwarded_user: str | None = Header(default=None),
    x_forwarded_groups: str | None = Header(default=None),
    x_auth_request_user: str | None = Header(default=None),
    x_auth_request_groups: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
    x_user_groups: str | None = Header(default=None),
) -> None:
    settings = load_settings()
    if settings.auth_mode == "oidc":
        _ = require_auth_context(
            request=request,
            x_api_key=x_api_key,
            authorization=authorization,
            x_user_id=x_user_id,
            x_user_groups=x_user_groups,
            x_forwarded_user=x_forwarded_user,
            x_forwarded_groups=x_forwarded_groups,
            x_auth_request_user=x_auth_request_user,
            x_auth_request_groups=x_auth_request_groups,
        )
        return

    _check_gateway_key(settings, x_api_key)


def require_auth_context(
    request: Request,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
    x_user_groups: str | None = Header(default=None),
    x_forwarded_user: str | None = Header(default=None),
    x_forwarded_groups: str | None = Header(default=None),
    x_auth_request_user: str | None = Header(default=None),
    x_auth_request_groups: str | None = Header(default=None),
) -> AuthContext:
    settings = load_settings()

    if settings.auth_mode == "oidc":
        token = _extract_bearer_token(authorization)
        if token is not None:
            return _verify_oidc_token(token)

        session_token = request.cookies.get(settings.oidc_cookie_name)
        if session_token:
            return parse_oidc_session_token(session_token)

        if not settings.oidc_trust_proxy_headers:
            # Identity headers are client-controlled unless an authenticating proxy strips and
            # re-sets them. Only honor them when explicitly configured.
            raise HTTPException(status_code=401, detail="Missing bearer token or session")

        user_id = _identity_from_proxy_headers(
            x_forwarded_user=x_forwarded_user,
            x_auth_request_user=x_auth_request_user,
            x_user_id=x_user_id,
        )
        if user_id is None:
            raise HTTPException(status_code=401, detail="Missing upstream OIDC identity headers")

        groups = _csv_header_values(x_forwarded_groups or x_auth_request_groups or x_user_groups)
        required = set(settings.oidc_required_groups)
        if required and required.isdisjoint(groups):
            raise HTTPException(status_code=403, detail="OIDC user does not satisfy required groups")

        return AuthContext(user_id=user_id, groups=groups, auth_type="oidc")

    _check_gateway_key(settings, x_api_key)

    user = (x_user_id or "").strip()
    groups = _csv_header_values(x_user_groups)
    if user or groups:
        _verify_signed_identity(settings, user, groups, request.headers.get("x-user-signature"))
    return AuthContext(user_id=user, groups=groups, auth_type="api_key")


def require_user_id(ctx: AuthContext = Depends(require_auth_context)) -> str:
    if not ctx.user_id:
        raise HTTPException(status_code=401, detail="Missing X-User-Id header")
    return ctx.user_id
