# Prometheus Metrics in the Reports Tab — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep gateway and store metrics with history in Prometheus, and draw a few of them in the dashboard's Reports tab, replacing the live-only "WordPress metrics" section.

**Architecture:** Every gateway pod (control plane and edges) exposes `/metrics` on its own port, 9100, which is never behind the Ingress. A Prometheus server (no Grafana, Alertmanager or exporters), installed by Argo, scrapes:
- the gateway pods;
- k3s's Traefik, for storefront traffic per store;
- the kubelet's cAdvisor, for store CPU and memory.

The control gateway gets `GET /api/reports/metrics?range=`. It runs a fixed list of PromQL queries and returns chart series, which `Metrics.jsx` draws with Recharts. The browser never talks to Prometheus.

**Tech Stack:** Python 3.13, Starlette, `prometheus-client` (multiprocess mode, because edges run 2 uvicorn workers), `httpx2`, Helm, the Argo CD Application for `prometheus-community/prometheus` 29.47.0, React and Recharts.

**Spec:** none as a file. The design was agreed in chat on 2026-11-10 and is summarized under Design below.

## Design (agreed in chat)

- Prometheus server only. No Grafana, no alerts, no Alertmanager, kube-state-metrics, node-exporter or pushgateway.
- Charts appear in the Reports tab (admin only), with a 1h / 24h / 7d / 30d selector. Gateways: calls per minute, error rate %, p95 ms. Stores: storefront requests per minute, 5xx per minute, CPU in millicores, memory in MiB.
- Retention: 365 days, capped at 15 GB on a 20 Gi volume. Scrape interval 30 s.
- Per-user, per-team and per-tool history stays in `audit_log` (the existing Reports charts). User IDs and tool names never become Prometheus labels.
- Remove the live metrics-server and Apache-exporter path: `Metrics.jsx`'s 5-minute browser history, `/api/stores/metrics`, `StoreMetrics`, its RBAC, and the egress and NetworkPolicy to port 9117. The Apache sidecar stays in existing stores, unused.
- Store data needs no store-chart change: CPU and memory come from cAdvisor, traffic from Traefik.

## Global Constraints

- Never `git commit`; the user makes all commits. Each task ends with the files left changed, and "Commit" steps are omitted.
- Gateway tests: `cd services/gateway && uv run --frozen pytest -q` with `TEST_DATABASE_URL=postgresql://postgres:t@127.0.0.1:5433/postgres`. Postgres is already running on 5433.
- SQL in the gateway must never contain a literal `%`, because `_pg` rewrites `?` to `%s`. (No new SQL is expected.)
- The metrics port is `9100`. The multiprocess directory is `/tmp/metrics`. The Prometheus URL in prod is `http://prometheus-server.monitoring.svc`. The namespace is `monitoring`. The Prometheus server pod labels are `app.kubernetes.io/name: prometheus` and `app.kubernetes.io/component: server`.
- Metric names: `gateway_tool_calls_total` (counter; labels `gateway`, `server`, `outcome`) and `gateway_tool_call_seconds` (histogram; labels `gateway`, `server`; buckets `0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30`).
- Label values must never come from raw request input. `server` is the matched row's name, or `"unknown"`, or `"gateway"` for built-ins. `gateway` is an already-validated slug.
- Root README edits go in the project-root `README.md`: no links, dashboard URL only.
- GitOps is live: never run `deploy-vps.sh`. The user applies anything on the VPS by hand.

## Review Focus

1. **Two uvicorn workers on an edge:** a scrape must report the sum over both workers, not whichever worker answered. Pinned by `test_multiprocess_counts_add_up_across_processes` (Task 1).
2. **A call for an unknown server or tool:** label `server="unknown"`, never the requested name, so a client can't create series without limit. Pinned by `test_unknown_tool_is_counted_as_unknown_server` (Task 1).
3. **No traffic in the range:** Prometheus returns `NaN` for 0/0 error rates and empty results for no stores. The API returns `null` points and empty series, never a 500. Pinned by `test_nan_and_empty_results` (Task 2).
4. **Prometheus down, or not configured:** 502 and 503 with a readable message, and the rest of the Reports tab still renders. Pinned by `test_prometheus_down_is_502` (Task 2) and by the UI error state (Task 5).
5. **A bad or missing `range`:** missing means `24h`, and an unknown value is 400. Pinned by `test_range_default_and_validation` (Task 2).

