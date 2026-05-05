"""Kubernetes client wrapper — creates/deletes Pod, Service per workspace."""

import logging
from datetime import datetime, timezone
from typing import Any

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from poc.utils.config import (
    AGENT_DEBUG,
    AGENT_DISPLAY_MODE,
    AGENT_IMAGE,
    AGENT_MODEL,
    AGENT_MODEL_PROVIDER,
    AGENT_PORT,
    CLEANUP_JOB_IMAGE,
    CLEANUP_JOB_TTL_SECONDS,
    CWA_API_BASE,
    CWA_API_KEY,
    DEFAULT_CPU_LIMIT,
    DEFAULT_CPU_REQUEST,
    DEFAULT_MEMORY_LIMIT,
    DEFAULT_MEMORY_REQUEST,
    IDLE_TIMEOUT_MINUTES,
    K8S_NAMESPACE,
    LIVENESS_FAILURE_THRESHOLD,
    LIVENESS_INITIAL_DELAY,
    LIVENESS_PERIOD,
    LIVENESS_TIMEOUT,
    OPENAI_API_BASE,
    OPENAI_API_KEY,
    POD_RESTART_POLICY,
    READINESS_FAILURE_THRESHOLD,
    READINESS_INITIAL_DELAY,
    READINESS_PERIOD,
    READINESS_TIMEOUT,
    SUB_AGENT_API_BASE,
    SUB_AGENT_API_KEY,
    SUB_AGENT_MODEL,
    SUB_AGENT_MODEL_PROVIDER,
    TERMINATION_GRACE_PERIOD_SECONDS,
)

logger = logging.getLogger("orchestrator.k8s")


