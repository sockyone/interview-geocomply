"""Opt-in chart readiness check against the dedicated kind cluster.

Build/load checkout:chart-test first; see platform/charts/http-service/README.md.
Only a generated namespace is created/deleted. No dependency charts required.
"""
import copy
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get('CHECKOUT_KIND_TEST') == '1', 'opt-in kind integration')
class ChartKindTests(unittest.TestCase):
    def test_database_readiness_and_fixed_replicas(self):
        ad = yaml.safe_load((ROOT / 'ApplicationDefinitions/checkout.yaml').read_text())
        selection = ad['spec']['chart']
        chart = ROOT / 'platform/charts' / selection['name']
        metadata = yaml.safe_load((chart / 'Chart.yaml').read_text())
        self.assertEqual(metadata['version'], selection['version'])
        namespace = 'checkout-chart-' + secrets.token_hex(4)
        config = str(ROOT / '.runtime/performance-test/kubeconfig')
        base = ['kubectl', '--kubeconfig', config, '--context', 'kind-performance-test']
        helm = ['helm', '--kubeconfig', config, '--kube-context', 'kind-performance-test', '-n', namespace]

        def kubectl(*args, data=None):
            result = subprocess.run(base + ['-n', namespace] + list(args),
                                    input=json.dumps(data) if data else None,
                                    capture_output=True, text=True, timeout=200)
            if result.returncode:
                # Do not echo submitted Secret manifests or credentials.
                self.fail(result.stderr)
            return result.stdout

        def apply(kind, name, **fields):
            kubectl('apply', '-f', '-', data={'apiVersion': 'apps/v1' if kind == 'Deployment' else
                                            'batch/v1' if kind == 'Job' else 'v1',
                                            'kind': kind, 'metadata': {'name': name}, **fields})

        kubectl('create', 'namespace', namespace)
        try:
            password = secrets.token_urlsafe(24)
            apply('Secret', 'database', stringData={
                'password': password,
                'url': f'postgresql://checkout:{password}@database:5432/checkout',
                'unavailable': f'postgresql://checkout:{password}@127.0.0.1:1/checkout'})
            apply('Service', 'database', spec={'selector': {'app': 'database'},
                                              'ports': [{'port': 5432, 'targetPort': 5432}]})
            apply('Deployment', 'database', spec={
                'replicas': 1, 'selector': {'matchLabels': {'app': 'database'}},
                'template': {'metadata': {'labels': {'app': 'database'}}, 'spec': {
                    'containers': [{'name': 'database', 'image': 'postgres:17.6',
                        'env': [{'name': 'POSTGRES_DB', 'value': 'checkout'},
                                {'name': 'POSTGRES_USER', 'value': 'checkout'},
                                {'name': 'POSTGRES_PASSWORD', 'valueFrom': {'secretKeyRef': {'name': 'database', 'key': 'password'}}}],
                        'readinessProbe': {'exec': {'command': ['pg_isready', '-h', '127.0.0.1', '-U', 'checkout', '-d', 'checkout']}, 'periodSeconds': 2},
                        'volumeMounts': [{'name': 'data', 'mountPath': '/var/lib/postgresql/data'}]}],
                    'volumes': [{'name': 'data', 'emptyDir': {}}]}}})
            kubectl('rollout', 'status', 'deployment/database', '--timeout=150s')
            apply('Job', 'prepare', spec={'backoffLimit': 0, 'activeDeadlineSeconds': 90,
                  'template': {'spec': {'restartPolicy': 'Never', 'containers': [{
                      'name': 'prepare', 'image': 'checkout:chart-test', 'imagePullPolicy': 'Never',
                      'command': ['python', '-m', 'checkout.database', '--seed'],
                      'env': [{'name': 'DATABASE_URL', 'valueFrom': {'secretKeyRef': {'name': 'database', 'key': 'url'}}}]}]}}})
            kubectl('wait', '--for=condition=complete', 'job/prepare', '--timeout=100s')
            with tempfile.TemporaryDirectory() as directory:
                values = Path(directory) / 'values.json'
                for release, key in [('healthy', 'url'), ('unavailable', 'unavailable')]:
                    settings = copy.deepcopy(ad['spec']['values'])
                    settings['image'].update(tag='chart-test', pullPolicy='Never')
                    settings['replicaCount'] = 2
                    settings['secretEnv']['DATABASE_URL'] = {'name': 'database', 'key': key}
                    values.write_text(json.dumps(settings))
                    subprocess.run(helm + ['install', release, str(chart), '-f', str(values)],
                                   check=True, capture_output=True, text=True, timeout=60)
            kubectl('rollout', 'status', 'deployment/healthy-http-service', '--timeout=120s')
            deployment = json.loads(kubectl('get', 'deployment/healthy-http-service', '-o', 'json'))
            self.assertEqual(deployment['status']['readyReplicas'], 2)
            # Wait for the failing Pod to be running and for multiple readiness probes.
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                pods = json.loads(kubectl('get', 'pods', '-l', 'app.kubernetes.io/instance=unavailable', '-o', 'json'))['items']
                if len(pods) == 2 and all(p['status']['phase'] == 'Running' for p in pods):
                    break
                time.sleep(2)
            else:
                self.fail('Unavailable-database Pods never started')
            time.sleep(15)
            pods = json.loads(kubectl('get', 'pods', '-l', 'app.kubernetes.io/instance=unavailable', '-o', 'json'))['items']
            self.assertEqual(len(pods), 2)
            for pod in pods:
                status = pod['status']['containerStatuses'][0]
                self.assertFalse(status['ready'])
                self.assertEqual(status['restartCount'], 0)
                # HTTP process health remains usable while DB readiness fails.
                kubectl('exec', pod['metadata']['name'], '--', 'python', '-c',
                        "import urllib.request; assert urllib.request.urlopen('http://localhost:8080/healthz', timeout=3).status == 200")
            endpoints = json.loads(kubectl('get', 'endpointslices', '-l', 'kubernetes.io/service-name=unavailable-http-service', '-o', 'json'))
            self.assertFalse(any(e.get('conditions', {}).get('ready', False)
                                 for item in endpoints['items'] for e in item.get('endpoints', [])))
        except Exception:
            print(kubectl('get', 'pods,jobs,deployments', '-o', 'wide'))
            print(kubectl('get', 'events', '--field-selector', 'type=Warning'))
            raise
        finally:
            kubectl('delete', 'namespace', namespace, '--wait=true', '--timeout=120s')