---

### Task 1: Gateway instruments and the metrics port

**Files:**
- Create: `services/gateway/app/metrics.py` (instruments, plus `serve()`; Task 2 adds the report handler to the same file)
- Modify: `services/gateway/pyproject.toml` (+ `uv.lock`): add `prometheus-client>=0.21` with `uv add prometheus-client`
- Modify: `services/gateway/app/upstream.py`: record at each exit of `call_tool` (lines 446-450, 460-468, 486-516) and of `_builtin` (around line 585)
- Modify: `services/gateway/app/main.py`: `Settings.metrics_port: int = 0` (env `METRICS_PORT`), and in `lifespan`, `if settings.metrics_port: metrics.serve(settings.metrics_port)`
- Test: `services/gateway/tests/test_metrics.py`

**Interfaces:**
- Produces: `metrics.record_call(gateway: str, server: str, outcome: str, seconds: float) -> None`; `metrics.registry() -> prometheus_client.CollectorRegistry` (a multiprocess-aggregating registry when `PROMETHEUS_MULTIPROC_DIR` is set, otherwise `prometheus_client.REGISTRY`); `metrics.serve(port: int) -> bool` (True if this process bound the port, False on `OSError` because another worker already serves it).
- At import, `metrics.py` creates `PROMETHEUS_MULTIPROC_DIR` with `os.makedirs(..., exist_ok=True)` **before** importing `prometheus_client`.

- [ ] **Step 1: Write the failing tests** in `tests/test_metrics.py`:

```python
def sample(name, **labels):
    from prometheus_client import REGISTRY
    return REGISTRY.get_sample_value(name, labels) or 0

def test_a_call_is_counted_with_its_outcome_and_duration(db_url):
    # Set up like tests/test_gateway.py: one shop server, one gateway "main", bob allowed shop__orders.
    # Call shop__orders once through /api/tools/call as bob.
    before = sample("gateway_tool_calls_total", gateway="main", server="shop", outcome="ok")
    ...
    assert sample("gateway_tool_calls_total", gateway="main", server="shop", outcome="ok") == before + 1
    assert sample("gateway_tool_call_seconds_count", gateway="main", server="shop") >= 1

def test_unknown_tool_is_counted_as_unknown_server(db_url):
    # Call "nosuch__thing" through /api/tools/call.
    assert sample("gateway_tool_calls_total", gateway="main", server="unknown", outcome="denied") >= 1
    assert sample("gateway_tool_calls_total", gateway="main", server="nosuch", outcome="denied") == 0

def test_serve_exposes_text_and_a_second_bind_is_refused():
    port = free_port()  # bind ("127.0.0.1", 0), read the port, close
    assert metrics.serve(port) is True
    assert "gateway_tool_calls_total" in httpx2.get(f"http://127.0.0.1:{port}/metrics").text
    assert metrics.serve(port) is False

def test_multiprocess_counts_add_up_across_processes(tmp_path):
    # Two `python -c` subprocesses with PROMETHEUS_MULTIPROC_DIR=tmp_path each call
    # metrics.record_call("main", "shop", "ok", 0.2); then a third subprocess prints
    # generate_latest(metrics.registry()). Expect the counter's value to be 2.0.
    assert 'gateway_tool_calls_total{gateway="main",outcome="ok",server="shop"} 2.0' in out
```

