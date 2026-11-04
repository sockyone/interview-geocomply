"""Export container resource usage for a run namespace and time window from Prometheus.

Queries go through the Kubernetes API service proxy, so no port-forward or ambient
network path is needed. The result states what is missing: a Pod that lived for less
than one scrape interval (for example a fast seed Job) may have no samples at all, and
the last seconds before the export can lag behind the scrape.
"""
from datetime import datetime, timezone
import json
import statistics
from urllib.parse import urlencode

PROMETHEUS = 'http:monitoring-prometheus:9090'
SCRAPE_SECONDS = 5  # matches scrapeInterval / cAdvisorInterval in platform/cluster/monitoring/values.yaml
# The kubelet's cAdvisor refreshes container stats only about every 10-15 s, so that is the real
# sample spacing for CPU/memory however fast Prometheus scrapes. Wait this long after the load.
CADVISOR_REFRESH_SECONDS = 15


def role_for_pod(pod, services):
    """Map generated Pod names to run roles; names follow the runner's fixed resource names."""
    if pod.startswith('database-seed-'):
        return 'seed'
    if pod.startswith('database-'):
        return 'database'
    if pod.startswith('suite-'):
        return 'generator'
    if pod.startswith('application-http-service-'):
        return 'application'
    for name in services:
        if pod.startswith(f'{name}-http-service-'):
            return f'service:{name}'
    return 'other'


def expected_roles(services):
    return ['database', 'seed', 'application', 'generator', *[f'service:{name}' for name in sorted(services)]]


def selector(namespace):
    return f'{{namespace="{namespace}",container!="",container!="POD"}}'


def query_samples(cluster, metric, namespace, end, lookback_seconds):
    """Actual scraped samples (no resampling) for each container series."""
    path = (f'/api/v1/namespaces/monitoring/services/{PROMETHEUS}/proxy/api/v1/query?'
            + urlencode({'query': f'{metric}{selector(namespace)}[{int(lookback_seconds)}s]', 'time': f'{end:.3f}'}))
    response = json.loads(cluster.command('get', '--raw', path, timeout=30))
    if response.get('status') != 'success':
        raise RuntimeError('Prometheus query was not successful')
    return response['data']['result']


def counter_rates(series):
    """CPU seconds counter -> cores between consecutive scrapes; resets and gaps are skipped."""
    converted = []
    for item in series:
        points = [(float(t), float(v)) for t, v in item['values']]
        rates = [[later[0], (later[1] - earlier[1]) / (later[0] - earlier[0])]
                 for earlier, later in zip(points, points[1:])
                 if later[0] > earlier[0] and later[1] >= earlier[1]]
        converted.append({'metric': item['metric'], 'values': [[t, str(v)] for t, v in rates]})
    return converted


def within(series, start, end):
    return [{'metric': item['metric'], 'values': [[t, v] for t, v in item['values'] if start <= float(t) <= end]}
            for item in series]


def _stats(values):
    return {'samples': len(values), 'peak': max(values), 'mean': statistics.fmean(values)} if values else None


def summarize(raw, services):
    """Pure aggregation of Prometheus matrices into per-role peak/mean per metric."""
    roles = {role: {'pods': set(), 'cpuCores': [], 'memoryBytes': []} for role in expected_roles(services)}
    for metric, series in raw.items():
        for item in series:
            role = role_for_pod(item['metric'].get('pod', ''), services)
            entry = roles.setdefault(role, {'pods': set(), 'cpuCores': [], 'memoryBytes': []})
            entry['pods'].add(item['metric'].get('pod', ''))
            entry[metric].append([(float(t), float(v)) for t, v in item['values'] if v not in ('NaN', '+Inf', '-Inf')])
    summary, missing = {}, []
    for role, entry in sorted(roles.items()):
        result = {'pods': sorted(entry['pods'])}
        for metric in ('cpuCores', 'memoryBytes'):
            # Total across a role's containers at each timestamp; peak/mean of that total.
            totals = {}
            for samples in entry[metric]:
                for timestamp, value in samples:
                    totals[timestamp] = totals.get(timestamp, 0.0) + value
            result[metric] = _stats([totals[key] for key in sorted(totals)])
        summary[role] = result
        if result['cpuCores'] is None and result['memoryBytes'] is None:
            missing.append(role)
    return summary, missing


def series_csv(raw, services, origin=None):
    """Flat CSV of every retained Prometheus sample: one row per container sample."""
    rows = ['timestamp_utc,seconds_since_load_start,role,pod,container,metric,value']
    for metric, series in raw.items():
        for item in series:
            labels = item['metric']
            for timestamp, value in item['values']:
                stamp = datetime.fromtimestamp(float(timestamp), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                offset = '' if origin is None else f'{float(timestamp) - origin:.1f}'
                rows.append(','.join([stamp, offset, role_for_pod(labels.get('pod', ''), services),
                                      labels.get('pod', ''), labels.get('container', ''), metric, str(value)]))
    return '\n'.join(rows) + '\n'


def export_resource_usage(cluster, namespace, start, end, services):
    """Return a usage document; Prometheus problems become ``available: False`` with a reason."""
    window = {'start': datetime.fromtimestamp(start).astimezone().isoformat(),
              'end': datetime.fromtimestamp(end).astimezone().isoformat()}
    try:
        lookback = end - start + 30
        cpu = query_samples(cluster, 'container_cpu_usage_seconds_total', namespace, end, lookback)
        memory = query_samples(cluster, 'container_memory_working_set_bytes', namespace, end, lookback)
        raw = {'cpuCores': within(counter_rates(cpu), start, end), 'memoryBytes': within(memory, start, end)}
    except Exception as error:
        return {'available': False, 'reason': f'Prometheus query failed ({type(error).__name__})', 'window': window}, {}
    summary, missing = summarize(raw, services)
    document = {'available': bool(summary) and len(missing) < len(summary), 'source': 'prometheus', 'window': window,
                'namespace': namespace, 'roles': summary, 'missingRoles': missing,
                'notes': [f'CPU is the rate between consecutive samples (cAdvisor refreshes about every {CADVISOR_REFRESH_SECONDS} s); memory is working set. Values are totals across a role\'s containers.',
                          'Pods shorter than one scrape interval, and the final seconds before export, may have no samples.']}
    return document, raw
