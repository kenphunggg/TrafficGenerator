# TrafficGenerator Usage

Copy the commented example config and edit it for the target cluster:

```bash
cp trafficgen.config.example.toml trafficgen.config.toml
```

Validate the config and trace without sending traffic:

```bash
python -m traffic_generator --config trafficgen.config.toml --validate
```

Inspect the first planned requests:

```bash
python -m traffic_generator --config trafficgen.config.toml --dry-run --limit 20
```

Run with Locust using the config file:

```bash
locust -f traffic_generator/locustfile.py --headless -u 1 -r 1 --trafficgen-config trafficgen.config.toml
```

`TRAFFICGEN_CONFIG=trafficgen.config.toml` can be used instead of the Locust
argument. Per-setting environment variables are intended for automation only;
prefer the TOML file for normal runs.