- [ ] **Step 2: Run `uv run --frozen pytest -q tests/test_metrics.py`.** Expected: FAIL (`app.metrics` doesn't exist).
- [ ] **Step 3: Implement `app/metrics.py`** with the names and buckets from Global Constraints. `serve` calls `prometheus_client.start_http_server(port, registry=registry())` inside `try/except OSError: return False`. In `upstream.py`, call `metrics.record_call` once per call:
  - asked exit: `outcome="asked"`;
  - cancelled: `outcome="cancelled"`, inside the shielded block;
  - normal end: the same expression written to `audit_log.outcome`, recorded before `finish()` runs, so a failed audit update still counts;
  - `_builtin`: `server=approvals.BUILTIN`.
  
  In every case `server = row["name"] if row else "unknown"`, `gateway = gateway_slug`, and `seconds = time.monotonic() - started`.
- [ ] **Step 4: Run the new tests, then the full suite.** Expected: all pass; the previous total was 173.

### Task 2: The Reports metrics API on the control plane

**Files:**
- Modify: `services/gateway/app/metrics.py`: add `RANGES`, `CHARTS`, `report(request)` and `routes`
- Modify: `services/gateway/app/main.py`: `Settings.prometheus_url: str = ""` (env `PROMETHEUS_URL`, with the trailing `/` stripped); add `*metrics.routes` to the control-plane routes
- Test: `services/gateway/tests/test_metrics_report.py`

**Interfaces:**
- Consumes: `require_admin` and `handler`/`HTTPError` from `app/api.py`, as `stores.py` uses them.
- Produces: `GET /api/reports/metrics?range=1h|24h|7d|30d` (default `24h`). It returns `{"range": str, "step": int, "charts": [{"id", "group", "title", "unit", "series": [{"name": str, "points": [[t: int, v: float|null], ...]}]}]}`, with charts in the order of `CHARTS`. Errors: 403 for non-admins, 503 `"metrics are not configured"` when `prometheus_url` is empty, 400 for an unknown range, 502 `"metrics backend unreachable"` on any Prometheus failure.
- Values:
  - `RANGES = {"1h": (3600, 60), "24h": (86400, 600), "7d": (604800, 3600), "30d": (2592000, 14400)}`, each `(seconds, step)`.
  - `end = int(time.time()) // step * step`, `start = end - seconds`.
  - The rate window is `f"{max(step, 120)}s"`.
  - Points round to 2 decimals; `NaN` and `±Inf` become `None`.
  - A series name is its `gateway` label, or its `store` label.
- `CHARTS` (`id`, `group`, `title`, `unit`, `query` with a `{w}` window placeholder):

| id | group | title | unit | query |
|---|---|---|---|---|
| `gw_calls` | Gateways | Tool calls per minute | `/min` | `sum by (gateway) (rate(gateway_tool_calls_total[{w}])) * 60` |
| `gw_errors` | Gateways | Error rate | `%` | `100 * sum by (gateway) (rate(gateway_tool_calls_total{outcome=~"tool_error\|timeout\|unreachable"}[{w}])) / sum by (gateway) (rate(gateway_tool_calls_total[{w}]))` |
| `gw_p95` | Gateways | p95 latency | ` ms` | `1000 * histogram_quantile(0.95, sum by (gateway, le) (rate(gateway_tool_call_seconds_bucket[{w}])))` |
| `st_requests` | Stores | Storefront requests per minute | `/min` | `sum by (store) (label_replace(rate(traefik_service_requests_total{service=~"store-.+-store-wordpress-.+"}[{w}]), "store", "$1", "service", "store-(.+)-store-wordpress-.+")) * 60` |
| `st_5xx` | Stores | Storefront 5xx per minute | `/min` | as `st_requests`, with `code=~"5.."` added to the selector |
| `st_cpu` | Stores | CPU | ` m` | `sum by (store) (label_replace(rate(container_cpu_usage_seconds_total{namespace=~"store-.+", container!="", container!="POD"}[{w}]), "store", "$1", "namespace", "store-(.+)")) * 1000` |
| `st_mem` | Stores | Memory | ` MiB` | `sum by (store) (label_replace(container_memory_working_set_bytes{namespace=~"store-.+", container!="", container!="POD"}, "store", "$1", "namespace", "store-(.+)")) / 1048576` |

(In the table, `\|` is a literal `|` in the query. Python's `.format` needs the braces doubled.)

Queries run concurrently with `asyncio.gather` on one `httpx2.AsyncClient(timeout=10)`, using `GET {prometheus_url}/api/v1/query_range` with `query`, `start`, `end`, `step`.

- [ ] **Step 1: Write the failing tests.** A fake Prometheus is a small Starlette app served with `tests.test_gateway.serve`; it records each request's params and answers `{"status": "success", "data": {"resultType": "matrix", "result": [...]}}`.
  - `test_admin_only_and_needs_configuration`: a member gets 403; an admin with `prometheus_url=""` gets 503.
  - `test_returns_every_chart_in_order`: `[c["id"] for c in charts] == ["gw_calls", "gw_errors", "gw_p95", "st_requests", "st_5xx", "st_cpu", "st_mem"]`; the fake saw 7 queries with `step == "600"`, `int(end) % 600 == 0` and `int(end) - int(start) == 86400`; and a series named `main` with the points `[[t, 1.5]]`.
  - `test_nan_and_empty_results`: the fake answers `"NaN"` for one point and `[]` for the store queries, giving a `None` point and `series == []`.
  - `test_range_default_and_validation`: no `range` gives `"24h"`; `range=2h` gives 400.
  - `test_prometheus_down_is_502`: `prometheus_url="http://127.0.0.1:9"` gives 502.
- [ ] **Step 2: Run them.** Expected: FAIL (route missing).
- [ ] **Step 3: Implement** with the values above.
- [ ] **Step 4: Run the new tests, then the full suite.** Expected: all pass.

### Task 3: Remove the live store-metrics path

**Files:**
- Modify: `services/gateway/app/kube.py`: delete `_CPU`, `_MEM`, `quantity`, `apache_workers` and `StoreMetrics` (lines 149-210), and any imports only they used
- Modify: `services/gateway/app/stores.py`: delete `store_metrics` and its route (lines 67-77, 140); drop `asyncio` if now unused
- Modify: `services/gateway/app/main.py`: drop the `store_metrics` parameter, `app.state.store_metrics` and the `StoreMetrics` import
- Modify: `services/gateway/tests/test_stores.py`: delete `EXPORTER_TEXT`, `test_apache_workers_from_the_exporter` and `test_store_metrics_lists_only_ready_stores`
- Modify: `charts/gateway/templates/rbac.yaml`: delete the `pod-metrics` ClusterRole and ClusterRoleBinding
- Modify: `charts/gateway/templates/networkpolicy.yaml`: delete the control-only egress to WordPress on 9117
- Modify: `services/store-operator/store_operator/manifests.py`: delete `METRICS_PORT` and the `metrics-ingress` policy (lines 128-129, 152-157), and update the `network_policies` docstring if needed
- Modify: `services/store-operator/tests/test_manifests.py`: remove `"metrics-ingress"` from the expected set, and its assertions (lines 33-39)
- Modify: `charts/store/values.yaml:35-37`: the comment now says the sidecar is unused and may be removed in a later store-chart change

- [ ] **Step 1: Make the deletions.** `grep -rn "store_metrics\|StoreMetrics\|apache_workers\|metrics-ingress\|9117" services charts` should print only the store chart's `metrics:` block and the comment.
- [ ] **Step 2: Run the gateway suite and `cd services/store-operator && uv run --frozen pytest -q`.** Expected: both pass. Gateway: Task 2's total minus 2. Operator: 37.
- [ ] **Step 3: Run `helm lint --strict charts/gateway` and `helm template team charts/gateway -f deploy/gateway-values-prod.yaml`.** Expected: no errors, and no `pod-metrics` in the output.

Note for the user: the operator doesn't delete NetworkPolicies it no longer renders, so existing stores keep an unused `metrics-ingress` policy. It's harmless; `kubectl delete networkpolicy metrics-ingress -n store-<name>` removes it.

### Task 4: Charts and the Argo monitoring app

**Files:**
- Modify: `charts/gateway/values.yaml`: add
  ```yaml
  # /metrics on its own port, never behind the Ingress; Prometheus (namespace `namespace`) scrapes it.
  metrics:
    port: 9100
    namespace: monitoring
    # Control only: where /api/reports/metrics queries. Empty: the Reports charts say "not configured".
    prometheusUrl: ""
  ```
- Modify: `charts/gateway/templates/deployment.yaml`:
  - pod annotations `prometheus.io/scrape: "true"` and `prometheus.io/port: "{{ .Values.metrics.port }}"`;
  - container port `metrics` set to `.Values.metrics.port`;
  - env `METRICS_PORT` and `PROMETHEUS_MULTIPROC_DIR=/tmp/metrics` (both modes; `/tmp` is already an emptyDir);
  - env `PROMETHEUS_URL` (control only, when set).
- Modify: `charts/gateway/templates/networkpolicy.yaml`:
  - an ingress rule from `namespaceSelector kubernetes.io/metadata.name: {{ .Values.metrics.namespace }}` plus `podSelector app.kubernetes.io/name: prometheus`, on `{{ .Values.metrics.port }}`;
  - control only: an egress rule to the same namespace and pod selector on TCP 9090.
- Modify: `deploy/gateway-values-prod.yaml`: `metrics: { prometheusUrl: http://prometheus-server.monitoring.svc }`
- Create: `deploy/argocd/apps/monitoring.yaml`

`monitoring.yaml` (exact):
```yaml
# Prometheus server only: history for the dashboard's Reports charts (gateway /metrics,
# Traefik per store, cAdvisor). No Grafana, Alertmanager or exporters.
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: monitoring
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://prometheus-community.github.io/helm-charts
    chart: prometheus
    targetRevision: 29.47.0
    helm:
      releaseName: prometheus
      valuesObject:
        alertmanager: { enabled: false }
        kube-state-metrics: { enabled: false }
        prometheus-node-exporter: { enabled: false }
        prometheus-pushgateway: { enabled: false }
        server:
          global: { scrape_interval: 30s }
          retention: 365d
          retentionSize: 15GB
          persistentVolume: { size: 20Gi }
          resources:
            requests: { cpu: 100m, memory: 300Mi }
            limits: { memory: 1Gi }
        extraManifests:
          - |
            # Only the control gateway may query Prometheus.
            apiVersion: networking.k8s.io/v1
            kind: NetworkPolicy
            metadata: { name: prometheus-server-ingress }
            spec:
              podSelector:
                matchLabels: { app.kubernetes.io/name: prometheus, app.kubernetes.io/component: server }
              policyTypes: [Ingress]
              ingress:
                - from:
                    - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: mercora } }
                      podSelector: { matchLabels: { app.kubernetes.io/name: gateway } }
                  ports: [{ protocol: TCP, port: 9090 }]
  destination:
    server: https://kubernetes.default.svc
    namespace: monitoring
  syncPolicy:
    automated: { prune: true, selfHeal: true }
    syncOptions: [CreateNamespace=true]
```

- [ ] **Step 1: Make the changes.**
- [ ] **Step 2: Verify the charts.** Run `helm lint --strict charts/gateway`, then `helm template team charts/gateway -f deploy/gateway-values-prod.yaml | grep -n "9100\|PROMETHEUS\|prometheus.io"` and `helm template e charts/gateway --set mode=edge --set slug=x | grep -n "PROMETHEUS_URL"`. Expected: the port, annotations and both env vars on control; no `PROMETHEUS_URL` on the edge.
- [ ] **Step 3: Verify the Prometheus values render.** Run `helm template prometheus prometheus --repo https://prometheus-community.github.io/helm-charts --version 29.47.0 -f <(yq '.spec.source.helm.valuesObject' deploy/argocd/apps/monitoring.yaml) | grep -n "retention\|prometheus-server-ingress\|kind: Deployment"`. Expected: `--storage.tsdb.retention.time=365d` and `.size=15GB`, the NetworkPolicy, and exactly one Deployment (the server).

### Task 5: The dashboard's metrics section

**Files:**
- Modify (rewrite): `services/dashboard/src/Metrics.jsx`
- `Reports.jsx` keeps `<Metrics />` where it is.

**Interfaces:**
- Consumes: `api(\`/reports/metrics?range=${range}\`)` from Task 2's response shape.
- Produces: the default export `Metrics()`.

- [ ] **Step 1: Rewrite `Metrics.jsx`.**
  - **Heading:** `<h2>Gateway and store metrics</h2>` with the note "History from Prometheus."
  - **Range selector:** four buttons for `1h`, `24h` (the default), `7d` and `30d`, with `aria-pressed`, in the existing button style. Changing the range refetches; no polling.
  - **Layout:** charts grouped under `<h3>` headings for `Gateways` and `Stores`, in response order, inside the existing `charts` grid. Each chart is a `card` with its title.
  - **Chart component:** a multi-line chart, adapted from the existing `Graph` (same axis, tooltip and theme tokens). Rows come from merging series by `t`, one `<Line>` per series name, colors cycling `var(--chart-1)` to `var(--chart-5)`, `connectNulls={false}`, and the `unit` shown in the tooltip.
  - **X axis:** `HH:MM` for 1h and 24h; day plus `HH:MM` for 7d and 30d.
  - **Empty chart:** a chart with no series shows "No data yet."
  - **Errors:** a 503 shows "Metrics aren't set up: Prometheus isn't configured." Any other error shows its message. Either way the rest of Reports still renders.
- [ ] **Step 2: Build it with `cd services/dashboard && npm run build`.** Expected: the build succeeds and no file imports the old `/stores/metrics`. Check with `grep -rn "stores/metrics" services/dashboard/src`, which should print nothing.

### Task 6: Docs and VPS checks

**Files:**
- Modify: project-root `README.md` section 12 ("Observability metrics", lines 399-405): rewrite it to cover Prometheus (server only, 365 days and 15 GB), the gateway metrics on 9100, Traefik and cAdvisor for stores, and the Reports charts. Use paths without links: `team-mcp-gateway/services/gateway/app/metrics.py`, `team-mcp-gateway/services/dashboard/src/Metrics.jsx`, `team-mcp-gateway/deploy/argocd/apps/monitoring.yaml`.
- Modify: `docs/gitops.md`: a short "Monitoring" section covering what Argo installs, where the data lives (`monitoring` namespace, 20 Gi volume), and the checks below.

- [ ] **Step 1: Write the docs.**
- [ ] **Step 2: Hand the user the VPS checks** to run after the merge deploys, and put them in `docs/gitops.md`:
  1. `kubectl -n monitoring get pods`: the server is Running.
  2. `kubectl -n monitoring port-forward svc/prometheus-server 9090:80`, then open Status → Targets. The gateway pods, `kubernetes-nodes-cadvisor` and Traefik should be up.
  3. Traefik exposes metrics with scrape annotations: `kubectl -n kube-system get pod -l app.kubernetes.io/name=traefik -o jsonpath='{.items[0].metadata.annotations}'` should show `prometheus.io/scrape`.
  4. The store regex matches: in Prometheus, query `traefik_service_requests_total` and check that a store's `service` label looks like `store-<name>-store-wordpress-<port>@kubernetes`. If it doesn't, change the two `st_*` queries in `app/metrics.py`.
  5. The Reports tab shows lines within about 2 minutes of traffic.

---

## Self-review notes

- **Coverage:** every design bullet maps to a task. Removal is Task 3; retention and "no extras" are Task 4; the Reports UI is Task 5.
- **Rulings:**
  - *"Upstreams up" isn't charted.* A gauge per worker in multiprocess mode keeps reporting deleted servers. The Servers page already shows live health from `server_status`.
  - *woo-mcp metrics are dropped.* The user asked for "a few metrics".
  - *The monitoring app takes its values inline (`valuesObject`) instead of a values file.* That avoids an Argo multi-source app. The cost of being wrong is moving them into a file later.
