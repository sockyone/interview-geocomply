"""Platform guardrails for every run namespace: resource limits and network isolation.

The policy belongs to the platform, not to test authors. It is enforced twice:

1. Pre-flight (``check_plan``): every Pod the test will create is computed from the
   real manifests (rendered Helm chart, database, seed and suite Jobs) and checked
   against the policy before anything is deployed, so an oversized test fails fast
   with a readable message instead of a Pod that never schedules.
2. In the cluster (``namespace_manifests``): a LimitRange, a ResourceQuota and
   NetworkPolicies are created in the run namespace before any workload. Kubernetes
   then rejects any Pod that exceeds the limits, even one created outside this tool.

Network isolation: all ingress and egress is denied except traffic between Pods of
the same run namespace, DNS lookups, and scraping from the monitoring namespace.
A load test therefore cannot send traffic to another namespace, another test or
any system outside the cluster, and the application under test cannot reach real
dependencies such as a production payment provider.
"""
from dataclasses import dataclass, field
from decimal import Decimal
import re

import yaml

from perf_runner.database import RUN_LABEL

CPU, MEMORY = 'cpu', 'memory'
_MEMORY_UNITS = {'': 1, 'k': 10**3, 'M': 10**6, 'G': 10**9, 'T': 10**12,
                 'Ki': 2**10, 'Mi': 2**20, 'Gi': 2**30, 'Ti': 2**40}


class GuardrailError(ValueError):
    """The test would exceed the platform resource policy."""


def parse_quantity(value, resource):
    """Return CPU in cores or memory in bytes as a Decimal."""
    text = str(value).strip()
    if resource == CPU:
        match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)(m?)', text)
        if not match:
            raise GuardrailError(f'Unsupported CPU quantity: {value}')
        return Decimal(match[1]) / (1000 if match[2] else 1)
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti|k|M|G|T)?', text)
    if not match:
        raise GuardrailError(f'Unsupported memory quantity: {value}')
    return Decimal(match[1]) * _MEMORY_UNITS[match[2] or '']


def format_quantity(amount, resource):
    if resource == CPU:
        return f'{int(amount * 1000)}m' if amount < 1 or amount % 1 else str(int(amount))
    return f'{int(amount / 2**20)}Mi'


@dataclass(frozen=True)
class Policy:
    """Platform-owned limits. Changing these is a platform decision, not a test setting."""
    # Applied to a container that declares no requests/limits (LimitRange defaults).
    default_request: dict = field(default_factory=lambda: {CPU: '100m', MEMORY: '128Mi'})
    default_limit: dict = field(default_factory=lambda: {CPU: '500m', MEMORY: '512Mi'})
    # The largest single container a test may run.
    container_max: dict = field(default_factory=lambda: {CPU: '2', MEMORY: '2Gi'})
    # Totals for the whole run namespace (every Pod of one test together).
    namespace_requests: dict = field(default_factory=lambda: {CPU: '4', MEMORY: '4Gi'})
    namespace_limits: dict = field(default_factory=lambda: {CPU: '8', MEMORY: '8Gi'})
    max_pods: int = 20
    max_services: int = 10

    def document(self):
        return {'defaultRequest': self.default_request, 'defaultLimit': self.default_limit,
                'containerMax': self.container_max, 'namespaceRequests': self.namespace_requests,
                'namespaceLimits': self.namespace_limits, 'maxPods': self.max_pods,
                'maxServices': self.max_services}


DEFAULT_POLICY = Policy()


# ---------------------------------------------------------------------------- in-cluster objects

