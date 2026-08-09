# TrafficGenerator

TrafficGenerator replays trace traffic against Knative services and then collects
Prometheus metrics for the same run. The normal workflow is one command: prepare
KService aliases if needed, run Locust, collect CPU/pod metrics, aggregate CSVs,
and render plots.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Edit `trafficgen.config.toml` before running. The main settings are:

- `[trace]`: trace file and minute range.
- `[traffic]`: traffic scale.
- `[target]`: service base, namespace, and request path.
- `[ksvc_aliases]`: template and whether to apply computed KService aliases.
- `[metrics.prometheus]`: Prometheus URL and pod/service regex.

## Main Run Command

This is the command normally used:

```bash
source .venv/bin/activate

python -m traffic_generator experiment run \
  --config trafficgen.config.toml \
  --configuration full-nimbus \
  --run-id run-01
```

What it does:

1. Computes the required alias count from peak scaled RPS and assumed busy time.
   Configure `routing.dry_run_assumed_service_time_sec`; if it is unset, the
   runner falls back to `routing.request_timeout_sec` and may overestimate.
2. Checks whether those computed KService aliases already exist.
3. Creates/applies missing aliases when `[ksvc_aliases].enabled = true` and
   `apply = true`, after user confirmation.
4. Runs Locust traffic replay with `MAX_KSVC_ALIASES` set to the computed count,
   so suffix routing waits for an in-range alias instead of calling an uncreated
   KService.
5. Waits for all requests to finish or timeout.
6. Collects Prometheus metrics when available.
7. Writes CSV metrics and plot images into the same run folder. If Locust
   exits nonzero, the runner still writes artifacts from available logs and
   returns the traffic failure code afterward.

## Output Folder

For the command above, all run output is written here:

```text
results/full-nimbus/run-01/
```

Expected files:

```text
requests.jsonl
responses.jsonl
prometheus_samples.jsonl
summary_metrics.csv
timeline.csv
latency_samples.csv
nimbus_tradeoff.png
nimbus_timeline.png
nimbus_latency_distribution.png
```

Check the files with:

```bash
ls -lah results/full-nimbus/run-01
cat results/full-nimbus/run-01/summary_metrics.csv
```

## Useful Checks

Validate config and trace without sending traffic:

```bash
python -m traffic_generator --config trafficgen.config.toml --validate
```

Preview the replay schedule without sending traffic:

```bash
python -m traffic_generator --config trafficgen.config.toml --dry-run --limit 20
```

Estimate how many KService aliases are needed for a chosen busy time:

```bash
python -m traffic_generator \
  --config trafficgen.config.toml \
  --dry-run \
  --limit 0 \
  --assumed-service-time-sec 30
```

Use a realistic p95 request lifetime here. Do not use the request timeout unless
that is truly how long one request keeps a pod busy.

## Recover Metrics For An Existing Run

If a run has `requests.jsonl` and `responses.jsonl` but metrics are missing,
run:

```bash
python -m traffic_generator metrics all \
  --config trafficgen.config.toml \
  --run-dir results/full-nimbus/run-01 \
  --run-id run-01
```

## Notes

- Direct `locust ...` runs only generate request/response logs; they do not run
  the Prometheus metrics pipeline.
- One trace minute is replayed as 60 real seconds.
- CPU and pod lines in `nimbus_timeline.png` need enough Prometheus samples. For
  short tests, use a longer minute range or a smaller `[metrics].step_sec`.
