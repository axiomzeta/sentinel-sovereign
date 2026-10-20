from gateway.operator_controller import FINALIZER, AxiomZetaSentinelGatewayReconciler


class FakeAdapter:
    def __init__(self, resources):
        self.resources = resources
        self.patch_calls = []
        self.deployment_calls = []
        self.service_calls = []
        self.cleanup_calls = []
        self.fail_apply = False
        self.healthy = True

    def list_custom_resources(self):
        return self.resources

    def patch_custom_resource(self, *, namespace, name, patch):
        self.patch_calls.append({"namespace": namespace, "name": name, "patch": patch})

    def patch_custom_resource_status(self, *, namespace, name, patch):
        # Test double records status-subresource writes separately, mirroring
        # the API server: status must not go through the main resource endpoint.
        self.patch_calls.append({"namespace": namespace, "name": name, "patch": patch, "status_subresource": True})

    def apply_deployment(self, *, namespace, name, body):
        if self.fail_apply:
            raise RuntimeError("boom")
        self.deployment_calls.append({"namespace": namespace, "name": name, "body": body})

    def apply_service(self, *, namespace, name, body):
        if self.fail_apply:
            raise RuntimeError("boom")
        self.service_calls.append({"namespace": namespace, "name": name, "body": body})

    def delete_managed_resources(self, *, namespace, name):
        self.cleanup_calls.append({"namespace": namespace, "name": name})

    def deployment_is_healthy(self, *, namespace, name, desired_replicas):
        return self.healthy


def _base_resource():
    return {
        "metadata": {
            "name": "axiom-zeta-sentinel-enterprise",
            "namespace": "axiomzetasentinel",
            "generation": 3,
            "finalizers": [],
        },
        "spec": {
            "image": "ghcr.io/acme/axiom-zeta-sentinel:latest",
            "replicas": 3,
            "tenantRuntimeCacheEnabled": False,
            "auth": {"mode": "oidc", "requiredGroups": ["axiom-zeta-sentinel-users"]},
            "policy": {
                "mode": "opa",
                "opaUrl": "http://opa:8181",
                "bundleConfigMapName": "policy-bundle",
                "defaultDecision": "deny",
                "decisionLogPath": "/var/log/axiom-zeta-sentinel/policy.jsonl",
            },
            "airgap": {
                "enabled": True,
                "allowOutboundNetwork": False,
                "allowedHosts": ["idp.example.com", "opa.svc.cluster.local"],
            },
            "highAvailability": {"enabled": True, "minAvailable": 2},
        },
    }


def test_desired_deployment_includes_airgap_policy_env_and_bundle_mount():
    adapter = FakeAdapter(resources=[])
    reconciler = AxiomZetaSentinelGatewayReconciler(adapter)

    deployment = reconciler._desired_deployment(_base_resource())
    template_spec = deployment["spec"]["template"]["spec"]
    container = template_spec["containers"][0]
    env = {item["name"]: item["value"] for item in container["env"]}

    assert env["AIRGAP_ALLOWED_HOSTS"] == "idp.example.com,opa.svc.cluster.local"
    assert env["POLICY_DEFAULT_DECISION"] == "deny"
    assert env["POLICY_DECISION_LOG_PATH"] == "/var/log/axiom-zeta-sentinel/policy.jsonl"
    assert env["POLICY_BUNDLE_PATH"] == "/etc/axiom-zeta-sentinel/policy/policy-bundle.json"
    assert env["TENANT_RUNTIME_CACHE_ENABLED"] == "false"
    assert env["OIDC_SESSION_TTL_SECONDS"] == "3600"
    assert env["OIDC_SESSION_MAX_LIFETIME_SECONDS"] == "86400"

    volumes = template_spec["volumes"]
    assert {"name": "policy-bundle", "configMap": {"name": "policy-bundle"}} in volumes
    volume_mounts = container["volumeMounts"]
    assert {
        "name": "policy-bundle",
        "mountPath": "/etc/axiom-zeta-sentinel/policy",
        "readOnly": True,
    } in volume_mounts


def test_desired_deployment_includes_auth_audit_log_settings():
    adapter = FakeAdapter(resources=[])
    reconciler = AxiomZetaSentinelGatewayReconciler(adapter)
    resource = _base_resource()
    resource["spec"]["auth"]["oidc"] = {
        "sessionTtlSeconds": 1200,
        "sessionMaxLifetimeSeconds": 7200,
        "auditLogPath": "/var/log/axiom-zeta-sentinel/auth-audit.jsonl",
        "auditLogMaxBytes": 2048,
        "auditLogBackupCount": 3,
    }

    deployment = reconciler._desired_deployment(resource)
    env = {item["name"]: item["value"] for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}

    assert env["AUTH_AUDIT_LOG_PATH"] == "/var/log/axiom-zeta-sentinel/auth-audit.jsonl"
    assert env["AUTH_AUDIT_LOG_MAX_BYTES"] == "2048"
    assert env["AUTH_AUDIT_LOG_BACKUP_COUNT"] == "3"


