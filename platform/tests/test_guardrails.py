"""Default (no cluster) checks for namespace guardrails and the pre-flight resource plan."""
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import unittest

import yaml

from perf_runner.definition import load_definition
from perf_runner.guardrails import (DEFAULT_POLICY, GuardrailError, Policy, check_plan, namespace_manifests,
                                    parse_quantity, plan_test, pod_plan, rendered_pods)

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / 'platform/charts/http-service'
DEFINITION = ROOT / 'LoadTestDefinitions/checkout.yaml'


def render(values):
    return subprocess.run(['helm', 'template', 'application', str(CHART), '-f', '-'], input=json.dumps(values),
                          capture_output=True, text=True, check=True).stdout


def app_values(**extra):
    return {'image': {'repository': 'example/app', 'tag': '1'}, 'runId': 'x', **extra}


class QuantityTests(unittest.TestCase):
    def test_cpu_and_memory_units(self):
        self.assertEqual(parse_quantity('250m', 'cpu'), Decimal('0.25'))
        self.assertEqual(parse_quantity('1.5', 'cpu'), Decimal('1.5'))
        self.assertEqual(parse_quantity('512Mi', 'memory'), 512 * 2**20)
        self.assertEqual(parse_quantity('1G', 'memory'), 10**9)
        with self.assertRaises(GuardrailError):
            parse_quantity('lots', 'cpu')


class NamespaceObjectTests(unittest.TestCase):
    def setUp(self):
        self.objects = {(m['kind'], m['metadata']['name']): m for m in namespace_manifests('lt-a-1')}

    def test_every_object_is_owned_by_the_run(self):
        for manifest in self.objects.values():
            self.assertEqual(manifest['metadata']['labels']['perf-runner/run-id'], 'lt-a-1')

    def test_every_container_gets_defaults_and_a_maximum(self):
        limit = self.objects['LimitRange', 'run-limits']['spec']['limits'][0]
        self.assertEqual(limit['type'], 'Container')
        self.assertEqual(set(limit), {'type', 'default', 'defaultRequest', 'max'})

    def test_quota_caps_the_namespace_and_blocks_external_exposure(self):
        hard = self.objects['ResourceQuota', 'run-quota']['spec']['hard']
        for key in ('requests.cpu', 'requests.memory', 'limits.cpu', 'limits.memory', 'pods'):
            self.assertIn(key, hard)
        self.assertEqual((hard['services.loadbalancers'], hard['services.nodeports']), ('0', '0'))

    def test_network_is_default_deny_with_only_namespace_dns_and_monitoring_allowed(self):
        deny = self.objects['NetworkPolicy', 'default-deny']['spec']
        self.assertEqual((deny['podSelector'], deny['policyTypes']), ({}, ['Ingress', 'Egress']))
        self.assertNotIn('ingress', deny)
        self.assertNotIn('egress', deny)
        same = self.objects['NetworkPolicy', 'allow-same-namespace']['spec']
        self.assertEqual(same['egress'], [{'to': [{'podSelector': {}}]}])
        self.assertEqual(same['ingress'], [{'from': [{'podSelector': {}}]}])
        dns = self.objects['NetworkPolicy', 'allow-dns']['spec']['egress'][0]
        self.assertEqual({p['port'] for p in dns['ports']}, {53})
        self.assertEqual(dns['to'][0]['podSelector'], {'matchLabels': {'k8s-app': 'kube-dns'}})
        # No rule anywhere opens egress to an ipBlock (the internet or other networks).
        self.assertNotIn('ipBlock', json.dumps(list(self.objects.values())))


