"""ServiceMonitor & Prometheus CRD converter for helmfile2compose.

Converts kube-prometheus-stack ServiceMonitor CRDs into a Prometheus compose
service with auto-generated scrape configuration. The Prometheus CRD provides
image/version/retention; ServiceMonitors provide scrape targets.
"""

from __future__ import annotations

import fnmatch
import os
import sys

import yaml

from h2c import ConvertResult, Provider


class ServiceMonitorProvider(Provider):
    """Convert Prometheus + ServiceMonitor CRDs to a Prometheus compose service."""

    name = "servicemonitor"
    kinds = ["Prometheus", "ServiceMonitor"]
    priority = 600

    def __init__(self):
        self._prometheus_spec: dict | None = None

    def convert(self, kind: str, manifests: list[dict], ctx) -> ConvertResult:
        if kind == "Prometheus":
            self._index_prometheus(manifests)
            return ConvertResult()
        return self._process_servicemonitors(manifests, ctx)

    # -- Phase 1: Index Prometheus CRD -----------------------------------

    def _index_prometheus(self, manifests: list[dict]) -> None:
        for i, m in enumerate(manifests):
            spec = m.get("spec", {})
            if i == 0:
                self._prometheus_spec = {
                    "image": spec.get("image", ""),
                    "version": spec.get("version", ""),
                    "retention": spec.get("retention", ""),
                }
            else:
                print(
                    f"Warning: ignoring extra Prometheus CR "
                    f"'{m.get('metadata', {}).get('name', '?')}' "
                    f"(only the first is used)",
                    file=sys.stderr,
                )

    # -- Phase 2: Process ServiceMonitors --------------------------------

    def _process_servicemonitors(self, manifests: list[dict], ctx) -> ConvertResult:
        if not manifests:
            return ConvertResult()

        exclude = ctx.config.get("exclude", [])
        scrape_jobs: list[dict] = []
        ca_mounts: list[str] = []

        for m in manifests:
            name = m.get("metadata", {}).get("name", "?")
            spec = m.get("spec", {})

            match_labels = spec.get("selector", {}).get("matchLabels", {})
            if not match_labels:
                ctx.warnings.append(
                    f"ServiceMonitor '{name}': no selector.matchLabels, skipping"
                )
                continue

            # Resolve target: try K8s Service first, then name-based fallback
            target_svc = self._find_service(match_labels, ctx)
            if target_svc is not None:
                svc_name = target_svc["name"]
                compose_name = ctx.alias_map.get(svc_name, svc_name)
            else:
                compose_name = self._fallback_by_name(name, match_labels, ctx)
                if compose_name is None:
                    ctx.warnings.append(
                        f"ServiceMonitor '{name}': no K8s Service matches "
                        f"labels {match_labels}, skipping"
                    )
                    continue
                target_svc = None  # no Service — port resolution limited

            # Skip excluded services
            if _is_excluded(compose_name, exclude):
                continue

            endpoints = spec.get("endpoints", [])
            for idx, ep in enumerate(endpoints):
                job = self._build_scrape_job(
                    name, idx, len(endpoints), ep, target_svc, compose_name, ctx
                )
                if job is None:
                    continue
                scrape_jobs.append(job["job"])
                ca_mounts.extend(job.get("ca_mounts", []))

        if not scrape_jobs:
            ctx.warnings.append("No resolvable ServiceMonitors found")
            return ConvertResult()

        self._write_scrape_config(scrape_jobs, ctx)
        service = self._build_prometheus_service(ca_mounts, ctx)

        # Register Prometheus in services_by_selector so _build_network_aliases
        # generates FQDN aliases. The K8s Service name differs from the compose
        # service name — register both + alias_map entry.
        k8s_svc = self._find_prometheus_k8s_service(ctx)
        if k8s_svc:
            k8s_name = k8s_svc["name"]
            ns = k8s_svc.get("namespace", "")
            ctx.alias_map[k8s_name] = "prometheus"
            if "prometheus" not in ctx.services_by_selector:
                ctx.services_by_selector["prometheus"] = {
                    "name": "prometheus",
                    "namespace": ns,
                    "selector": {},
                    "type": "ClusterIP",
                    "ports": k8s_svc.get("ports", []),
                }

        return ConvertResult(services={"prometheus": service})

    def _write_scrape_config(self, scrape_jobs: list[dict], ctx) -> None:
        """Write prometheus.yml from resolved scrape jobs."""
        prom_config = _render_prometheus_yml(scrape_jobs)
        cm_name = "prometheus-scrape-config"
        cm_dir = os.path.join(ctx.output_dir, "configmaps", cm_name)
        os.makedirs(cm_dir, exist_ok=True)
        with open(os.path.join(cm_dir, "prometheus.yml"), "w", encoding="utf-8") as f:
            f.write(prom_config)
        ctx.generated_cms.add(cm_name)

    def _build_prometheus_service(self, ca_mounts: list[str], ctx) -> dict:
        """Build the Prometheus compose service definition."""
        prom = self._prometheus_spec or {}
        image = prom.get("image", "") or "prom/prometheus"
        version = prom.get("version", "")
        if version and ":" not in image:
            image = f"{image}:v{version.lstrip('v')}"
        elif ":" not in image:
            image = f"{image}:latest"
        retention = prom.get("retention", "") or "15d"

        if not self._prometheus_spec:
            ctx.warnings.append(
                "No Prometheus CR found in manifests — using defaults "
                f"(image={image}, retention={retention})"
            )

        volumes = [
            "./configmaps/prometheus-scrape-config/prometheus.yml:"
            "/etc/prometheus/prometheus.yml:ro",
        ]
        seen = set()
        for mount in ca_mounts:
            if mount not in seen:
                seen.add(mount)
                volumes.append(mount)

        return {
            "image": image,
            "restart": "always",
            "command": [
                "--config.file=/etc/prometheus/prometheus.yml",
                f"--storage.tsdb.retention.time={retention}",
            ],
            "volumes": volumes,
            "ports": ["9090:9090"],
        }

    # -- Helpers ---------------------------------------------------------

    @staticmethod
    def _find_prometheus_k8s_service(ctx) -> dict | None:
        """Find the K8s Service that exposes Prometheus (port 9090)."""
        for svc_name, svc_info in ctx.services_by_selector.items():
            if "prometheus" in svc_name and svc_name != "prometheus":
                ports = svc_info.get("ports", [])
                if any(p.get("port") == 9090 or p.get("targetPort") == 9090
                       for p in ports):
                    return svc_info
        return None

    @staticmethod
    def _find_service(match_labels: dict, ctx) -> dict | None:
        """Find K8s Service whose spec.selector matches the given labels.

        ServiceMonitor selector.matchLabels targets Service metadata.labels,
        which in standard Helm charts are identical to the Service's
        spec.selector (pod labels). We match against spec.selector since
        that's what ctx.services_by_selector indexes.
        """
        for svc_info in ctx.services_by_selector.values():
            selector = svc_info.get("selector", {})
            if not selector:
                continue
            if all(selector.get(k) == v for k, v in match_labels.items()):
                return svc_info
        return None

    @staticmethod
    def _fallback_by_name(sm_name: str, match_labels: dict, ctx) -> str | None:
        """Try to match a ServiceMonitor to a compose service by name.

        When no K8s Service exists in manifests (e.g. keycloak — the Service
        is created at runtime by the K8s operator), check if the SM name or
        a matchLabels value corresponds to a known compose service.
        """
        # Collect candidate names: the SM name + all matchLabels values
        candidates = [sm_name] + list(match_labels.values())
        # Check against alias_map values (compose service names) and keys
        known = set(ctx.alias_map.values()) | set(ctx.alias_map.keys())
        # Also check services_by_selector keys (K8s Service names)
        known |= set(ctx.services_by_selector.keys())
        for candidate in candidates:
            compose_name = ctx.alias_map.get(candidate, candidate)
            if compose_name in known:
                return compose_name
        return None

    @staticmethod
    def _resolve_port(ep: dict, svc_info: dict | None) -> int | str | None:
        """Resolve endpoint port (named or numeric) via the K8s Service.

        If svc_info is None (name-based fallback, no K8s Service available),
        only numeric ports can be resolved.
        """
        port_ref = ep.get("port", "")
        if not port_ref:
            if svc_info is None:
                return None
            ports = svc_info.get("ports", [])
            if ports:
                return ports[0].get("targetPort", ports[0].get("port"))
            return None

        # Numeric port
        if isinstance(port_ref, int):
            return port_ref
        if isinstance(port_ref, str) and port_ref.isdigit():
            return int(port_ref)

        # Named port — need a Service to resolve
        if svc_info is None:
            return None

        for sp in svc_info.get("ports", []):
            if sp.get("name") == port_ref:
                tp = sp.get("targetPort", sp.get("port"))
                # targetPort can itself be a name (references container port name)
                # — only return if numeric
                if isinstance(tp, int):
                    return tp
                if isinstance(tp, str) and tp.isdigit():
                    return int(tp)
                # Named targetPort — can't resolve without pod spec
                return None

        return None

    def _build_scrape_job(
        self,
        sm_name: str,
        ep_idx: int,
        ep_count: int,
        ep: dict,
        svc_info: dict | None,
        compose_name: str,
        ctx,
    ) -> dict | None:
        """Build a single scrape job dict from a ServiceMonitor endpoint."""
        port = self._resolve_port(ep, svc_info)
        if port is None:
            port_ref = ep.get("port", "(none)")
            ctx.warnings.append(
                f"ServiceMonitor '{sm_name}': could not resolve port "
                f"'{port_ref}' to a number, skipping endpoint"
            )
            return None

        job_name = sm_name if ep_count == 1 else f"{sm_name}-{ep_idx}"
        scheme = ep.get("scheme", "http")
        path = ep.get("path", "/metrics")
        interval = ep.get("interval", "30s")

        # Use FQDN target if namespace is available (compose DNS resolves
        # it via network aliases — matches cert SANs for HTTPS)
        svc_info_for_ns = ctx.services_by_selector.get(compose_name, {})
        ns = svc_info_for_ns.get("namespace", "")
        if svc_info is not None:
            ns = svc_info.get("namespace", "") or ns
        if ns:
            target_host = f"{compose_name}.{ns}.svc.cluster.local"
        else:
            target_host = compose_name

        job: dict = {
            "job_name": job_name,
            "metrics_path": path,
            "scrape_interval": interval,
            "scheme": scheme,
            "static_configs": [
                {"targets": [f"{target_host}:{port}"]},
            ],
        }

        ca_mounts: list[str] = []

        if scheme == "https":
            tls_config, ca_mounts = _build_tls_config(
                ep, sm_name, ctx)
            if tls_config:
                job["tls_config"] = tls_config

        return {"job": job, "ca_mounts": ca_mounts}


