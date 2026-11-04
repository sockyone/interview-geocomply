"""Render/schema checks using real Helm; no Kubernetes cluster required."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / 'platform/charts/http-service'


class ChartTests(unittest.TestCase):
    def render(self, values=None, valid=True):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'values.json'
            path.write_text(json.dumps(values or {}))
            result = subprocess.run(['helm', 'template', 'example', str(CHART),
                                     '-f', str(path)], capture_output=True, text=True)
        if not valid:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            return result.stderr
        self.assertEqual(result.returncode, 0, result.stderr)
        return {doc['kind']: doc for doc in yaml.safe_load_all(result.stdout) if doc}

    def test_lint(self):
        subprocess.run(['helm', 'lint', str(CHART), '--strict'], check=True,
                       capture_output=True, text=True)

    def test_defaults_and_selectors(self):
        docs = self.render()
        self.assertEqual(set(docs), {'Deployment', 'Service'})
        deployment, service = docs['Deployment'], docs['Service']
        self.assertEqual(deployment['spec']['replicas'], 1)
        pod = deployment['spec']['template']
        self.assertEqual(service['spec']['selector'], pod['metadata']['labels'])
        self.assertEqual(deployment['spec']['selector']['matchLabels'], pod['metadata']['labels'])
        container = pod['spec']['containers'][0]
        self.assertEqual(container['image'], 'registry.example/application:0.1.0')
        self.assertEqual(container['readinessProbe']['httpGet'], {'path': '/', 'port': 'http'})
        self.assertEqual(container['livenessProbe']['httpGet']['path'], '/')
        self.assertEqual(container['resources']['requests']['cpu'], '100m')
        self.assertNotIn('env', container)
        self.assertNotIn('command', container)

    def test_checkout_ad_selects_supported_chart_and_supplies_values(self):
        ad = yaml.safe_load((ROOT / 'ApplicationDefinitions/checkout.yaml').read_text())
        self.assertEqual(ad['apiVersion'], 'perf-runner/v1alpha1')
        self.assertEqual(ad['kind'], 'ApplicationDefinition')
        self.assertEqual(ad['metadata']['name'], 'checkout')
        selection = ad['spec']['chart']
        metadata = yaml.safe_load((CHART / 'Chart.yaml').read_text())
        self.assertEqual(selection, {key: metadata[key] for key in ('name', 'version')})
        docs = self.render(ad['spec']['values'])
        container = docs['Deployment']['spec']['template']['spec']['containers'][0]
        self.assertEqual(container['image'], 'checkout:0.3.0')
        self.assertEqual(container['readinessProbe']['httpGet']['path'], '/readyz')
        self.assertEqual(container['livenessProbe']['httpGet']['path'], '/healthz')
        env = {entry['name']: entry for entry in container['env']}
        self.assertEqual(env['DATABASE_URL']['valueFrom']['secretKeyRef'],
                         {'name': 'checkout-database', 'key': 'url'})

    def test_overrides_and_secret_binding(self):
        digest = 'sha256:' + 'a' * 64
        docs = self.render({'image': {'repository': 'registry.example/team/api', 'digest': digest},
                            'replicaCount': 3, 'port': 9090, 'service': {'port': 80},
                            'command': ['python'], 'args': ['-m', 'example'],
                            'imagePullSecrets': [{'name': 'registry'}],
                            'env': {'PAYMENT_BASE_URL': 'http://stub:8080', 'QUOTED': 'true\nnext'},
                            'secretEnv': {'DATABASE_URL': {'name': 'database', 'key': 'url'}}})
        pod = docs['Deployment']['spec']['template']['spec']
        container = pod['containers'][0]
        self.assertEqual(docs['Deployment']['spec']['replicas'], 3)
        self.assertEqual(container['image'], 'registry.example/team/api@' + digest)
        self.assertEqual(container['ports'][0]['containerPort'], 9090)
        self.assertEqual(docs['Service']['spec']['ports'][0], {'name': 'http', 'port': 80, 'targetPort': 'http'})
        self.assertEqual(container['command'], ['python'])
        self.assertEqual(container['args'], ['-m', 'example'])
        self.assertEqual(pod['imagePullSecrets'], [{'name': 'registry'}])
        env = {item['name']: item for item in container['env']}
        self.assertEqual(env['QUOTED']['value'], 'true\nnext')
        self.assertEqual(env['DATABASE_URL']['valueFrom']['secretKeyRef'], {'name': 'database', 'key': 'url'})

    def test_run_ownership_labels_do_not_change_service_selectors(self):
        docs = self.render({'runId': 'run-1'})
        for kind in ('Deployment', 'Service'):
            self.assertEqual(docs[kind]['metadata']['labels']['perf-runner/run-id'], 'run-1')
        pod = docs['Deployment']['spec']['template']
        self.assertEqual(pod['metadata']['labels']['perf-runner/run-id'], 'run-1')
        self.assertNotIn('perf-runner/run-id', docs['Service']['spec']['selector'])

    def test_hpa_owns_replicas(self):
        docs = self.render({'autoscaling': {'enabled': True, 'minReplicas': 2, 'maxReplicas': 5}})
        self.assertNotIn('replicas', docs['Deployment']['spec'])
        hpa = docs['HorizontalPodAutoscaler']['spec']
        self.assertEqual((hpa['minReplicas'], hpa['maxReplicas']), (2, 5))
        self.assertEqual(hpa['scaleTargetRef']['name'], docs['Deployment']['metadata']['name'])
        self.assertEqual(hpa['metrics'][0]['resource']['target']['averageUtilization'], 70)

    def test_invalid_values(self):
        cases = [
            {'replicaCount': 0}, {'port': 65536}, {'service': {'port': 0}},
            {'replicaCounts': 2}, {'image': {'digest': 'latest'}},
            {'env': {'SETTING': 123}}, {'env': {'bad name': 'value'}},
            {'secretEnv': {'DATABASE_URL': {'name': 'database'}}},
            {'probes': {'readiness': {'timeoutSeconds': 0}}},
            {'autoscaling': {'enabled': 'true'}},
            {'autoscaling': {'enabled': True, 'minReplicas': 5, 'maxReplicas': 2}},
            {'autoscaling': {'enabled': True}, 'resources': {'requests': {'cpu': None}}},
            {'env': {'DATABASE_URL': 'literal'}, 'secretEnv': {'DATABASE_URL': {'name': 'db', 'key': 'url'}}},
        ]
        for values in cases:
            with self.subTest(values=values):
                self.render(values, valid=False)

    def test_packaged_chart_renders(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(['helm', 'package', str(CHART), '-d', directory],
                           check=True, capture_output=True, text=True)
            result = subprocess.run(['helm', 'template', 'packaged', str(Path(directory) / 'http-service-0.1.0.tgz')],
                                    check=True, capture_output=True, text=True)
            self.assertIn('kind: Deployment', result.stdout)
