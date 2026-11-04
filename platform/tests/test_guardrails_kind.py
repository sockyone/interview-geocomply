"""Opt-in check that the run-namespace guardrails are enforced by a real cluster.

Requires the kind cluster, the local registry with the sample Checkout image, and
GUARDRAILS_KIND_TEST=1. It creates and deletes one namespace of its own.

Network isolation depends on the cluster's CNI enforcing NetworkPolicy (kindnet does).
The probe waits before connecting because kindnet programs a new Pod's rules
asynchronously, within about a second of the Pod starting.
"""
import json
import os
from pathlib import Path
import subprocess
import unittest

from perf_runner.database import RUN_LABEL
from perf_runner.guardrails import namespace_manifests

ROOT = Path(__file__).resolve().parents[2]
KUBECONFIG = ROOT / '.runtime/performance-test/kubeconfig'
CONTEXT = 'kind-performance-test'
IMAGE = os.environ.get('GUARDRAILS_KIND_IMAGE', 'localhost:5001/performance/checkout:0.3.0')
NAMESPACE = 'lt-guardrails-check'

PROBE = '''
import socket, time
time.sleep(10)
for name, host, port in [("same", "target", 8080),
                         ("other-namespace", "monitoring-grafana.monitoring.svc.cluster.local", 80),
                         ("internet", "1.1.1.1", 443), ("kubernetes-api", "kubernetes.default.svc", 443)]:
    try:
        socket.create_connection((host, port), 3).close(); print(name, "open")
    except OSError:
        print(name, "blocked")
'''


def kubectl(*args, data=None, check=True):
    result = subprocess.run(['kubectl', '--kubeconfig', str(KUBECONFIG), '--context', CONTEXT, *args],
                            input=data, capture_output=True, text=True, timeout=150)
    if check and result.returncode:
        raise AssertionError(result.stderr)
    return result


@unittest.skipUnless(os.environ.get('GUARDRAILS_KIND_TEST') == '1', 'opt-in guardrail/kind integration')
class GuardrailsKindTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        kubectl('delete', 'namespace', NAMESPACE, '--ignore-not-found', '--wait=true')
        kubectl('create', 'namespace', NAMESPACE)
        cls.addClassCleanup(kubectl, 'delete', 'namespace', NAMESPACE, '--wait=false', check=False)
        kubectl('label', 'namespace', NAMESPACE, f'{RUN_LABEL}={NAMESPACE}')
        kubectl('-n', NAMESPACE, 'create', '-f', '-', data=json.dumps(
            {'apiVersion': 'v1', 'kind': 'List', 'items': namespace_manifests(NAMESPACE)}))

    def run_pod(self, name, *command, resources=None):
        container = {'name': name, 'image': IMAGE, 'command': list(command)}
        if resources:
            container['resources'] = resources
        return kubectl('-n', NAMESPACE, 'run', name, f'--image={IMAGE}', '--restart=Never', '--labels', f'app={name}',
                       f'--overrides={json.dumps({"spec": {"containers": [container]}})}', check=False)

    def test_traffic_stays_inside_the_namespace(self):
        self.run_pod('target', 'python', '-m', 'http.server', '8080')
        kubectl('-n', NAMESPACE, 'expose', 'pod', 'target', '--port=8080')
        kubectl('-n', NAMESPACE, 'wait', '--for=condition=Ready', 'pod/target', '--timeout=90s')
        self.run_pod('probe', 'python', '-c', PROBE)
        kubectl('-n', NAMESPACE, 'wait', '--for=jsonpath={.status.phase}=Succeeded', 'pod/probe', '--timeout=120s')
        results = dict(line.split() for line in kubectl('-n', NAMESPACE, 'logs', 'probe').stdout.splitlines())
        self.assertEqual(results, {'same': 'open', 'other-namespace': 'blocked', 'internet': 'blocked',
                                   'kubernetes-api': 'blocked'})

    def test_containers_without_resources_get_default_limits(self):
        self.run_pod('defaults', 'sleep', '1')
        pod = json.loads(kubectl('-n', NAMESPACE, 'get', 'pod', 'defaults', '-o', 'json').stdout)
        self.assertEqual(set(pod['spec']['containers'][0]['resources']['limits']), {'cpu', 'memory'})

    def test_oversized_container_and_external_exposure_are_rejected(self):
        result = self.run_pod('big', 'sleep', '1', resources={'limits': {'cpu': '4', 'memory': '1Gi'}})
        self.assertIn('maximum cpu usage per Container is 2', result.stderr)
        result = kubectl('-n', NAMESPACE, 'create', 'service', 'nodeport', 'np', '--tcp=80:80', check=False)
        self.assertIn('exceeded quota', result.stderr)


if __name__ == '__main__':
    unittest.main()
