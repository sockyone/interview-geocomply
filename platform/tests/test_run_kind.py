"""Opt-in LoadTestRun lifecycle checks against the real kind cluster and local OCI registry.

Requires the published artifacts (see docs/design/published-definition.md), the registry
reachable from kind nodes, and RUN_KIND_TEST=1. Every case runs the real CLI and removes
only the namespaces created by its own runs.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
KUBECONFIG = ROOT / '.runtime/performance-test/kubeconfig'
CONTEXT = 'kind-performance-test'
RUNS = ROOT / '.runtime/runs'
BASE = yaml.safe_load((ROOT / 'LoadTestDefinitions/checkout.yaml').read_text())


def kubectl(*args):
    return subprocess.run(['kubectl', '--kubeconfig', str(KUBECONFIG), '--context', CONTEXT, *args],
                          capture_output=True, text=True, timeout=60).stdout


@unittest.skipUnless(os.environ.get('RUN_KIND_TEST') == '1', 'opt-in LoadTestRun/kind integration')
class LoadTestRunKindTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.started = []

    def definition(self, mutate=lambda spec: None):
        document = deepcopy(BASE)
        mutate(document['spec'])
        path = Path(self.directory.name) / f'definition-{len(self.started)}.yaml'
        path.write_text(yaml.safe_dump(document))
        return path

    def start(self, mutate=lambda spec: None):
        process = subprocess.Popen([sys.executable, '-m', 'perf_runner.run', 'run', '--definition',
            str(self.definition(mutate)), '--kubeconfig', str(KUBECONFIG), '--context', CONTEXT,
            '--runs-dir', str(RUNS)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.started.append(process)
        self.addCleanup(self.reap, process)
        return process

    def reap(self, process):
        if process.poll() is None:
            process.kill()
        process.communicate()

    def finish(self, process, timeout=420):
        stdout, stderr = process.communicate(timeout=timeout)
        record_line = [line for line in stderr.splitlines() if line.startswith('Record: ')]
        self.assertTrue(record_line, stderr[-2000:])
        path = Path(record_line[0].removeprefix('Record: '))
        return process.returncode, json.loads(path.read_text()), path.parent

    def assert_namespace_gone(self, run_id):
        self.assertEqual(kubectl('get', 'namespace', run_id, '--ignore-not-found', '-o', 'name').strip(), '')

    def test_complete_run_and_concurrent_isolation(self):
        first, second = self.start(), self.start()
        results = [self.finish(first), self.finish(second)]
        for code, record, directory in results:
            self.assertEqual((code, record['status'], record['outcome']['cleanup'], record['outcome']['collection']),
                             (0, 'passed', 'ok', 'ok'), record['errors'])
            self.assertEqual([p['name'] for p in record['phases']],
                             ['validate', 'resolve', 'namespace', 'database', 'services', 'application', 'suite'])
            self.assert_namespace_gone(record['runId'])
            self.assertTrue((directory / 'resolved/resolved.json').is_file())
            self.assertTrue((directory / 'progress.log').is_file())
            progress = [json.loads(line) for line in (directory / 'events.jsonl').read_text().splitlines()]
            self.assertTrue(any(e.get('event') == 'workload-progress' for e in progress))
            self.assertTrue(any(e.get('event') == 'run-complete' for e in progress))
            self.assertIn('[9/11] Removing run resources', (directory / 'progress.log').read_text())
            self.assertTrue((directory / 'diagnostics/events.json').is_file())
            result = json.loads((directory / 'testresult.json').read_text())
            self.assertEqual((result['kind'], result['status']), ('TestResult', 'passed'))
            self.assertEqual([s['name'] for s in result['stages']],
                             [s['name'] for s in BASE['spec']['load']['stages']])
            self.assertIn('suiteRuntime', result['artifacts'])
            self.assertIn('resourceUsage', result)
            self.assertIn('PASSED', (directory / 'report.md').read_text())
        (_, a, a_dir), (_, b, b_dir) = results
        self.assertNotEqual((a['runId'], a['namespace']), (b['runId'], b['namespace']))
        users = [json.loads((d / 'database/result.json').read_text())['outputs']['user'] for d in (a_dir, b_dir)]
        self.assertNotEqual(users[0], users[1])  # independently generated credentials
        self.assertEqual(a['artifacts']['suiteRuntime'], b['artifacts']['suiteRuntime'])
        for _, record, directory in results:
            self.assertNotIn('password', (directory / 'run.json').read_text().lower().replace('"password_secret"', ''))
            for secret_file in directory.rglob('*.log'):
                self.assertNotIn('postgresql://user_', secret_file.read_text())

    def test_threshold_failure_is_distinct_from_execution_failure(self):
        def strict(spec):
            spec['load']['thresholds'][0]['max'] = 0
        code, record, directory = self.finish(self.start(strict))
        self.assertEqual((code, record['status']), (1, 'failed'))
        self.assertEqual((record['outcome']['execution'], record['outcome']['performance']), ('completed', 'failed'))
        self.assertEqual(record['outcome']['cleanup'], 'ok')
        self.assertTrue((directory / 'suite/result.json').is_file())
        report = (directory / 'report.md').read_text()
        self.assertIn('FAILED', report)
        self.assertIn('journeyP95Ms', report)
        result = json.loads((directory / 'testresult.json').read_text())
        self.assertEqual(result['thresholds'][0]['outcome'], 'failed')
        self.assert_namespace_gone(record['runId'])

    def test_seed_failure_retains_diagnostics_and_blocks_later_phases(self):
        def missing_file(spec):
            spec['database']['seed']['args'] = ['--seed-file', 'fixtures/does-not-exist.csv']
        code, record, directory = self.finish(self.start(missing_file))
        self.assertEqual((code, record['status'], record['phase']), (2, 'error', 'database'))
        self.assertNotIn('application', [p['name'] for p in record['phases']])
        self.assertTrue((directory / 'database/seed/seed.log').is_file())
        result = json.loads((directory / 'testresult.json').read_text())
        self.assertEqual((result['status'], result['stages']), ('error', []))
        self.assertTrue(any('performance is unknown' in text for text in result['limitations']))
        self.assertEqual(record['outcome']['cleanup'], 'ok')
        self.assert_namespace_gone(record['runId'])

    def test_seed_deadline_is_enforced(self):
        def hang(spec):
            spec['database']['seed'].update(module='http.server', args=['9999'], timeoutSeconds=5)
        started = time.monotonic()
        code, record, directory = self.finish(self.start(hang))
        self.assertEqual((code, record['status'], record['phase']), (2, 'error', 'database'))
        self.assertIn('DeadlineExceeded', json.dumps(record['phases']))
        self.assertLess(time.monotonic() - started, 240)
        self.assert_namespace_gone(record['runId'])

    def test_application_readiness_timeout(self):
        def never_ready(spec):
            # A valid output, but the wrong one: the app cannot reach its database, so /readyz never passes.
            spec['application']['bindings']['DATABASE_URL'] = 'services.payments.url'
            spec['execution']['applicationReadinessTimeoutSeconds'] = 20
        code, record, directory = self.finish(self.start(never_ready))
        self.assertEqual((code, record['status'], record['phase']), (2, 'error', 'application'))
        self.assertIn('application', [p['name'] for p in record['phases'] if p['outcome'] == 'failed'])
        self.assertEqual(record['outcome']['performance'], 'not-run')
        self.assertTrue((directory / 'diagnostics/resources.json').is_file())
        self.assert_namespace_gone(record['runId'])

    def wait_for_phase(self, process, phase, timeout=240):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.assertIsNone(process.poll(), 'run ended before reaching ' + phase)
            for path in RUNS.glob('lt-*/run.json'):
                try:
                    record = json.loads(path.read_text())
                except ValueError:
                    continue
                if record.get('pid') == process.pid and record.get('phase') == phase:
                    return record
            time.sleep(.3)
        self.fail('Timed out waiting for ' + phase)

    def test_ordinary_cancellation_collects_and_cleans(self):
        process = self.start()
        record = self.wait_for_phase(process, 'suite')
        deadline = time.monotonic() + 60
        while 'job' not in json.loads((RUNS / record['runId'] / 'run.json').read_text()).get('suite', {}):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.3)
        time.sleep(3)  # let the Job pod start
        os.kill(process.pid, signal.SIGTERM)
        code, final, directory = self.finish(process, timeout=240)
        self.assertEqual((code, final['status'], final['outcome']['cleanup']), (130, 'cancelled', 'ok'))
        self.assertTrue((directory / 'diagnostics/resources.json').is_file())
        self.assertEqual(json.loads((directory / 'testresult.json').read_text())['status'], 'cancelled')
        self.assert_namespace_gone(final['runId'])

    def test_hard_kill_is_detected_and_explicitly_cleaned(self):
        process = self.start()
        record = self.wait_for_phase(process, 'database')
        run_id = record['runId']
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        listing = subprocess.run([sys.executable, '-m', 'perf_runner.run', 'status', '--runs-dir', str(RUNS)],
                                 capture_output=True, text=True, check=True).stdout
        self.assertEqual({r['runId']: r['activity'] for r in json.loads(listing)}[run_id], 'interrupted')
        self.assertEqual(kubectl('get', 'namespace', run_id, '-o', 'name').strip(), f'namespace/{run_id}')
        subprocess.run([sys.executable, '-m', 'perf_runner.run', 'cleanup', '--run-id', run_id,
                        '--runs-dir', str(RUNS)], capture_output=True, text=True, check=True)
        self.assert_namespace_gone(run_id)
        final = json.loads((RUNS / run_id / 'run.json').read_text())
        self.assertTrue(final['interrupted'])


if __name__ == '__main__':
    unittest.main()
