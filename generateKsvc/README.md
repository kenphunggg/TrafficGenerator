# generateKsvc

Create many Knative Service aliases from one template, then apply them one at a
time for TrafficGenerator suffix routing.

The tool is intended for service names like:

```text
measure-yolo-001
measure-yolo-002
...
measure-yolo-010
```

It can also pre-pull every image used by the template on every configured node
before applying any Knative Service.

For normal TrafficGenerator experiments, configure this through the central
`trafficgen.config.toml` file instead of running this script separately:

```toml
[cluster]
namespace = "serverless"

[[services]]
service_base = "measure-yolo"
ksvc_template = "generateKsvc/templates/measure-yolo.yaml"
alias_count = "auto"

[ksvc_aliases]
enabled = true
output_dir = "generated-ksvc"
pull_images = true
nodes = "generateKsvc/nodes.json"
create_namespace = true
apply = true
```

The experiment runner computes the alias count from peak scaled RPS and assumed
busy time unless `[[services]].alias_count` is set to an integer.

Then run the automated comparison:

```bash
python -m traffic_generator experiment compare \
  --config trafficgen.config.toml \
  --run-id rep-01 \
  --yes \
  --continue-on-failure
```

`experiment compare` prepares aliases once before switching case state. It forces
sequential alias waits even if no-wait flags are present, because the measurement
needs every alias Ready and scaled to zero before traffic starts. The direct
commands below are the manual equivalent of the `[ksvc_aliases]` section.

## Node Inventory

Use `generateKsvc/nodes.json` for the nodes that should pre-pull container
images:

```json
[
  {
    "hostname": "master",
    "username": "thai",
    "ip": "192.168.10.134",
    "sudo_password": ""
  },
  {
    "hostname": "worker",
    "username": "thai",
    "ip": "192.168.10.133",
    "sudo_password": ""
  }
]
```

The script SSHes to each node and runs one of these commands:

```bash
sudo -n crictl pull <image>
```

or, when `sudo_password` is set for that node:

```bash
sudo -S -p '' crictl pull <image>
```

If `sudo_password` is blank, the script requires passwordless sudo. If your user
can run `crictl` without sudo, leave `sudo_password` blank and pass:

```bash
--crictl-command "crictl pull"
```

Storing sudo passwords in JSON is convenient but sensitive. Keep
`generateKsvc/nodes.json` local and do not commit it.

## Generate Only

Render YAML files without applying them:

```bash
python3 generateKsvc/generate_ksvc.py \
  --template generateKsvc/templates/measure-yolo.yaml \
  --count 10 \
  --output-dir generated-ksvc
```

## Pull Images And Apply

Pull template images on all nodes, then apply services one-by-one:

```bash
python3 generateKsvc/generate_ksvc.py \
  --template generateKsvc/templates/measure-yolo.yaml \
  --count 10 \
  --nodes generateKsvc/nodes.json \
  --output-dir generated-ksvc \
  --pull-images \
  --create-namespace \
  --apply
```

Before applying, the script checks that the target namespace exists. If it does
not exist, pass `--create-namespace` or create it manually with
`kubectl create namespace <name>`.

For each generated ksvc, the script applies the YAML, waits for the Ready
condition, then waits until no pods for that ksvc remain before applying the next
one. Generated YAML preserves template annotations. If the template has min scale
1, the script prints a warning and skips scale-to-zero waiting because that
service is expected to keep one pod running.

## Useful Options

Override the base name instead of using `metadata.name` from the template:

```bash
--base-name measure-yolo
```

Use a different suffix format:

```bash
--suffix-template "{base}-{index:03d}"
```

Create the namespace if it does not exist:

```bash
--create-namespace
```

Skip waiting for scale-to-zero:

```bash
--no-wait-scale-zero
```

Print SSH and kubectl commands without running them:

```bash
--dry-run
```

`--dry-run` still writes generated YAML files so you can inspect them.

Disable colored output when saving logs to a plain text file:

```bash
--no-color
```

## Template Notes

Use a Knative Service template such as:

```yaml
apiVersion: serving.knative.dev/v1
kind: Service
metadata:
  name: measure-yolo
  namespace: serverless
spec:
  template:
    metadata:
      annotations:
        autoscaling.knative.dev/min-scale: "0"
        autoscaling.knative.dev/max-scale: "1"
    spec:
      containerConcurrency: 1
      containers:
        - image: docker.io/lazyken/measure-yolo:v1
```

By default, the script keeps every template field and annotation exactly as-is and
changes only `metadata.name`. For example, `measure-yolo-001.yaml` contains:

```yaml
metadata:
  name: measure-yolo-001
```

For TrafficGenerator suffix routing, the generated service names must match
`routing.suffix_template` in `trafficgen.config.toml`. The number of generated
services should match the experiment runner's planned alias count, which is
computed automatically or overridden by `[[services]].alias_count`.
