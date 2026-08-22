# TrafficGenerator Usage

TrafficGenerator uses one central TOML file for normal operation:

```bash
cp trafficgen.config.example.toml trafficgen.config.toml
```

Edit `trafficgen.config.toml` for the trace, global scale, service blocks,
suffix routing, KService alias preparation, system repo paths, Prometheus
metrics, and plot output paths.

## Config Shape

New configs should put service-specific values in `[[services]]`:

```toml
[cluster]
namespace = "serverless"

[traffic]
scale = 0.3

[[services]]
service_base = "measure-yolo"
trace_apps = ["yolo_x_cpu"]
path = "/detect/local"
ksvc_template = "generateKsvc/templates/measure-yolo.yaml"
alias_count = "auto"
cpu.cpu_budget_static = "1000m"
cpu.c_opt_cold = "875m"
cpu.c_opt_warm = "437m"
cpu.c_min_cold = "500m"
cpu.c_min_warm = "300m"
nimbus.node_pool_selector = "nimbus.io/pool=serverless"
nimbus.pre_measured_dir = "../nimbus/results/backup-tuned"

[ksvc_aliases]
enabled = true
apply = true
```

Set `alias_count` to an integer only when you intentionally want to override the
calculator. Otherwise leave it as `"auto"`.

## Validate And Preview

Validate the config and trace without sending traffic:

```bash
python -m traffic_generator --config trafficgen.config.toml --validate
```

Preview the first planned requests:

```bash
python -m traffic_generator --config trafficgen.config.toml --dry-run --limit 20
```

Estimate the alias range needed for suffix routing with a chosen busy time:

```bash
python -m traffic_generator \
  --config trafficgen.config.toml \
  --dry-run \
  --limit 0 \
  --assumed-service-time-sec 30
```

Dry-run the full comparison plan without applying cluster state:

```bash
python -m traffic_generator experiment compare \
  --config trafficgen.config.toml \
  --run-id dry-check \
  --dry-run \
  --skip-crd-apply \
  --skip-alias-prepare \
  --skip-nimbus-service \
  --skip-cluster-capacity-check \
  --no-color
```

## Automated Comparison Run

Use `experiment compare` for the normal three-scenario measurement:

```bash
python -m traffic_generator experiment compare \
  --config trafficgen.config.toml \
  --run-id rep-01 \
  --yes \
  --skip-per-run-plot \
  --continue-on-failure
```

The command prepares the planned aliases once, switches workload state for
`static-knative`, `fixed-boost`, and `full-nimbus`, runs each case with the same
traffic config and run ID, then aggregates/plots only that run ID. Existing
KServices are reused; only pods are deleted to force a cold-start reset. Per-run
`Nimbus` and `StartupCPUBoost` CR objects are deleted before and after every case,
including failed/interrupted cases, so stale policy state does not leak into the
next scenario. With `--continue-on-failure`, later cases and final comparison
plots are still attempted after a traffic failure.

Useful options:

- `--cases static-knative full-nimbus`: run only selected cases in order.
- `--continue-on-failure`: keep running later cases after a failed traffic run.
- `--skip-alias-prepare`: assume aliases already exist.
- `--skip-crd-apply`: do not apply KSCB/Nimbus CRDs from `[system.repos]`.
- `--skip-nimbus-service`: do not apply `nimbus/config/nimbus-service.yaml`.
- `--state-wait-timeout-sec 600`: wait longer for KService readiness, scale-to-zero, and Nimbus apply status.

External prerequisites are still required: this command does not build Knative,
does not start the Nimbus host process, and does not install the KSCB controller.
It also does not rebuild/revert the Knative autoscaler between cases. Prepare
those system-level pieces before measurement, or use manual single-case runs when
you need a strict upstream Static/Fix baseline with `NIMBUS_DECIDE_URL` unset.

## Generated CR Shape

`fixed-boost` creates one `StartupCPUBoost` CR per alias:

```yaml
apiVersion: autoscaling.x-k8s.io/v1alpha1
kind: StartupCPUBoost
metadata:
  name: fixed-boost-measure-yolo-001
  namespace: serverless
selector:
  matchExpressions:
  - key: serving.knative.dev/service
    operator: In
    values: ["measure-yolo-001"]
spec:
  resourcePolicy:
    containerPolicies:
    - containerName: user-container
      fixedResources:
        requests: "875m"
        limits: "875m"
  durationPolicy:
    apiCondition:
      url: "http://measure-yolo-001.serverless.svc.cluster.local/status"
      response: "READY"
```

`full-nimbus` creates one `Nimbus` CR per service. It follows the preload shape
from `../nimbus/config/my-boost-preload.yaml`, with aliases and service name
computed from `trafficgen.config.toml`:

```yaml
apiVersion: lazyken.io/v1alpha1
kind: Nimbus
metadata:
  name: nimbus-measure-yolo
  namespace: serverless
selector:
  matchExpressions:
  - key: serving.knative.dev/service
    operator: In
    values: ["measure-yolo-001", "measure-yolo-002"]
spec:
  placement:
    nodeSelector:
      nimbus.io/pool: serverless
  metric: p95
  acceptableResponseTime:
    cold: 15000
    warm: 1500
  preMeasured:
    loadFromDir: ../nimbus/results/backup-tuned
  online:
    enabled: true
  resourcePolicy:
    containerPolicies:
    - containerName: user-container
      cpuBudget: "1000m"
  durationPolicy:
    coldApiCondition:
      path: /status
      response: READY
    warmApiCondition:
      path: /detect/local
      statusCode: 200
      bodyContains: '"success":true'
```

For the new Nimbus online design, `online.enabled` is the only
TrafficGenerator-set online switch. TrafficGenerator does not emit `policy`,
`placementPolicy`, `reserveRatio`, or adaptive-CPU knobs. Nimbus computes
`adaptive_cpu` cold CPU from the loaded profile and live pool headroom, keeps
the KService `nodeSelector` at the pool selector, and lets Kubernetes choose
the concrete node. `.status.online.assignments[].node` is usually the pool
label value rather than a hostname; `degraded=true` means Nimbus used the
adaptive floor fallback or reported Pending.

## Manual Single Case

Use this only when you want to switch cluster state yourself:

```bash
python -m traffic_generator experiment run \
  --config trafficgen.config.toml \
  --configuration full-nimbus \
  --run-id run-01 \
  --yes
```

## Manual Metrics Pipeline

When logs already exist in `results/<run-id>/<configuration>/`, run the offline
stages manually:

```bash
python -m traffic_generator metrics all \
  --config trafficgen.config.toml \
  --run-id rep-01
```

For one shared run folder only:

```bash
python -m traffic_generator metrics all \
  --config trafficgen.config.toml \
  --run-dir results/rep-01 \
  --run-id rep-01
```

For one case directory only:

```bash
python -m traffic_generator metrics all \
  --config trafficgen.config.toml \
  --run-dir results/rep-01/full-nimbus \
  --run-id rep-01
```

The cross-scenario outputs are written to `results/<run-id>/metrics/` and
`results/<run-id>/plots/` when a run id is supplied. Legacy global folders
`results/metrics/` and `results/plots/` are still used only when no run id is
provided.

## Manual Traffic Replay

Run Locust directly only when you want request/response logs without the offline
metrics pipeline:

```bash
locust \
  -f traffic_generator/locustfile.py \
  --headless \
  -u 1 \
  -r 1 \
  --trafficgen-config trafficgen.config.toml
```

`TRAFFICGEN_CONFIG=trafficgen.config.toml` can be used instead of the Locust
argument. Per-setting environment variables are intended for automation only;
prefer the TOML file for normal runs.
