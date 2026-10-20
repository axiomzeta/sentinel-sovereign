from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

FINALIZER = "platform.axiomzetasentinel.io/finalizer"


class OperatorAdapter(Protocol):
    def list_custom_resources(self) -> list[dict[str, object]]: ...

    def patch_custom_resource(
        self,
        *,
        namespace: str,
        name: str,
        patch: dict[str, object],
    ) -> None: ...

    def patch_custom_resource_status(
        self,
        *,
        namespace: str,
        name: str,
        patch: dict[str, object],
    ) -> None: ...

    def apply_deployment(
        self,
        *,
        namespace: str,
        name: str,
        body: dict[str, object],
    ) -> None: ...

    def apply_service(
        self,
        *,
        namespace: str,
        name: str,
        body: dict[str, object],
    ) -> None: ...

    def delete_managed_resources(self, *, namespace: str, name: str) -> None: ...

    def deployment_is_healthy(self, *, namespace: str, name: str, desired_replicas: int) -> bool: ...


@dataclass
class ReconcileResult:
    namespace: str
    name: str
    phase: str


class AxiomZetaSentinelGatewayReconciler:
    def __init__(self, adapter: OperatorAdapter) -> None:
        self.adapter = adapter
        self._last_known_good: dict[tuple[str, str], tuple[dict[str, object], dict[str, object], str]] = {}

    def run_forever(self, poll_seconds: float = 5.0) -> None:
        while True:
            self.reconcile_once()
            time.sleep(max(0.5, poll_seconds))

    def reconcile_once(self) -> list[ReconcileResult]:
        out: list[ReconcileResult] = []
        resources = self.adapter.list_custom_resources()
        for resource in resources:
            out.append(self._reconcile_resource(resource))
        return out

    def _reconcile_resource(self, resource: dict[str, object]) -> ReconcileResult:
        metadata = resource.get("metadata") if isinstance(resource, dict) else None
        if not isinstance(metadata, dict):
            raise ValueError("Custom resource is missing metadata")

        name = str(metadata.get("name") or "")
        namespace = str(metadata.get("namespace") or "default")
        generation = int(metadata.get("generation") or 0)
        if not name:
            raise ValueError("Custom resource metadata.name is required")

        deletion_timestamp = metadata.get("deletionTimestamp")
        finalizers = metadata.get("finalizers")
        if not isinstance(finalizers, list):
            finalizers = []

        if deletion_timestamp:
            self._reconcile_deletion(namespace=namespace, name=name, finalizers=finalizers, generation=generation)
            return ReconcileResult(namespace=namespace, name=name, phase="Terminating")

        if FINALIZER not in finalizers:
            self.adapter.patch_custom_resource(
                namespace=namespace,
                name=name,
                patch={"metadata": {"finalizers": finalizers + [FINALIZER]}},
            )

        resource_hash = self._resource_hash(resource)
        last = self._last_known_good.get((namespace, name))
        if last is not None and last[2] == resource_hash:
            self._set_status(
                namespace=namespace,
                name=name,
                generation=generation,
                phase="Ready",
                condition_type="Ready",
                condition_status="True",
                reason="NoOp",
                message="Resource already matches last known good spec",
            )
            return ReconcileResult(namespace=namespace, name=name, phase="Ready")

        try:
            deployment = self._desired_deployment(resource)
            service = self._desired_service(name)
            self.adapter.apply_deployment(namespace=namespace, name=name, body=deployment)
            self.adapter.apply_service(namespace=namespace, name=name, body=service)

            desired_replicas = int(deployment.get("spec", {}).get("replicas", 1))
            if not self.adapter.deployment_is_healthy(
                namespace=namespace,
                name=name,
                desired_replicas=desired_replicas,
            ):
                self._set_status(
                    namespace=namespace,
                    name=name,
                    generation=generation,
                    phase="Progressing",
                    condition_type="Ready",
                    condition_status="False",
                    reason="RolloutInProgress",
                    message="Deployment rollout is not healthy yet",
                )
                return ReconcileResult(namespace=namespace, name=name, phase="Progressing")

            self._last_known_good[(namespace, name)] = (deployment, service, resource_hash)
            self._set_status(
                namespace=namespace,
                name=name,
                generation=generation,
                phase="Ready",
                condition_type="Ready",
                condition_status="True",
                reason="Reconciled",
                message="Resources applied successfully",
            )
            return ReconcileResult(namespace=namespace, name=name, phase="Ready")
        except Exception as exc:
            previous = self._last_known_good.get((namespace, name))
            rolled_back = False
            rollback_error: str | None = None
            if previous is not None:
                try:
                    self.adapter.apply_deployment(namespace=namespace, name=name, body=previous[0])
                    self.adapter.apply_service(namespace=namespace, name=name, body=previous[1])
                    rolled_back = True
                except Exception as rollback_exc:
                    rollback_error = str(rollback_exc)

            reason = "RollbackApplied" if rolled_back else "ReconcileError"
            message = str(exc)
            if rollback_error is not None:
                message = f"{message}; rollback failed: {rollback_error}"
            self._set_status(
                namespace=namespace,
                name=name,
                generation=generation,
                phase="Degraded",
                condition_type="Ready",
                condition_status="False",
                reason=reason,
                message=message,
            )
            return ReconcileResult(namespace=namespace, name=name, phase="Degraded")

    def _reconcile_deletion(self, *, namespace: str, name: str, finalizers: list[object], generation: int) -> None:
        self._last_known_good.pop((namespace, name), None)
        if FINALIZER in finalizers:
            self.adapter.delete_managed_resources(namespace=namespace, name=name)
            remaining = [item for item in finalizers if item != FINALIZER]
            self.adapter.patch_custom_resource(
                namespace=namespace,
                name=name,
                patch={"metadata": {"finalizers": remaining}},
            )

        self._set_status(
            namespace=namespace,
            name=name,
            generation=generation,
            phase="Terminating",
            condition_type="Ready",
            condition_status="False",
            reason="Deleting",
            message="Custom resource is being deleted",
        )

    def _set_status(
        self,
        *,
        namespace: str,
        name: str,
        generation: int,
        phase: str,
        condition_type: str,
        condition_status: str,
        reason: str,
        message: str,
    ) -> None:
        # Status writes go through the /status subresource (the CRD enables it);
        # the finalizer patch in _reconcile_resource stays on the main endpoint.
        self.adapter.patch_custom_resource_status(
            namespace=namespace,
            name=name,
            patch={
                "status": {
                    "phase": phase,
                    "observedGeneration": generation,
                    "conditions": [
                        {
                            "type": condition_type,
                            "status": condition_status,
                            "reason": reason,
                            "message": message,
                        }
                    ],
                }
            },
        )

    def _resource_hash(self, resource: dict[str, object]) -> str:
        # Canonical JSON (sorted keys) so semantically identical specs hash alike
        # regardless of dict insertion order; repr() is order-sensitive.
        canonical = json.dumps(resource.get("spec", {}), sort_keys=True, default=str, separators=(",", ":"))
        return sha256(canonical.encode("utf-8")).hexdigest()

    def _desired_deployment(self, resource: dict[str, object]) -> dict[str, object]:
        spec = resource.get("spec")
        if not isinstance(spec, dict):
            raise ValueError("Custom resource spec is required")

        image = spec.get("image")
        if not isinstance(image, str) or not image.strip():
            raise ValueError("spec.image is required")

        replicas = spec.get("replicas")
        if not isinstance(replicas, int) or replicas < 1:
            replicas = 1

        auth = spec.get("auth") if isinstance(spec.get("auth"), dict) else {}
        high_availability = spec.get("highAvailability") if isinstance(spec.get("highAvailability"), dict) else {}
        airgap = spec.get("airgap") if isinstance(spec.get("airgap"), dict) else {}
        policy = spec.get("policy") if isinstance(spec.get("policy"), dict) else {}
        allowed_hosts = airgap.get("allowedHosts") if isinstance(airgap.get("allowedHosts"), list) else []
        policy_bundle_config_map = (
            str(policy.get("bundleConfigMapName", "")).strip() if policy.get("bundleConfigMapName") is not None else ""
        )
        policy_bundle_path = "/etc/axiom-zeta-sentinel/policy/policy-bundle.json" if policy_bundle_config_map else ""
        oidc = auth.get("oidc") if isinstance(auth.get("oidc"), dict) else {}

        env = [
            {"name": "AUTH_MODE", "value": str(auth.get("mode", "api_key"))},
            {"name": "OIDC_ISSUER", "value": str(auth.get("oidcIssuer", ""))},
            {"name": "OIDC_AUDIENCE", "value": str(auth.get("oidcAudience", ""))},
            {"name": "OIDC_JWKS_URL", "value": str(auth.get("oidcJwksUrl", ""))},
            {"name": "OIDC_SESSION_TTL_SECONDS", "value": str(oidc.get("sessionTtlSeconds", "3600"))},
            {"name": "OIDC_SESSION_MAX_LIFETIME_SECONDS", "value": str(oidc.get("sessionMaxLifetimeSeconds", "86400"))},
            {"name": "AUTH_AUDIT_LOG_PATH", "value": str(oidc.get("auditLogPath", ""))},
            {"name": "AUTH_AUDIT_LOG_MAX_BYTES", "value": str(oidc.get("auditLogMaxBytes", "10485760"))},
            {"name": "AUTH_AUDIT_LOG_BACKUP_COUNT", "value": str(oidc.get("auditLogBackupCount", "5"))},
            {"name": "OIDC_REQUIRED_GROUPS", "value": ",".join(auth.get("requiredGroups", []))},
            {"name": "OIDC_GROUP_ROLE_MAP", "value": ",".join(auth.get("groupRoleMap", []))},
            {"name": "POLICY_MODE", "value": str(policy.get("mode", "builtin"))},
            {"name": "OPA_URL", "value": str(policy.get("opaUrl", ""))},
            {"name": "POLICY_BUNDLE_PATH", "value": policy_bundle_path},
            {"name": "POLICY_DEFAULT_DECISION", "value": str(policy.get("defaultDecision", "deny"))},
            {"name": "POLICY_DECISION_LOG_PATH", "value": str(policy.get("decisionLogPath", ""))},
            {"name": "AIRGAPPED_MODE", "value": str(bool(airgap.get("enabled", False))).lower()},
            {
                "name": "ALLOW_OUTBOUND_NETWORK",
                "value": str(bool(airgap.get("allowOutboundNetwork", True))).lower(),
            },
            {"name": "AIRGAP_ALLOWED_HOSTS", "value": ",".join(str(host) for host in allowed_hosts)},
            {
                "name": "HA_MODE_ENABLED",
                "value": str(bool(high_availability.get("enabled", False))).lower(),
            },
            {"name": "TENANT_RUNTIME_CACHE_ENABLED", "value": str(bool(spec.get("tenantRuntimeCacheEnabled", False))).lower()},
        ]

        # Secrets never live in the CR. If spec.auth.secretName names a Secret in the
        # CR's namespace, inject the gateway secrets from it; without this the spawned
        # gateway fails closed (no GATEWAY_API_KEY) or cannot complete OIDC login
        # (no OIDC_CLIENT_SECRET / OIDC_SESSION_SECRET).
        secret_name = str(auth.get("secretName", "") or "").strip()
        if secret_name:
            for key in ("GATEWAY_API_KEY", "OIDC_CLIENT_SECRET", "OIDC_SESSION_SECRET"):
                env.append(
                    {
                        "name": key,
                        "valueFrom": {"secretKeyRef": {"name": secret_name, "key": key, "optional": True}},
                    }
                )

        volumes: list[dict[str, object]] = []
        volume_mounts: list[dict[str, object]] = []
        if policy_bundle_config_map:
            volumes.append(
                {
                    "name": "policy-bundle",
                    "configMap": {"name": policy_bundle_config_map},
                }
            )
            volume_mounts.append(
                {
                    "name": "policy-bundle",
                    "mountPath": "/etc/axiom-zeta-sentinel/policy",
                    "readOnly": True,
                }
            )

        labels = {"app": "axiom-zeta-sentinel-sovereign", "axiomzetasentinel.io/instance": str(resource["metadata"]["name"])}
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": str(resource["metadata"]["name"]),
                "labels": labels,
                "annotations": {
                    "axiomzetasentinel.io/reconciled-at": str(int(time.time())),
                },
            },
            "spec": {
                "replicas": replicas,
                "strategy": {
                    "type": "RollingUpdate",
                    "rollingUpdate": {
                        "maxUnavailable": 0,
                        "maxSurge": 1,
                    },
                },
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 10001,
                            "runAsGroup": 10001,
                            "fsGroup": 10001,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "gateway",
                                "image": image,
                                "ports": [{"containerPort": 8000, "name": "http"}],
                                "env": env,
                                "volumeMounts": volume_mounts,
                                "readinessProbe": {"httpGet": {"path": "/v1/health", "port": "http"}},
                                "livenessProbe": {"httpGet": {"path": "/v1/health", "port": "http"}},
                            }
                        ],
                        "volumes": volumes,
                    },
                },
            },
        }

    def _desired_service(self, name: str) -> dict[str, object]:
        labels = {"app": "axiom-zeta-sentinel-sovereign", "axiomzetasentinel.io/instance": name}
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name, "labels": labels},
            "spec": {
                "selector": labels,
                "ports": [{"port": 8000, "targetPort": "http", "name": "http"}],
                "type": "ClusterIP",
            },
        }