def namespace_manifests(run_id, policy=DEFAULT_POLICY, *, monitoring_namespace='monitoring'):
    """LimitRange, ResourceQuota and NetworkPolicies for one run namespace."""
    labels = {RUN_LABEL: run_id, 'app.kubernetes.io/managed-by': 'perf-runner'}

    def meta(name):
        return {'name': name, 'labels': labels}

    limit_range = {'apiVersion': 'v1', 'kind': 'LimitRange', 'metadata': meta('run-limits'),
        'spec': {'limits': [{'type': 'Container', 'default': policy.default_limit,
                             'defaultRequest': policy.default_request, 'max': policy.container_max}]}}
    quota = {'apiVersion': 'v1', 'kind': 'ResourceQuota', 'metadata': meta('run-quota'),
        'spec': {'hard': {
            'requests.cpu': policy.namespace_requests[CPU], 'requests.memory': policy.namespace_requests[MEMORY],
            'limits.cpu': policy.namespace_limits[CPU], 'limits.memory': policy.namespace_limits[MEMORY],
            'pods': str(policy.max_pods), 'services': str(policy.max_services),
            'services.loadbalancers': '0', 'services.nodeports': '0', 'persistentvolumeclaims': '0'}}}

    def network_policy(name, spec):
        return {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': meta(name),
                'spec': {'podSelector': {}, **spec}}

    same_namespace = [{'podSelector': {}}]
    policies = [
        # Everything not explicitly allowed below is dropped, in both directions.
        network_policy('default-deny', {'policyTypes': ['Ingress', 'Egress']}),
        network_policy('allow-same-namespace', {'policyTypes': ['Ingress', 'Egress'],
            'ingress': [{'from': same_namespace}], 'egress': [{'to': same_namespace}]}),
        network_policy('allow-dns', {'policyTypes': ['Egress'], 'egress': [{
            'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
                    'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
            'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]}]}),
        network_policy('allow-monitoring-scrape', {'policyTypes': ['Ingress'], 'ingress': [{
            'from': [{'namespaceSelector': {'matchLabels': {
                'kubernetes.io/metadata.name': monitoring_namespace}}}]}]}),
    ]
    return [limit_range, quota, *policies]


# ---------------------------------------------------------------------------- pre-flight plan

def _container_resources(container, policy):
    """Effective requests/limits after LimitRange defaults (missing limit -> default limit)."""
    resources = container.get('resources') or {}
    limits = {key: (resources.get('limits') or {}).get(key, policy.default_limit[key]) for key in (CPU, MEMORY)}
    requests = {}
    for key in (CPU, MEMORY):
        explicit = (resources.get('requests') or {}).get(key)
        # Kubernetes copies an explicit limit into a missing request before applying defaults.
        requests[key] = explicit or ((resources.get('limits') or {}).get(key) or policy.default_request[key])
    return ({key: parse_quantity(value, key) for key, value in requests.items()},
            {key: parse_quantity(value, key) for key, value in limits.items()})


def pod_plan(component, pod_spec, replicas, policy=DEFAULT_POLICY):
    """One plan entry: per-container values and the Pod's effective totals.

    A Pod's effective value is max(sum of app containers, largest init container),
    which is what scheduling and ResourceQuota use.
    """
    containers = []
    totals = {'requests': {CPU: Decimal(0), MEMORY: Decimal(0)}, 'limits': {CPU: Decimal(0), MEMORY: Decimal(0)}}
    init_max = {'requests': {CPU: Decimal(0), MEMORY: Decimal(0)}, 'limits': {CPU: Decimal(0), MEMORY: Decimal(0)}}
    for kind, items in (('init', pod_spec.get('initContainers') or []), ('main', pod_spec.get('containers') or [])):
        for container in items:
            requests, limits = _container_resources(container, policy)
            containers.append({'name': container['name'], 'init': kind == 'init',
                               'requests': requests, 'limits': limits})
            for category, values in (('requests', requests), ('limits', limits)):
                for key in (CPU, MEMORY):
                    if kind == 'init':
                        init_max[category][key] = max(init_max[category][key], values[key])
                    else:
                        totals[category][key] += values[key]
    effective = {category: {key: max(totals[category][key], init_max[category][key]) for key in (CPU, MEMORY)}
                 for category in totals}
    return {'component': component, 'replicas': int(replicas), 'containers': containers, 'pod': effective}


def rendered_pods(component, manifest_text, policy=DEFAULT_POLICY):
    """Plan entries for Deployments rendered by the supported chart (HPA max counts)."""
    documents = [doc for doc in yaml.safe_load_all(manifest_text) if isinstance(doc, dict)]
    autoscaler = {doc['spec']['scaleTargetRef']['name']: doc['spec']['maxReplicas']
                  for doc in documents if doc.get('kind') == 'HorizontalPodAutoscaler'}
    plans = []
    for doc in documents:
        if doc.get('kind') != 'Deployment':
            continue
        replicas = autoscaler.get(doc['metadata']['name'], doc['spec'].get('replicas', 1))
        plans.append(pod_plan(component, doc['spec']['template']['spec'], replicas, policy))
    if not plans:
        raise GuardrailError(f'No Deployment rendered for {component}')
    return plans


def check_plan(plans, policy=DEFAULT_POLICY):
    """Raise GuardrailError listing every violation; return the totals for the record.

    Totals assume every Pod runs at the same time. That is conservative (the seed Job
    finishes before the workload starts) and keeps the rule easy to reason about.
    """
    problems = []
    container_max = {key: parse_quantity(value, key) for key, value in policy.container_max.items()}
    totals = {'requests': {CPU: Decimal(0), MEMORY: Decimal(0)}, 'limits': {CPU: Decimal(0), MEMORY: Decimal(0)}}
    pods = 0
    for plan in plans:
        for container in plan['containers']:
            for key in (CPU, MEMORY):
                if container['limits'][key] > container_max[key]:
                    problems.append(f"{plan['component']}/{container['name']}: {key} limit "
                                    f"{format_quantity(container['limits'][key], key)} exceeds the per-container "
                                    f"maximum {policy.container_max[key]}")
                if container['requests'][key] > container['limits'][key]:
                    problems.append(f"{plan['component']}/{container['name']}: {key} request exceeds its limit")
        pods += plan['replicas']
        for category in totals:
            for key in (CPU, MEMORY):
                totals[category][key] += plan['pod'][category][key] * plan['replicas']
    caps = {'requests': policy.namespace_requests, 'limits': policy.namespace_limits}
    for category, cap in caps.items():
        for key in (CPU, MEMORY):
            if totals[category][key] > parse_quantity(cap[key], key):
                problems.append(f'All Pods together need {format_quantity(totals[category][key], key)} {key} '
                                f'{category}; the run namespace allows {cap[key]}')
    if pods > policy.max_pods:
        problems.append(f'The test needs {pods} Pods; the run namespace allows {policy.max_pods}')
    if problems:
        raise GuardrailError('Test exceeds platform resource limits:\n  - ' + '\n  - '.join(problems))
    return summary(plans, totals, pods, policy)


def summary(plans, totals, pods, policy):
    return {'policy': policy.document(), 'pods': pods,
            'totals': {category: {key: format_quantity(value, key) for key, value in values.items()}
                       for category, values in totals.items()},
            'components': [{'component': plan['component'], 'replicas': plan['replicas'],
                            'pod': {category: {key: format_quantity(value, key) for key, value in values.items()}
                                    for category, values in plan['pod'].items()}} for plan in plans]}


def plan_test(definition, rendered, policy=DEFAULT_POLICY):
    """Plan every Pod of one test from the same builders the lifecycle uses.

    ``rendered`` maps component name -> ``helm template`` output for the application
    and each supporting service.
    """
    from perf_runner.database import database_resources
    from perf_runner.jobs import seed_job_manifest, suite_job_manifest

    plans = []
    for kind, name, fields in database_resources('preflight', image='postgres', database='db', user='u',
                                                 password='p', url='u'):
        if kind == 'Deployment':
            plans.append(pod_plan('database', fields['spec']['template']['spec'], fields['spec']['replicas'], policy))
    seed = seed_job_manifest(run_id='preflight', image='seed', module='m', args=(), bindings={},
                             outputs={'database.host': 'database', 'database.port': 5432}, timeout_seconds=60)
    suite = suite_job_manifest(run_id='preflight', image='runner', script_config_map='s',
                               profile_config_map_name='p', base_url='http://a', readiness_path='/',
                               request_timeout_seconds=1, deadline_seconds=60,
                               resources=definition.execution.resources)
    for component, job in (('seed', seed), ('suite', suite)):
        plans.append(pod_plan(component, job['spec']['template']['spec'], 1, policy))
    for component, text in rendered.items():
        plans.extend(rendered_pods(component, text, policy))
    return plans