class K8sClient:
    def __init__(self):
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self.core = client.CoreV1Api()
        self.batch = client.BatchV1Api()
        self.ns = K8S_NAMESPACE

    # ── Pod ───────────────────────────────────────────────────────────

    def list_pod_names(self) -> set[str]:
        """List all managed Pod names in the namespace (single K8s API call)."""
        try:
            pods = self.core.list_namespaced_pod(
                self.ns, label_selector="app=k8s-agent-platform",
            )
            return {
                p.metadata.name
                for p in pods.items
                if p.status.phase not in ("Succeeded", "Failed")
            }
        except ApiException:
            logger.warning("Failed to list pods, returning empty set")
            return set()

    def pod_exists(self, pod_name: str) -> bool:
        try:
            pod = self.core.read_namespaced_pod(pod_name, self.ns)
            return pod.status.phase not in ("Succeeded", "Failed")
        except ApiException as e:
            if e.status == 404:
                return False
            raise

    def pod_is_running(self, pod_name: str) -> bool:
        try:
            pod = self.core.read_namespaced_pod(pod_name, self.ns)
            return pod.status.phase == "Running"
        except ApiException:
            return False

    def wait_for_pod_ready(self, pod_name: str, timeout_seconds: int = 120) -> bool:
        """Poll K8s until Pod is Ready or timeout. Returns True if ready."""
        import time

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                pod = self.core.read_namespaced_pod(pod_name, self.ns)
                # Check for CrashLoopBackOff
                if pod.status.container_statuses:
                    for cs in pod.status.container_statuses:
                        if cs.state and cs.state.waiting:
                            if cs.state.waiting.reason == "CrashLoopBackOff":
                                logger.error("Pod %s in CrashLoopBackOff", pod_name)
                                return False
                # Check Ready condition
                if pod.status.conditions:
                    for cond in pod.status.conditions:
                        if cond.type == "Ready" and cond.status == "True":
                            logger.info("Pod %s is ready", pod_name)
                            return True
            except ApiException:
                pass
            time.sleep(2)

        logger.warning("Pod %s readiness timeout (%ds)", pod_name, timeout_seconds)
        return False

    def create_pod(
        self,
        workspace_id: str,
        pod_name: str,
        pvc_name: str,
        shared_workspaces: list[dict] | None = None,
    ) -> dict[str, Any]:
        workspace_path = f"/workspace/{workspace_id}"
        now = datetime.now(timezone.utc).isoformat()

        # Build volume mounts and volumes (primary + shared)
        volume_mounts = [
            client.V1VolumeMount(
                name="workspace", mount_path=workspace_path,
            ),
        ]
        volumes = [
            client.V1Volume(
                name="workspace",
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                    claim_name=pvc_name,
                ),
            ),
        ]

        # Shared workspaces (group)
        for sw in (shared_workspaces or []):
            vol_name = f"shared-{sw['workspace_id']}"
            read_only = sw["role"] == "readonly"
            volume_mounts.append(
                client.V1VolumeMount(
                    name=vol_name,
                    mount_path=f"/shared/{sw['workspace_id']}",
                    read_only=read_only,
                ),
            )
            volumes.append(
                client.V1Volume(
                    name=vol_name,
                    persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                        claim_name=sw["pvc_name"],
                        read_only=read_only,
                    ),
                ),
            )

        body = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=self.ns,
                labels={
                    "workspace_id": workspace_id,
                    "app": "k8s-agent-platform",
                    "component": "agent",
                },
                annotations={
                    "created_by": "orchestrator",
                    "created_at": now,
                },
            ),
            spec=client.V1PodSpec(
                containers=[
                    client.V1Container(
                        name="agent",
                        image=AGENT_IMAGE,
                        image_pull_policy="IfNotPresent",
                        ports=[
                            client.V1ContainerPort(
                                container_port=AGENT_PORT, name="http", protocol="TCP",
                            ),
                        ],
                        env=[
                            client.V1EnvVar(name="WORKSPACE_ID", value=workspace_id),
                            client.V1EnvVar(name="WORKSPACE_PATH", value=workspace_path),
                            client.V1EnvVar(
                                name="ORCHESTRATOR_URL",
                                value=f"http://orchestrator.{self.ns}.svc.cluster.local",
                            ),
                            client.V1EnvVar(
                                name="IDLE_TIMEOUT_MINUTES",
                                value=str(IDLE_TIMEOUT_MINUTES),
                            ),
                            client.V1EnvVar(name="AGENT_MODEL", value=AGENT_MODEL),
                            client.V1EnvVar(name="AGENT_MODEL_PROVIDER", value=AGENT_MODEL_PROVIDER),
                            client.V1EnvVar(name="OPENAI_API_BASE", value=OPENAI_API_BASE),
                            client.V1EnvVar(name="OPENAI_API_KEY", value=OPENAI_API_KEY),
                            client.V1EnvVar(name="SUB_AGENT_MODEL", value=SUB_AGENT_MODEL),
                            client.V1EnvVar(name="SUB_AGENT_MODEL_PROVIDER", value=SUB_AGENT_MODEL_PROVIDER),
                            client.V1EnvVar(name="SUB_AGENT_API_BASE", value=SUB_AGENT_API_BASE),
                            client.V1EnvVar(name="SUB_AGENT_API_KEY", value=SUB_AGENT_API_KEY),
                            client.V1EnvVar(name="AGENT_DEBUG", value=str(AGENT_DEBUG)),
                            client.V1EnvVar(name="AGENT_DISPLAY_MODE", value=AGENT_DISPLAY_MODE),
                            client.V1EnvVar(name="CWA_API_BASE", value=CWA_API_BASE),
                            client.V1EnvVar(name="CWA_API_KEY", value=CWA_API_KEY),
                            client.V1EnvVar(
                                name="AGENT_DATABASE_URL",
                                value="postgresql://postgres:REDACTED@host.docker.internal:5432/claw_data",
                            ),
                            client.V1EnvVar(
                                name="POD_NAME",
                                value_from=client.V1EnvVarSource(
                                    field_ref=client.V1ObjectFieldSelector(
                                        field_path="metadata.name",
                                    ),
                                ),
                            ),
                            client.V1EnvVar(
                                name="POD_NAMESPACE",
                                value_from=client.V1EnvVarSource(
                                    field_ref=client.V1ObjectFieldSelector(
                                        field_path="metadata.namespace",
                                    ),
                                ),
                            ),
                        ],
                        volume_mounts=volume_mounts,
                        liveness_probe=client.V1Probe(
                            http_get=client.V1HTTPGetAction(
                                path="/health", port=AGENT_PORT,
                            ),
                            initial_delay_seconds=LIVENESS_INITIAL_DELAY,
                            period_seconds=LIVENESS_PERIOD,
                            timeout_seconds=LIVENESS_TIMEOUT,
                            failure_threshold=LIVENESS_FAILURE_THRESHOLD,
                        ),
                        readiness_probe=client.V1Probe(
                            http_get=client.V1HTTPGetAction(
                                path="/readiness", port=AGENT_PORT,
                            ),
                            initial_delay_seconds=READINESS_INITIAL_DELAY,
                            period_seconds=READINESS_PERIOD,
                            timeout_seconds=READINESS_TIMEOUT,
                            failure_threshold=READINESS_FAILURE_THRESHOLD,
                        ),
                        lifecycle=client.V1Lifecycle(
                            pre_stop=client.V1LifecycleHandler(
                                http_get=client.V1HTTPGetAction(
                                    path="/shutdown", port=AGENT_PORT,
                                ),
                            ),
                        ),
                        resources=client.V1ResourceRequirements(
                            requests={
                                "cpu": DEFAULT_CPU_REQUEST,
                                "memory": DEFAULT_MEMORY_REQUEST,
                            },
                            limits={
                                "cpu": DEFAULT_CPU_LIMIT,
                                "memory": DEFAULT_MEMORY_LIMIT,
                            },
                        ),
                    )
                ],
                volumes=volumes,
                restart_policy=POD_RESTART_POLICY,
                termination_grace_period_seconds=TERMINATION_GRACE_PERIOD_SECONDS,
                affinity=client.V1Affinity(
                    pod_anti_affinity=client.V1PodAntiAffinity(
                        preferred_during_scheduling_ignored_during_execution=[
                            client.V1WeightedPodAffinityTerm(
                                weight=50,
                                pod_affinity_term=client.V1PodAffinityTerm(
                                    label_selector=client.V1LabelSelector(
                                        match_expressions=[
                                            client.V1LabelSelectorRequirement(
                                                key="component",
                                                operator="In",
                                                values=["agent"],
                                            ),
                                        ],
                                    ),
                                    topology_key="kubernetes.io/hostname",
                                ),
                            ),
                        ],
                    ),
                ),
            ),
        )
        resp = self.core.create_namespaced_pod(self.ns, body)
        logger.info("Created Pod %s for workspace %s", pod_name, workspace_id)
        return {"name": resp.metadata.name, "status": resp.status.phase}

    def delete_pod(self, pod_name: str) -> None:
        try:
            self.core.delete_namespaced_pod(pod_name, self.ns)
            logger.info("Deleted Pod %s", pod_name)
        except ApiException as e:
            if e.status != 404:
                raise

    # ── Service ───────────────────────────────────────────────────────

    def service_exists(self, service_name: str) -> bool:
        try:
            self.core.read_namespaced_service(service_name, self.ns)
            return True
        except ApiException as e:
            if e.status == 404:
                return False
            raise

    def create_service(
        self, workspace_id: str, service_name: str,
    ) -> dict[str, Any]:
        body = client.V1Service(
            metadata=client.V1ObjectMeta(
                name=service_name,
                namespace=self.ns,
                labels={
                    "workspace_id": workspace_id,
                    "app": "k8s-agent-platform",
                    "component": "agent-service",
                },
                annotations={
                    "created_by": "orchestrator",
                },
            ),
            spec=client.V1ServiceSpec(
                type="ClusterIP",
                selector={
                    "workspace_id": workspace_id,
                    "component": "agent",
                },
                ports=[
                    client.V1ServicePort(
                        name="http", port=80, target_port=AGENT_PORT, protocol="TCP",
                    ),
                ],
                session_affinity="ClientIP",
                session_affinity_config=client.V1SessionAffinityConfig(
                    client_ip=client.V1ClientIPConfig(timeout_seconds=10800),
                ),
            ),
        )
        resp = self.core.create_namespaced_service(self.ns, body)
        logger.info("Created Service %s for workspace %s", service_name, workspace_id)
        return {"name": resp.metadata.name}

    def delete_service(self, service_name: str) -> None:
        try:
            self.core.delete_namespaced_service(service_name, self.ns)
            logger.info("Deleted Service %s", service_name)
        except ApiException as e:
            if e.status != 404:
                raise

    # ── Cleanup Job ────────────────────────────────────────────────

    def create_cleanup_job(
        self,
        workspace_id: str,
        pvc_name: str,
        session_id: str,
    ) -> str:
        """Create a K8s Job to delete a session folder from the workspace PVC.

        The Job mounts the PVC and runs `rm -rf sessions/{session_id}/`.
        Returns the Job name.
        """
        job_name = f"cleanup-{session_id[:20]}-{int(datetime.now(timezone.utc).timestamp())}"
        workspace_path = f"/workspace/{workspace_id}"
        target_path = f"{workspace_path}/sessions/{session_id}"

        body = client.V1Job(
            metadata=client.V1ObjectMeta(
                name=job_name,
                namespace=self.ns,
                labels={
                    "workspace_id": workspace_id,
                    "app": "k8s-agent-platform",
                    "component": "session-cleanup",
                },
            ),
            spec=client.V1JobSpec(
                ttl_seconds_after_finished=CLEANUP_JOB_TTL_SECONDS,
                backoff_limit=2,
                template=client.V1PodTemplateSpec(
                    spec=client.V1PodSpec(
                        containers=[
                            client.V1Container(
                                name="cleanup",
                                image=CLEANUP_JOB_IMAGE,
                                command=["sh", "-c", f"rm -rf {target_path}"],
                                volume_mounts=[
                                    client.V1VolumeMount(
                                        name="workspace",
                                        mount_path=workspace_path,
                                    ),
                                ],
                            ),
                        ],
                        volumes=[
                            client.V1Volume(
                                name="workspace",
                                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                                    claim_name=pvc_name,
                                ),
                            ),
                        ],
                        restart_policy="Never",
                    ),
                ),
            ),
        )
        self.batch.create_namespaced_job(self.ns, body)
        logger.info(
            "Created cleanup Job %s for session %s (workspace=%s)",
            job_name, session_id, workspace_id,
        )
        return job_name
