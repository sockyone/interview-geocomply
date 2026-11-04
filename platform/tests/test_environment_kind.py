"""Opt-in full environment checks on the explicit kind cluster; no load engine."""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from perf_runner.database import RUN_LABEL, KubernetesTarget, RunContext, _save
from perf_runner.environment import EnvironmentPreparationError, load_environment, prepare_environment
from perf_runner.kubernetes import Kubernetes
from perf_runner.services import deploy_payment_stub

ROOT = Path(__file__).resolve().parents[2]


def call(base, method, path, data=None):
    request = Request(base + path, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={'Content-Type': 'application/json'})
    try:
        response = urlopen(request, timeout=10)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


@unittest.skipUnless(os.environ.get('ENVIRONMENT_KIND_TEST') == '1', 'opt-in environment kind integration')
class EnvironmentKindTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chart_temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.chart_temp.cleanup)
        subprocess.run(['helm', 'package', str(ROOT / 'platform/charts/http-service'),
                        '-d', cls.chart_temp.name], check=True, capture_output=True)
        cls.chart = Path(cls.chart_temp.name) / 'http-service-0.1.0.tgz'

    def setUp(self):
        self.run_id = 'environment-check-' + secrets.token_hex(4)
        self.target = KubernetesTarget(ROOT / '.runtime/performance-test/kubeconfig', 'kind-performance-test')
        self.run = RunContext(self.run_id, self.run_id, ROOT / '.runtime/runs' / self.run_id)
        self.cluster = Kubernetes(self.target, self.run)
        self.cluster.command('create', '-f', '-', data={'apiVersion': 'v1', 'kind': 'Namespace',
            'metadata': {'name': self.run_id, 'labels': {RUN_LABEL: self.run_id}}})
        self.namespace_removed = False
        self.addCleanup(self.cleanup_namespace)
        self.db, self.application, self.services, self.bindings = load_environment(ROOT / 'docs/examples/local/environment.yaml')
        self.application['spec']['values']['image']['pullPolicy'] = 'Never'
        self.services = {name: replace(spec, image={**spec.image, 'pullPolicy': 'Never'})
                         for name, spec in self.services.items()}

    def cleanup_namespace(self):
        if not self.namespace_removed:
            from perf_runner.database import _write_private
            self.run.artifacts_dir.mkdir(parents=True, exist_ok=True)
            for release in ('application', 'payments'):
                try:
                    logs = self.cluster.command('logs', f'deployment/{release}-http-service',
                                                '--all-containers=true', '--tail=200', timeout=10)
                    _write_private(self.run.artifacts_dir / f'{release}.log', logs)
                except Exception:
                    pass
            self.cluster.delete_owned_namespace()

    def prepare(self, db=None):
        return prepare_environment(db or self.db, self.application, self.services, self.bindings,
                                  chart=self.chart, target=self.target, run=self.run)

    def test_purchase_payment_modes_and_namespace_isolation(self):
        environment = self.prepare()
        # Confirm this namespace's Pod is scheduled on the named kind cluster.
        nodes = subprocess.run([str(ROOT / '.runtime/bin/kind'), 'get', 'nodes', '--name', 'performance-test'],
                               capture_output=True, text=True, check=True).stdout.splitlines()
        pods = json.loads(self.cluster.command('get', 'pods', '-o', 'json'))['items']
        self.assertEqual(len(pods), 3)
        self.assertTrue(all(pod['spec']['nodeName'] in nodes for pod in pods))
        self.assertTrue(all(pod['metadata']['labels'][RUN_LABEL] == self.run_id for pod in pods))
        evidence = {'run_id': self.run_id, 'kind_nodes': nodes, 'outcomes': {}}
        with self.cluster.port_forward(environment.application.service, environment.application.port,
                                       self.run.artifacts_dir / 'application-forward.log') as base:
            status, draft = call(base, 'POST', '/orders', {'productId': 'coffee', 'quantity': 2})
            self.assertEqual(status, 201)
            path = '/orders/' + draft['id']
            self.assertEqual(call(base, 'PATCH', path, {'quantity': 3})[0], 200)
            status, paid = call(base, 'POST', path + '/checkout', {})
            self.assertEqual((status, paid['status'], paid['amountCents']), (200, 'paid', 1500))
            self.assertEqual(call(base, 'GET', path), (200, paid))
            self.assertEqual(call(base, 'POST', path + '/checkout', {}), (200, paid))
            evidence['outcomes']['approved'] = status
            for outcome, delay, expected in [('declined', 0, 422), ('error', 0, 502), ('approved', 1500, 504)]:
                spec = replace(self.services['payments'], outcome=outcome, delay_ms=delay)
                deploy_payment_stub('payments', spec, chart=self.chart, target=self.target, run=self.run)
                # Probe reads from the API Pod before business traffic after a rollout.
                # Controller readiness can precede kube-proxy endpoint convergence.
                self.cluster.command('exec', f'deployment/{environment.application.deployment}',
                    '--', 'python', '-c', '''import time, urllib.request
url = %r + '/healthz'
deadline = time.monotonic() + 30
while True:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            assert response.status == 200
        break
    except OSError:
        if time.monotonic() >= deadline:
            raise
        time.sleep(.2)
''' % environment.services['payments'].url, timeout=35)
                status, draft = call(base, 'POST', '/orders', {'productId': 'tea', 'quantity': 1})
                self.assertEqual(status, 201)
                path = '/orders/' + draft['id']
                status, body = call(base, 'POST', path + '/checkout', {})
                self.assertEqual(status, expected, body)
                self.assertEqual(call(base, 'GET', path)[1]['status'], 'draft')
                evidence['outcomes']['timeout' if delay else outcome] = status
            # No backend endpoints: a genuinely unavailable payment service.
            self.cluster.command('scale', f'deployment/{environment.services["payments"].deployment}', '--replicas=0')
            self.cluster.command('wait', '--for=delete', 'pod', '-l',
                                 'app.kubernetes.io/instance=payments', '--timeout=60s', timeout=65)
            _, draft = call(base, 'POST', '/orders', {'productId': 'tea', 'quantity': 1})
            status, body = call(base, 'POST', '/orders/' + draft['id'] + '/checkout', {})
            # A Service without backends can reject or drop connections depending
            # on the cluster dataplane. Both retain the documented transport semantics.
            self.assertIn(status, (502, 504), body)
            self.assertEqual(call(base, 'GET', '/orders/' + draft['id'])[1]['status'], 'draft')
            evidence['outcomes']['unavailable'] = status
        # Prove deleting one composed environment leaves another owned namespace intact.
        other = RunContext(self.run_id + '-other', self.run_id + '-other', self.run.artifacts_dir / 'other')
        other_cluster = Kubernetes(self.target, other)
        other_cluster.command('create', '-f', '-', data={'apiVersion': 'v1', 'kind': 'Namespace',
            'metadata': {'name': other.namespace, 'labels': {RUN_LABEL: other.run_id}}})
        self.addCleanup(other_cluster.delete_owned_namespace)
        other_cluster.command('create', 'configmap', 'sentinel', '--from-literal=state=preserved')
        self.cluster.delete_owned_namespace()
        self.namespace_removed = True
        sentinel = json.loads(other_cluster.command('get', 'configmap/sentinel', '-o', 'json'))
        self.assertEqual(sentinel['data']['state'], 'preserved')
        evidence.update(namespace_removed=True, other_namespace_preserved=True)
        _save(self.run.artifacts_dir / 'verification.json', evidence)
        print('Environment evidence:', self.run.artifacts_dir / 'verification.json')

    def test_application_readiness_failure_retains_diagnostics(self):
        from perf_runner.application import ApplicationDeploymentError, deploy_application
        self.application['spec']['values'].update(
            command=['python', '-c', 'import time; time.sleep(60)'], secretEnv={}, env={})
        with self.assertRaises(ApplicationDeploymentError) as error:
            deploy_application(self.application, chart=self.chart, target=self.target,
                               run=self.run, timeout_seconds=5)
        self.assertEqual(error.exception.phase, 'deploy')
        record = json.loads(error.exception.result_path.read_text())
        self.assertEqual(record['state'], 'failed')
        self.assertTrue((error.exception.result_path.parent / 'resources.json').exists())

    def test_seed_failure_blocks_application_and_services(self):
        bad_csv = self.run.artifacts_dir / 'bad.csv'
        bad_csv.parent.mkdir(parents=True)
        bad_csv.write_text('id,name,price_cents\ncoffee,Coffee,-1\n')
        db = replace(self.db, seed=replace(self.db.seed, args=('--seed-file', str(bad_csv))))
        with self.assertRaises(EnvironmentPreparationError) as error:
            self.prepare(db)
        self.assertEqual(error.exception.phase, 'database')
        deployments = json.loads(self.cluster.command('get', 'deployments', '-o', 'json'))['items']
        self.assertEqual([item['metadata']['name'] for item in deployments], ['database'])
        record = json.loads((self.run.artifacts_dir / 'environment/result.json').read_text())
        self.assertEqual(record['state'], 'failed')

    def test_unowned_namespace_cleanup_is_rejected(self):
        self.cluster.command('label', 'namespace', self.run_id, f'{RUN_LABEL}=another-run', '--overwrite')
        try:
            with self.assertRaisesRegex(ValueError, 'not owned'):
                self.cluster.delete_owned_namespace()
            self.assertTrue(self.cluster.command('get', 'namespace', self.run_id, '-o', 'name'))
        finally:
            self.cluster.command('label', 'namespace', self.run_id, f'{RUN_LABEL}={self.run_id}', '--overwrite')
