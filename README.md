# h2c-provider-servicemonitor

![vibe coded](https://img.shields.io/badge/vibe-coded-ff69b4)
![python 3](https://img.shields.io/badge/python-3-3776AB)
![heresy: 6/10](https://img.shields.io/badge/heresy-6%2F10-orange)
![pyyaml](https://img.shields.io/badge/dependencies-pyyaml-blue)
![public domain](https://img.shields.io/badge/license-public%20domain-brightgreen)

ServiceMonitor & Prometheus CRD converter for [helmfile2compose](https://github.com/helmfile2compose/h2c-core).

## Handled kinds

- `Prometheus` -- indexes image, version, and retention from the Prometheus CR
- `ServiceMonitor` -- generates Prometheus scrape config and a compose service

## What it does

Replaces the Prometheus Operator's ServiceMonitor reconciliation with a static Prometheus instance and auto-generated scrape configuration. Instead of a Prometheus Operator watching ServiceMonitor CRDs at runtime, this provider resolves them at conversion time and produces a `prometheus.yml` with `static_configs`.

**Prometheus CR:**
- Extracts `spec.image`, `spec.version`, `spec.retention` for the compose service
- Only the first Prometheus CR is used (others are warned and ignored)
- If no Prometheus CR exists, defaults to `prom/prometheus:latest` with 15d retention

**ServiceMonitor CR:**
- Resolves `spec.selector.matchLabels` against K8s Service selectors to find the target service
- Maps the K8s Service name to compose service name via `ctx.alias_map`
- Resolves named ports through the K8s Service `spec.ports[]` to get the target port
- Supports multiple endpoints per ServiceMonitor (suffixed job names)
- For `scheme: https` endpoints, mounts CA bundle ConfigMaps from trust-manager under `/etc/prometheus/ca/`
- Generates `configmaps/prometheus-scrape-config/prometheus.yml` with all resolved scrape jobs

## Priority

`60` -- runs after keycloak (priority 50, which may create Services that ServiceMonitors target) and after trust-manager (priority 20, which provides CA bundle ConfigMaps for TLS-enabled scrape targets).

## Optional companions

- **h2c-converter-cert-manager** + **h2c-converter-trust-manager** -- needed only for HTTPS scrape targets with CA bundle mounting. Not required for HTTP-only scraping.

## Dependencies

- `pyyaml` -- already required by h2c-core. Used to generate `prometheus.yml`.

## What it does NOT handle (v1)

- PrometheusRule / Alertmanager / AlertmanagerConfig
- PodMonitor
- additionalScrapeConfigs
- Grafana datasource auto-wiring (use a `replacement` in `helmfile2compose.yaml` to rewrite `kube-prometheus-stack-prometheus.monitoring` → `prometheus`)

For Grafana setup (k8s-sidecar workaround), see [kube-prometheus-stack workaround](https://helmfile2compose.github.io/maintainer/known-workarounds/kube-prometheus-stack/).

## Usage

Via h2c-manager (recommended):

```bash
python3 h2c-manager.py servicemonitor
```

Manual (all extensions must be in the same directory):

```bash
mkdir -p extensions
cp h2c-converter-cert-manager/cert_manager.py extensions/
cp h2c-converter-trust-manager/trust_manager.py extensions/
cp h2c-provider-servicemonitor/servicemonitor.py extensions/

python3 helmfile2compose.py \
  --extensions-dir ./extensions \
  --helmfile-dir ~/my-platform -e local --output-dir .
```

## License

Public domain.
