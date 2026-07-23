# TrafficGenerator User Guide

TrafficGenerator replays minute-level request traces as HTTP POST traffic. It can
read CSV traces, scale request counts, spread arrivals across each real-time
minute, optionally route concurrent requests across suffixed Knative Service
aliases, and write request/response logs.

## Requirements

- Python 3.11 or newer
- Access to the target HTTP services or Kubernetes cluster network
- Python dependencies from `requirements.txt`

Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 1. Create A Config File

Copy the example config:

```bash
cp trafficgen.config.example.toml trafficgen.config.toml
```

Edit `trafficgen.config.toml`.

Minimum important settings:

```toml
[trace]
file = "datatrace/day_night.csv"

[traffic]
scale = 0.3
dry_run = false
# random_seed = 12345

[target]
service_base = "measure-yolo"
namespace = "serverless"
path = "/detect/local"
url_template = "http://{service}.{namespace}.svc.cluster.local{path}"

[routing]
increase_service = true
suffix_template = "{service_base}-{index:03d}"
request_timeout_sec = 300
```

Known trace files:

```text
datatrace/day_night.csv
datatrace/non_station.csv
```

Accepted CSV columns are either:

```csv
minute,function_id,count
```

or:

```csv
minute,app_name,requests_per_minute
```

## 2. Validate The Config

Validate the config and trace without sending traffic:

```bash
python -m traffic_generator --config trafficgen.config.toml --validate
```

This prints the selected config, trace row count, and scheduled request count.

## 3. Preview The Replay Schedule

Print the first planned requests without sending HTTP traffic:

```bash
python -m traffic_generator --config trafficgen.config.toml --dry-run --limit 20
```

Print only the summary:

```bash
python -m traffic_generator --config trafficgen.config.toml --dry-run --limit 0
```

If `routing.increase_service = true`, dry-run can estimate alias reuse with an
assumed service time:

```bash
python -m traffic_generator \
  --config trafficgen.config.toml \
  --dry-run \
  --limit 20 \
  --assumed-service-time-sec 1.0
```

## 4. Run Traffic With Locust

Run the replay in headless mode:

```bash
locust \
  -f traffic_generator/locustfile.py \
  --headless \
  -u 1 \
  -r 1 \
  --trafficgen-config trafficgen.config.toml
```

Use `-u 1 -r 1`. The generator is designed so one Locust user drives the full
trace schedule. Individual HTTP requests are still sent concurrently when their
scheduled times overlap.

You can also select the config with an environment variable:

```bash
TRAFFICGEN_CONFIG=trafficgen.config.toml locust \
  -f traffic_generator/locustfile.py \
  --headless \
  -u 1 \
  -r 1
```

## 5. Check Logs

By default logs are written to:

```text
logs/requests.jsonl
logs/responses.jsonl
```

`requests.jsonl` records each planned/sent request, including request ID, trace
minute, arrival offset, target service, URL, headers, and request body metadata.

`responses.jsonl` records response status, timing, error details, optional
response body, and parsed fields such as `request_id`, `pod_name`, `cold_start`,
and `processing_time_ms` when the service returns them as JSON.

## Common Config Changes

Replay a smaller minute range:

```toml
[trace]
file = "datatrace/day_night.csv"
start_minute = 0
end_minute = 10
```

Scale traffic down to 30%:

```toml
[traffic]
scale = 0.3
```

Make schedule generation repeatable:

```toml
[traffic]
random_seed = 12345
```

Send all requests to one service name without suffix aliases:

```toml
[routing]
increase_service = false
```

Use suffixed service aliases:

```toml
[routing]
increase_service = true
suffix_template = "{service_base}-{index:03d}"
```

This targets services such as:

```text
measure-yolo-001
measure-yolo-002
measure-yolo-003
```

Use a custom JSON request body:

```toml
[request]
body = '{"image":"local"}'
include_request_id_in_body = true
```

Use a JSON body template:

```toml
[request]
body_template_file = "payloads/example.template.json"
```

Template fields include:

```text
{service}
{service_base}
{namespace}
{path}
{function_id}
{minute}
{arrival_offset_sec}
{scheduled_at_sec}
{request_index}
{request_id}
{alias_index}
{alias_decision}
```

## Notes

- One trace minute is always replayed as 60 real seconds.
- The request count for each trace row is scaled with half-up rounding.
- Arrival times inside each minute are randomized while preserving the exact
  scaled request count.
- Only POST requests are supported by the current implementation.
- Running Locust sends real HTTP traffic to the configured target.

## Generate Knative Service Aliases

TrafficGenerator suffix routing expects the target services to exist before the
replay starts. Use `generateKsvc/generate_ksvc.py` to create aliases such as
`measure-yolo-001` through `measure-yolo-010` from one Knative Service template.

Generate files only:

```bash
python generateKsvc/generate_ksvc.py \
  --template generateKsvc/templates/measure-yolo.yaml \
  --count 10 \
  --output-dir generated-ksvc
```

Pull the template image on nodes from `generateKsvc/nodes.json`, then apply each
ksvc one at a time. Generated YAML keeps template fields and annotations as-is
and changes only `metadata.name`. If the template has min scale 1, the script
prints a warning and skips scale-to-zero waiting:

```bash
python generateKsvc/generate_ksvc.py \
  --template generateKsvc/templates/measure-yolo.yaml \
  --count 10 \
  --nodes generateKsvc/nodes.json \
  --output-dir generated-ksvc \
  --pull-images \
  --create-namespace \
  --apply
```

See [generateKsvc/README.md](/home/thai/ken/ken_thesis/TrafficGenerator/generateKsvc/README.md)
for the node JSON format, optional `sudo_password`, and all options.
