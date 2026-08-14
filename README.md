# TrafficGenerator

TrafficGenerator replays trace traffic against Knative services, collects
Prometheus metrics for the same run, aggregates CSVs, and renders plots. The
current measurement path is `experiment compare`: it prepares aliases, switches
workload state for Static Knative, Fixed Startup Boost, and Full Nimbus, runs the
same traffic for each case, then writes final comparison CSVs and plots.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Edit `trafficgen.config.toml` before running. The main settings are:

- `[trace]`: trace file and minute range.
- `[traffic]`: global traffic scale and optional random seed.
- `[cluster]`: shared Kubernetes namespace, currently `serverless`.
- `[[services]]`: service base, trace app mapping, request path, KService template, optional `alias_count`, and per-service CPU/Nimbus profile.
- `[system.repos]`: local repo paths for KSCB, Knative Serving, and Nimbus setup automation.
- `[ksvc_aliases]`: whether the runner should generate/apply aliases.
- `[metrics.prometheus]`: Prometheus URL and pod/service regex.

`[target]` is still accepted for old configs, but new configs should use
`[[services]]`.

## Preflight

Validate config and trace without sending traffic:

```bash
python -m traffic_generator --config trafficgen.config.toml --validate
```

Estimate the alias range needed for suffix routing with a chosen busy time:

```bash
python -m traffic_generator \
  --config trafficgen.config.toml \
  --dry-run \
  --limit 0 \
  --assumed-service-time-sec 30
```

Preview the full comparison without applying cluster state or sending traffic:

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

## Automated Measurement

Before running this, make sure the external system pieces are already in the
intended state: KSCB controller installed with `REMOVE_LIMITS=false`, worker
nodes labeled for Nimbus placement, Prometheus reachable, and the Nimbus
process/Knative autoscaler hook available for the Full Nimbus case.

Important: `experiment compare` changes workload-level state only. It does not
build/revert the Knative autoscaler between cases. If you need a strict upstream
Static/Fix baseline with `NIMBUS_DECIDE_URL` unset, use manual single-case runs
or change the autoscaler state outside this command before each case.

```bash
python -m traffic_generator experiment compare \
  --config trafficgen.config.toml \
  --run-id rep-01 \
  --yes \
  --skip-per-run-plot \
  --continue-on-failure
```

What it does:

1. Computes alias demand from peak scaled RPS and assumed busy time.
2. Uses `[[services]].alias_count` when set; otherwise uses the computed count.
3. Optionally applies KSCB/Nimbus CRDs from `[system.repos]`.
4. Prepares KService aliases once, sequentially waiting for Ready and scale-to-zero. Existing aliases are reused; compare does not delete KServices between cases.
5. Applies each case state in order: `static-knative`, `fixed-boost`, `full-nimbus`.
6. Deletes pods only to force a cold-start/scale-to-zero reset; it does not delete KServices.
7. Deletes per-run `Nimbus` and `StartupCPUBoost` CR objects before and after every case, including failed/interrupted cases, so stale policy state does not leak into the next case. The CRDs and controllers are not removed.
8. Runs Locust for each case with the same `run_id` and `MAX_KSVC_ALIASES` cap.
9. Collects Prometheus, aggregates per-run CSVs, then writes final comparison CSVs and plots filtered to that `run_id`; with `--continue-on-failure`, later cases and final plots are still attempted after a traffic failure.

Current limitation: `experiment compare` supports one active `[[services]]` block
because alias preparation is still single-service.

## Generated CR Shape

`fixed-boost` creates one `StartupCPUBoost` CR per alias. The generated shape is:

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

`full-nimbus` creates one `Nimbus` CR per service, following the preload example in
`../nimbus/config/my-boost-preload.yaml`, but with the alias list and service name
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


## Outputs

Each case writes under the shared run folder:

```text
results/rep-01/static-knative/
results/rep-01/fixed-boost/
results/rep-01/full-nimbus/
```

Each run folder should contain:

```text
requests.jsonl
responses.jsonl
prometheus_samples.jsonl
summary_metrics.csv
timeline.csv
latency_samples.csv
```

Final cross-scenario outputs are written inside the shared run folder:

```text
results/rep-01/metrics/summary_metrics.csv
results/rep-01/metrics/timeline.csv
results/rep-01/metrics/latency_samples.csv
results/rep-01/plots/nimbus_scenario_comparison.png
results/rep-01/plots/nimbus_tradeoff.png
results/rep-01/plots/nimbus_timeline.png
results/rep-01/plots/nimbus_timeline_boxplot.png
results/rep-01/plots/nimbus_latency_distribution.png
```

## Manual Fallback

Use `experiment run` when you want to switch cluster state yourself and run one
case only:

```bash
python -m traffic_generator experiment run \
  --config trafficgen.config.toml \
  --configuration full-nimbus \
  --run-id run-01 \
  --yes
```

If existing run folders need only metrics/plots rebuilt:

```bash
python -m traffic_generator metrics all \
  --config trafficgen.config.toml \
  --run-id rep-01
```

## Notes

- Direct `locust ...` runs only generate request/response logs; they do not run
  the Prometheus metrics pipeline.
- One trace minute is replayed as 60 real seconds.
- CPU and pod lines in `nimbus_timeline.png` need enough Prometheus samples. For
  short tests, use a longer minute range or a smaller `[metrics].step_sec`.