def test_reconcile_adds_finalizer_applies_resources_and_sets_ready_status():
    adapter = FakeAdapter(resources=[_base_resource()])
    reconciler = AxiomZetaSentinelGatewayReconciler(adapter)

    results = reconciler.reconcile_once()

    assert len(results) == 1
    assert results[0].phase == "Ready"
    assert any(call["patch"]["metadata"]["finalizers"] == [FINALIZER] for call in adapter.patch_calls)
    assert len(adapter.deployment_calls) == 1
    assert len(adapter.service_calls) == 1

    status_patches = [c for c in adapter.patch_calls if "status" in c["patch"]]
    assert status_patches
    assert status_patches[-1]["patch"]["status"]["phase"] == "Ready"


def test_reconcile_handles_deletion_with_finalizer_cleanup_and_status():
    resource = _base_resource()
    resource["metadata"]["finalizers"] = [FINALIZER]
    resource["metadata"]["deletionTimestamp"] = "2026-07-09T00:00:00Z"

    adapter = FakeAdapter(resources=[resource])
    reconciler = AxiomZetaSentinelGatewayReconciler(adapter)

    results = reconciler.reconcile_once()

    assert results[0].phase == "Terminating"
    assert len(adapter.cleanup_calls) == 1
    metadata_patches = [c for c in adapter.patch_calls if "metadata" in c["patch"]]
    assert metadata_patches
    assert metadata_patches[-1]["patch"]["metadata"]["finalizers"] == []
    status_patches = [c for c in adapter.patch_calls if "status" in c["patch"]]
    assert status_patches[-1]["patch"]["status"]["phase"] == "Terminating"


def test_reconcile_sets_degraded_status_on_apply_error():
    adapter = FakeAdapter(resources=[_base_resource()])
    adapter.fail_apply = True
    reconciler = AxiomZetaSentinelGatewayReconciler(adapter)

    results = reconciler.reconcile_once()

    assert results[0].phase == "Degraded"
    status_patches = [c for c in adapter.patch_calls if "status" in c["patch"]]
    assert status_patches
    degraded = status_patches[-1]["patch"]["status"]
    assert degraded["phase"] == "Degraded"
    assert degraded["conditions"][0]["reason"] == "ReconcileError"


def test_reconcile_sets_progressing_until_rollout_is_healthy():
    adapter = FakeAdapter(resources=[_base_resource()])
    adapter.healthy = False
    reconciler = AxiomZetaSentinelGatewayReconciler(adapter)

    results = reconciler.reconcile_once()

    assert results[0].phase == "Progressing"
    status_patches = [c for c in adapter.patch_calls if "status" in c["patch"]]
    assert status_patches
    progressing = status_patches[-1]["patch"]["status"]
    assert progressing["phase"] == "Progressing"
    assert progressing["conditions"][0]["reason"] == "RolloutInProgress"


def test_reconcile_applies_rollback_when_previous_good_state_exists():
    resource = _base_resource()
    adapter = FakeAdapter(resources=[resource])
    reconciler = AxiomZetaSentinelGatewayReconciler(adapter)

    first = reconciler.reconcile_once()
    assert first[0].phase == "Ready"

    # Simulate a broken update after first successful reconcile.
    resource["spec"]["image"] = ""
    second = reconciler.reconcile_once()

    assert second[0].phase == "Degraded"
    status_patches = [c for c in adapter.patch_calls if "status" in c["patch"]]
    degraded = status_patches[-1]["patch"]["status"]
    assert degraded["conditions"][0]["reason"] == "RollbackApplied"


def test_status_writes_use_status_subresource():
    resource = _base_resource()
    adapter = FakeAdapter([resource])
    reconciler = AxiomZetaSentinelGatewayReconciler(adapter)
    reconciler.reconcile_once()
    status_patches = [c for c in adapter.patch_calls if c.get("status_subresource")]
    assert status_patches, "expected status patches via the status subresource"
    finalizer_patches = [
        c for c in adapter.patch_calls if "finalizers" in str(c["patch"].get("metadata", {})) and not c.get("status_subresource")
    ]
    assert finalizer_patches, "finalizer patch must stay on the main resource endpoint"


def test_deployment_wires_secrets_and_security_context():
    resource = _base_resource()
    resource["spec"]["auth"] = {"mode": "oidc", "secretName": "gw-secrets"}
    adapter = FakeAdapter([resource])
    reconciler = AxiomZetaSentinelGatewayReconciler(adapter)
    reconciler.reconcile_once()
    assert adapter.deployment_calls, "expected a deployment apply"
    body = adapter.deployment_calls[0]["body"]
    pod_spec = body["spec"]["template"]["spec"]
    assert pod_spec["securityContext"]["runAsNonRoot"] is True
    env = pod_spec["containers"][0]["env"]
    secret_refs = {e["name"]: e["valueFrom"]["secretKeyRef"]["name"] for e in env if "valueFrom" in e}
    assert secret_refs["GATEWAY_API_KEY"] == "gw-secrets"
    assert secret_refs["OIDC_SESSION_SECRET"] == "gw-secrets"