class KubernetesOperatorAdapter:
    def __init__(self) -> None:
        try:
            from kubernetes import client, config
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("kubernetes package is required for live operator mode") from exc

        self._client_mod = client

        # Auto-detect in-cluster vs local kubeconfig.
        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()

        self._custom = client.CustomObjectsApi()
        self._apps = client.AppsV1Api()
        self._core = client.CoreV1Api()

    def list_custom_resources(self) -> list[dict[str, object]]:  # pragma: no cover
        out = self._custom.list_cluster_custom_object(
            group="platform.axiomzetasentinel.io",
            version="v1alpha1",
            plural="axiomzetasentinelgateways",
        )
        items = out.get("items", [])
        return items if isinstance(items, list) else []

    def patch_custom_resource(self, *, namespace: str, name: str, patch: dict[str, object]) -> None:  # pragma: no cover
        self._custom.patch_namespaced_custom_object(
            group="platform.axiomzetasentinel.io",
            version="v1alpha1",
            namespace=namespace,
            plural="axiomzetasentinelgateways",
            name=name,
            body=patch,
        )

    def patch_custom_resource_status(self, *, namespace: str, name: str, patch: dict[str, object]) -> None:  # pragma: no cover
        # The CRD enables the status subresource; status writes MUST go through
        # this endpoint — the API server silently drops status on main-resource patches.
        self._custom.patch_namespaced_custom_object_status(
            group="platform.axiomzetasentinel.io",
            version="v1alpha1",
            namespace=namespace,
            plural="axiomzetasentinelgateways",
            name=name,
            body=patch,
        )

    def apply_deployment(self, *, namespace: str, name: str, body: dict[str, object]) -> None:  # pragma: no cover
        try:
            self._apps.read_namespaced_deployment(name=name, namespace=namespace)
            self._apps.patch_namespaced_deployment(name=name, namespace=namespace, body=body)
        except self._client_mod.exceptions.ApiException as exc:
            if exc.status != 404:
                raise
            self._apps.create_namespaced_deployment(namespace=namespace, body=body)

    def apply_service(self, *, namespace: str, name: str, body: dict[str, object]) -> None:  # pragma: no cover
        try:
            self._core.read_namespaced_service(name=name, namespace=namespace)
            self._core.patch_namespaced_service(name=name, namespace=namespace, body=body)
        except self._client_mod.exceptions.ApiException as exc:
            if exc.status != 404:
                raise
            self._core.create_namespaced_service(namespace=namespace, body=body)

    def delete_managed_resources(self, *, namespace: str, name: str) -> None:  # pragma: no cover
        delete_opts = self._client_mod.V1DeleteOptions()
        for method in (
            self._apps.delete_namespaced_deployment,
            self._core.delete_namespaced_service,
        ):
            try:
                method(name=name, namespace=namespace, body=delete_opts)
            except self._client_mod.exceptions.ApiException as exc:
                if exc.status != 404:
                    raise

    def deployment_is_healthy(self, *, namespace: str, name: str, desired_replicas: int) -> bool:  # pragma: no cover
        try:
            dep = self._apps.read_namespaced_deployment(name=name, namespace=namespace)
        except self._client_mod.exceptions.ApiException:
            return False

        status = dep.status
        if status is None:
            return False

        available = int(getattr(status, "available_replicas", 0) or 0)
        updated = int(getattr(status, "updated_replicas", 0) or 0)
        observed = int(getattr(status, "observed_generation", 0) or 0)
        metadata_generation = int(getattr(dep.metadata, "generation", 0) or 0)

        return observed >= metadata_generation and updated >= desired_replicas and available >= desired_replicas


def run_operator() -> None:  # pragma: no cover
    mode = os.getenv("AXIOM_ZETA_SENTINEL_OPERATOR_MODE", "kubernetes").strip().lower()
    if mode != "kubernetes":
        raise RuntimeError("Only AXIOM_ZETA_SENTINEL_OPERATOR_MODE=kubernetes is currently supported")
    adapter = KubernetesOperatorAdapter()
    reconciler = AxiomZetaSentinelGatewayReconciler(adapter)
    poll = float(os.getenv("AXIOM_ZETA_SENTINEL_OPERATOR_POLL_SECONDS", "5"))
    reconciler.run_forever(poll_seconds=poll)
