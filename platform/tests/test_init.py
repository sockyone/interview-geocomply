"""Check initialization boundaries without Docker or a live Kubernetes cluster."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
STUB = r'''#!/bin/bash
printf '%s|%s|%s\n' "${0##*/}" "$KUBECONFIG" "$*" >> "$CHECK_LOG"
case "${0##*/}" in
  docker) exit 0 ;;
  kind)
    case "$1 $2" in
      'get clusters') printf '%s\n' "${EXISTING_CLUSTER:-}" ;;
      'create cluster'|'export kubeconfig') printf 'test-config\n' > "$KUBECONFIG" ;;
    esac ;;
  helm) exit "${HELM_EXIT:-0}" ;;
  kubectl)
    if [[ "$*" == *'rollout status statefulset/'* ]]; then
      exit "${PROMETHEUS_EXIT:-0}"
    fi ;;
esac
'''


class InitializationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='monitor-init-check-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / 'platform/cluster', self.root / 'platform/cluster')
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        for tool in ('docker', 'kind', 'kubectl', 'helm'):
            path = bin_dir / tool
            path.write_text(STUB)
            path.chmod(0o755)
        self.log = self.root / 'commands.log'
        self.log.touch()
        self.original_config = self.root / 'original-kubeconfig'
        self.original_config.write_text('leave this alone')
        self.env = {
            'PATH': f'{bin_dir}:/usr/bin:/bin',
            'HOME': str(self.root),
            'KUBECONFIG': str(self.original_config),
            'CLUSTER_NAME': 'init-check',
            'CLUSTER_WAIT': '10s',
            'MONITORING_WAIT': '2m',
            'CHECK_LOG': str(self.log),
        }

    def run_script(self, script='init-kind.sh', **extra):
        return subprocess.run(
            ['/bin/bash', str(self.root / 'platform/cluster' / script)],
            env={**self.env, **extra}, capture_output=True, text=True,
        )

    def test_initialization_installs_and_waits_with_isolated_context(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.log.read_text()
        self.assertIn('create cluster --name init-check', commands)
        self.assertIn('--version 89.2.0', commands)
        self.assertIn('--kube-context kind-init-check', commands)
        self.assertIn('--namespace monitoring --create-namespace', commands)
        self.assertIn('wait --for=create statefulset/prometheus-monitoring-prometheus', commands)
        self.assertIn('rollout status statefulset/prometheus-monitoring-prometheus', commands)
        cluster_commands = '\n'.join(
            line for line in commands.splitlines() if not line.startswith('docker|')
        )
        self.assertNotIn(str(self.original_config), cluster_commands)
        self.assertEqual(self.original_config.read_text(), 'leave this alone')
        config = self.root / '.runtime/init-check/kubeconfig'
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_existing_cluster_is_reused_but_monitoring_reconciled(self):
        result = self.run_script(EXISTING_CLUSTER='init-check')
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.log.read_text()
        self.assertIn('export kubeconfig', commands)
        self.assertNotIn('create cluster', commands)
        self.assertIn('upgrade --install monitoring', commands)

    def test_install_failure_is_preserved_without_cleanup_or_success(self):
        result = self.run_script(HELM_EXIT='9')
        self.assertEqual(result.returncode, 9)
        self.assertNotIn('Cluster and monitoring ready', result.stdout)
        self.assertNotIn('delete cluster', self.log.read_text())

    def test_prometheus_readiness_failure_is_not_reported_as_success(self):
        result = self.run_script(PROMETHEUS_EXIT='7')
        self.assertEqual(result.returncode, 7)
        self.assertNotIn('Cluster and monitoring ready', result.stdout)

    def test_standalone_monitoring_requires_our_cluster_config(self):
        result = self.run_script('init-monitoring.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('kubeconfig is missing', result.stderr)
        self.assertEqual(self.log.read_text(), '')

    def test_invalid_timeout_fails_before_creating_cluster(self):
        result = self.run_script(MONITORING_WAIT='0s')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.log.read_text(), '')


if __name__ == '__main__':
    unittest.main()