class PlanTests(unittest.TestCase):
    def test_pod_effective_value_is_max_of_init_and_sum_of_containers(self):
        spec = {'initContainers': [{'name': 'init', 'resources': {'limits': {'cpu': '3', 'memory': '64Mi'}}}],
                'containers': [{'name': 'a', 'resources': {'limits': {'cpu': '1', 'memory': '128Mi'}}},
                               {'name': 'b', 'resources': {'limits': {'cpu': '1', 'memory': '128Mi'}}}]}
        plan = pod_plan('x', spec, 1)
        self.assertEqual(plan['pod']['limits'], {'cpu': Decimal(3), 'memory': 256 * 2**20})
        # A limit without a request means request == limit, as Kubernetes defaults it.
        self.assertEqual(plan['pod']['requests']['cpu'], Decimal(3))

    def test_missing_resources_use_the_limitrange_defaults(self):
        plan = pod_plan('x', {'containers': [{'name': 'a'}]}, 1)
        self.assertEqual(plan['containers'][0]['limits']['cpu'], parse_quantity(DEFAULT_POLICY.default_limit['cpu'], 'cpu'))

    def test_sample_test_fits_and_every_component_is_planned(self):
        definition = load_definition(DEFINITION)
        rendered = {'application': render(app_values()), 'payments': render(app_values())}
        summary = check_plan(plan_test(definition, rendered))
        self.assertEqual({c['component'] for c in summary['components']},
                         {'database', 'seed', 'suite', 'application', 'payments'})
        self.assertEqual(summary['pods'], 5)

    def test_every_planned_container_has_explicit_limits(self):
        # Built-in components must never rely on LimitRange defaults.
        definition = load_definition(DEFINITION)
        from perf_runner.database import database_resources
        from perf_runner.jobs import seed_job_manifest, suite_job_manifest
        pods = [fields['spec']['template']['spec'] for kind, _, fields in
                database_resources('r', image='i', database='d', user='u', password='p', url='u') if kind == 'Deployment']
        pods.append(seed_job_manifest(run_id='r', image='i', module='m', args=(), bindings={},
                    outputs={'database.host': 'h', 'database.port': 1}, timeout_seconds=1)['spec']['template']['spec'])
        pods.append(suite_job_manifest(run_id='r', image='i', script_config_map='s', profile_config_map_name='p',
                    base_url='http://a', readiness_path='/', request_timeout_seconds=1, deadline_seconds=1,
                    resources=definition.execution.resources)['spec']['template']['spec'])
        for pod in pods:
            for container in pod.get('initContainers', []) + pod['containers']:
                self.assertEqual(set(container['resources']['limits']), {'cpu', 'memory'}, container['name'])
                self.assertEqual(set(container['resources']['requests']), {'cpu', 'memory'}, container['name'])

    def test_oversized_container_is_rejected_with_a_readable_reason(self):
        big = render(app_values(resources={'requests': {'cpu': '1', 'memory': '1Gi'},
                                           'limits': {'cpu': '4', 'memory': '1Gi'}}))
        with self.assertRaisesRegex(GuardrailError, r'application/application: cpu limit 4 exceeds .* maximum 2'):
            check_plan(rendered_pods('application', big))

    def test_too_many_replicas_exceed_the_namespace(self):
        many = render(app_values(replicaCount=12))
        with self.assertRaisesRegex(GuardrailError, 'Pods together need 12 cpu limits; the run namespace allows 8'):
            check_plan(rendered_pods('application', many))

    def test_autoscaler_maximum_counts_not_the_starting_replicas(self):
        scaled = render(app_values(autoscaling={'enabled': True, 'minReplicas': 1, 'maxReplicas': 25,
                                                'targetCPUUtilizationPercentage': 70}))
        plan = rendered_pods('application', scaled)
        self.assertEqual(plan[0]['replicas'], 25)
        with self.assertRaisesRegex(GuardrailError, 'needs 25 Pods'):
            check_plan(plan)

    def test_policy_is_platform_configurable(self):
        tight = Policy(namespace_limits={'cpu': '500m', 'memory': '8Gi'})
        with self.assertRaisesRegex(GuardrailError, 'allows 500m'):
            check_plan(rendered_pods('application', render(app_values())), tight)


if __name__ == '__main__':
    unittest.main()
