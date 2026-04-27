"""Storage Service Kubernetes client — PVC CRUD, file operation Jobs, Pod status queries."""

import base64
import logging
import time
from datetime import datetime, timezone
from typing import Any

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from poc.utils.config import (
    CLEANUP_JOB_IMAGE,
    CLEANUP_JOB_TTL_SECONDS,
    DEFAULT_PVC_SIZE_GB,
    DEFAULT_PVC_STORAGE_CLASS,
    K8S_NAMESPACE,
)

logger = logging.getLogger("storage.k8s")


class StorageK8sClient:
    def __init__(self):
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self.core = client.CoreV1Api()
        self.batch = client.BatchV1Api()
        self.ns = K8S_NAMESPACE

    # ── PVC CRUD ─────────────────────────────────────────────────────

    def pvc_exists(self, pvc_name: str) -> bool:
        try:
            self.core.read_namespaced_persistent_volume_claim(pvc_name, self.ns)
            return True
        except ApiException as e:
            if e.status == 404:
                return False
            raise

    def create_pvc(
        self, workspace_id: str, pvc_name: str, size_gb: int = DEFAULT_PVC_SIZE_GB,
        access_mode: str = "ReadWriteOnce",
    ) -> dict[str, Any]:
        body = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(
                name=pvc_name,
                namespace=self.ns,
                labels={
                    "workspace_id": workspace_id,
                    "app": "k8s-agent-platform",
                },
            ),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=[access_mode],
                resources=client.V1VolumeResourceRequirements(
                    requests={"storage": f"{size_gb}Gi"},
                ),
                storage_class_name=DEFAULT_PVC_STORAGE_CLASS,
            ),
        )
        try:
            resp = self.core.create_namespaced_persistent_volume_claim(self.ns, body)
            logger.info("Created PVC %s for workspace %s", pvc_name, workspace_id)
            return {"name": resp.metadata.name, "status": resp.status.phase}
        except ApiException as e:
            if e.status == 409:
                logger.info("PVC %s already exists, reusing", pvc_name)
                return {"name": pvc_name, "status": "exists"}
            raise

    def delete_pvc(self, pvc_name: str) -> None:
        try:
            self.core.delete_namespaced_persistent_volume_claim(pvc_name, self.ns)
            logger.info("Deleted PVC %s", pvc_name)
        except ApiException as e:
            if e.status != 404:
                raise

    def list_pvcs_detail(self) -> list[dict[str, Any]]:
        """List all managed PVCs with detailed info."""
        pvcs = self.core.list_namespaced_persistent_volume_claim(
            self.ns, label_selector="app=k8s-agent-platform",
        )
        return [
            {
                "name": p.metadata.name,
                "workspace_id": p.metadata.labels.get("workspace_id", ""),
                "capacity": (
                    p.status.capacity.get("storage", "")
                    if p.status and p.status.capacity
                    else f"{DEFAULT_PVC_SIZE_GB}Gi"
                ),
                "status": p.status.phase if p.status else "Unknown",
                "storage_class": p.spec.storage_class_name,
                "created_at": (
                    p.metadata.creation_timestamp.isoformat()
                    if p.metadata.creation_timestamp
                    else None
                ),
            }
            for p in pvcs.items
        ]

    # ── Pod status queries ───────────────────────────────────────────

    def pod_is_running(self, pod_name: str) -> bool:
        try:
            pod = self.core.read_namespaced_pod(pod_name, self.ns)
            return pod.status.phase == "Running"
        except ApiException:
            return False

    def get_service_endpoint(self, service_name: str) -> str | None:
        """Get ClusterIP endpoint for a service. Returns host:port or None."""
        try:
            svc = self.core.read_namespaced_service(service_name, self.ns)
            cluster_ip = svc.spec.cluster_ip
            port = svc.spec.ports[0].port if svc.spec.ports else 80
            return f"{cluster_ip}:{port}" if cluster_ip else None
        except ApiException:
            return None

    # ── File operation Jobs ──────────────────────────────────────────

    def create_file_operation_job(
        self,
        workspace_id: str,
        pvc_name: str,
        operation: str,
        path: str,
        content_b64: str | None = None,
    ) -> str:
        """Create a short-lived K8s Job to perform file operations on a PVC.

        Operations: list, read, delete, mkdir
        For write operations, content is passed via base64.
        Returns Job name.
        """
        ts = int(datetime.now(timezone.utc).timestamp())
        job_name = f"storage-op-{operation}-{ts}"
        workspace_path = f"/workspace/{workspace_id}"
        target_path = f"{workspace_path}/{path}".rstrip("/") if path else workspace_path

        commands = {
            "list": f'ls -la "{target_path}" 2>/dev/null || echo "DIR_NOT_FOUND"',
            "read": f'base64 "{target_path}" 2>/dev/null || echo "FILE_NOT_FOUND"',
            "delete": f'rm -rf "{target_path}" && echo "DELETED"',
            "mkdir": f'mkdir -p "{target_path}" && echo "CREATED"',
        }

        if operation == "write" and content_b64:
            parent_dir = "/".join(target_path.rsplit("/", 1)[:-1])
            cmd = (
                f'mkdir -p "{parent_dir}" && '
                f'echo "{content_b64}" | base64 -d > "{target_path}" && '
                f'echo "WRITTEN"'
            )
        elif operation in commands:
            cmd = commands[operation]
        else:
            raise ValueError(f"Unknown operation: {operation}")

        body = client.V1Job(
            metadata=client.V1ObjectMeta(
                name=job_name,
                namespace=self.ns,
                labels={
                    "workspace_id": workspace_id,
                    "app": "k8s-agent-platform",
                    "component": "storage-file-op",
                },
            ),
            spec=client.V1JobSpec(
                ttl_seconds_after_finished=CLEANUP_JOB_TTL_SECONDS,
                backoff_limit=1,
                template=client.V1PodTemplateSpec(
                    spec=client.V1PodSpec(
                        containers=[
                            client.V1Container(
                                name="file-op",
                                image=CLEANUP_JOB_IMAGE,
                                command=["sh", "-c", cmd],
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
        logger.info("Created file-op Job %s (%s on %s)", job_name, operation, pvc_name)
        return job_name

    def wait_for_job_completion(
        self, job_name: str, timeout_seconds: int = 30,
    ) -> str | None:
        """Poll Job until completion and return Pod log. Returns None on timeout."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                job = self.batch.read_namespaced_job(job_name, self.ns)
                if job.status.succeeded and job.status.succeeded > 0:
                    return self._get_job_pod_log(job_name)
                if job.status.failed and job.status.failed > 0:
                    log = self._get_job_pod_log(job_name)
                    logger.error("Job %s failed: %s", job_name, log)
                    return None
            except ApiException:
                pass
            time.sleep(1)

        logger.warning("Job %s timed out after %ds", job_name, timeout_seconds)
        return None

    def _get_job_pod_log(self, job_name: str) -> str:
        """Get log from the Pod created by a Job."""
        try:
            pods = self.core.list_namespaced_pod(
                self.ns, label_selector=f"job-name={job_name}",
            )
            if pods.items:
                pod_name = pods.items[0].metadata.name
                return self.core.read_namespaced_pod_log(pod_name, self.ns)
        except ApiException as e:
            logger.error("Failed to get log for Job %s: %s", job_name, e)
        return ""

    def cleanup_job(self, job_name: str) -> None:
        """Delete a Job and its Pods."""
        try:
            self.batch.delete_namespaced_job(
                job_name, self.ns,
                propagation_policy="Background",
            )
        except ApiException:
            pass