def _build_tls_config(ep: dict, sm_name: str, ctx) -> tuple[dict, list[str]]:
    """Build Prometheus TLS config and CA mounts from a ServiceMonitor endpoint."""
    tls_cfg = ep.get("tlsConfig", {})
    tls_config: dict = {}
    ca_mounts: list[str] = []

    # CA certificate
    ca_ref = tls_cfg.get("ca", {}).get("configMap", {})
    cm_name = ca_ref.get("name", "")
    cm_key = ca_ref.get("key", "ca-certificates.crt")
    if cm_name and cm_name in ctx.configmaps:
        container_path = f"/etc/prometheus/ca/{cm_name}/{cm_key}"
        tls_config["ca_file"] = container_path
        ca_mounts.append(
            f"./configmaps/{cm_name}/{cm_key}:{container_path}:ro"
        )
    elif cm_name:
        ctx.warnings.append(
            f"ServiceMonitor '{sm_name}': CA configmap '{cm_name}' "
            f"not found — TLS job generated without ca_file"
        )

    # Server name for TLS verification
    server_name = tls_cfg.get("serverName", "")
    if server_name:
        tls_config["server_name"] = server_name

    return tls_config, ca_mounts


def _is_excluded(name: str, exclude: list[str]) -> bool:
    """Check if a service name matches any exclude pattern."""
    return any(fnmatch.fnmatch(name, pat) for pat in exclude)


# -- YAML generation -----------------------------------------------------

def _render_prometheus_yml(scrape_jobs: list[dict]) -> str:
    """Render a minimal prometheus.yml from scrape job dicts."""
    config = {
        "global": {"scrape_interval": "30s"},
        "scrape_configs": scrape_jobs,
    }
    return yaml.dump(config, default_flow_style=False, sort_keys=False)
