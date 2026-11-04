"""Default (no cluster) checks for LoadTestRun semantics, Job manifests and cleanup safety."""
import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from perf_runner import run as run_module
from perf_runner.database import KubernetesTarget
from perf_runner.definition import load_definition
from perf_runner.jobs import seed_job_manifest, suite_job_manifest, wait_for_job
from perf_runner.run import LoadTestRun, classify, cleanup_abandoned, make_run_id, parse_suite_result, run_activity

ROOT = Path(__file__).resolve().parents[2]
DEFINITION = ROOT / 'LoadTestDefinitions/checkout.yaml'


class FakeCluster:
    def __init__(self, fail=()):
        self.commands, self.fail = [], set(fail)

    def command(self, *args, data=None, timeout=30):
        self.commands.append(args)
        for word in self.fail:
            if word in args:
                raise RuntimeError(f'injected {word} failure')
        if args[:2] == ('get', 'namespace') and '--ignore-not-found' in args:
            return ''
        return '{"items": []}'

    def delete_owned_namespace(self, timeout_seconds=120):
        self.commands.append(('delete-owned-namespace', timeout_seconds))
        if 'delete' in self.fail:
            raise RuntimeError('injected delete failure')

    def assert_owned_namespace(self):
        pass


RESULT = {'schemaVersion': 'v1alpha1', 'runId': 'x', 'outcome': 'passed',
          'executionOutcome': 'completed', 'performanceOutcome': 'passed'}


class ClassifyTests(unittest.TestCase):
    def test_primary_outcome_is_never_overwritten(self):
        failed = {**RESULT, 'outcome': 'failed', 'performanceOutcome': 'failed'}
        self.assertEqual(classify(primary=None, result=RESULT, job_state='complete', collection='ok', cleanup='ok'),
                         ('passed', 'passed'))
        self.assertEqual(classify(primary=None, result=failed, job_state='failed', collection='failed', cleanup='failed'),
                         ('failed', 'failed'))
        self.assertEqual(classify(primary='error', result=None, job_state=None, collection='ok', cleanup='failed'),
                         ('error', 'error'))
        self.assertEqual(classify(primary='cancelled', result=RESULT, job_state='complete', collection='ok', cleanup='ok'),
                         ('cancelled', 'cancelled'))

    def test_collection_or_cleanup_failure_is_not_success(self):
        for collection, cleanup in (('failed', 'ok'), ('ok', 'failed')):
            self.assertEqual(classify(primary=None, result=RESULT, job_state='complete',
                                      collection=collection, cleanup=cleanup), ('passed', 'passed-with-errors'))

    def test_incomplete_or_missing_results_cannot_pass(self):
        for result in (None, {**RESULT, 'executionOutcome': 'failed'}, {**RESULT, 'performanceOutcome': 'incomplete'},
                       {**RESULT, 'outcome': 'failed'}):
            self.assertEqual(classify(primary=None, result=result, job_state='complete', collection='ok',
                                      cleanup='ok')[1], 'error')
        # A Job that exited non-zero cannot pass even when a result says it did.
        self.assertEqual(classify(primary=None, result=RESULT, job_state='failed', collection='ok', cleanup='ok')[1],
                         'error')

    def test_result_parsing_requires_exactly_one_matching_document(self):
        line = json.dumps({'schemaVersion': 'v1alpha1', 'runId': 'a'})
        self.assertEqual(parse_suite_result('noise\n' + line + '\n', 'a')['runId'], 'a')
        self.assertIsNone(parse_suite_result(line + '\n' + line, 'a'))
        self.assertIsNone(parse_suite_result(line, 'b'))

    def test_run_ids_are_unique_dns_labels(self):
        ids = {make_run_id('Checkout_Baseline!!') for _ in range(50)}
        self.assertEqual(len(ids), 50)
        self.assertTrue(all(len(i) <= 63 and i.startswith('lt-checkout-baseline-') for i in ids))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        kubeconfig = self.directory / 'kubeconfig'
        kubeconfig.write_text('apiVersion: v1\n')
        self.target = KubernetesTarget(kubeconfig, 'kind-test')

    def make(self, cluster=None, **kwargs):
        run = LoadTestRun(load_definition(DEFINITION), target=self.target, runs_dir=self.directory / 'runs', **kwargs)
        run.cluster = cluster or FakeCluster()
        return run

    def scripted(self, run, body, result=None, job_state='complete'):
        def provision():
            run.namespace_created = True
            body()
            run.suite_result, run.job_state = result, job_state
        run.provision_and_run = provision
        return run

    def test_passing_run_is_cleaned_and_recorded(self):
        run = self.scripted(self.make(), lambda: None, {**RESULT, 'runId': 'x'})
        record = run.execute()
        self.assertEqual((record['status'], record['exitCode'], record['state']), ('passed', 0, 'finished'))
        self.assertIn(('delete-owned-namespace', 120), run.cluster.commands)
        self.assertEqual(json.loads((run.directory / 'run.json').read_text())['status'], 'passed')
        self.assertEqual(oct((run.directory / 'run.json').stat().st_mode & 0o777), '0o600')

    def test_phase_failure_collects_cleans_and_reports_error(self):
        def fail():
            raise RuntimeError('database never became ready')
        run = self.scripted(self.make(), fail)
        record = run.execute()
        self.assertEqual((record['status'], record['exitCode']), ('error', 2))
        self.assertEqual(record['outcome']['cleanup'], 'ok')
        self.assertTrue((run.directory / 'diagnostics' / 'resources.json').exists())
        self.assertIn(('delete-owned-namespace', 120), run.cluster.commands)

    def test_cleanup_failure_cannot_overwrite_test_failure(self):
        failed = {**RESULT, 'outcome': 'failed', 'performanceOutcome': 'failed'}
        run = self.scripted(self.make(FakeCluster(fail=['delete'])), lambda: None, failed, 'failed')
        record = run.execute()
        self.assertEqual((record['status'], record['outcome']['test'], record['outcome']['cleanup']),
                         ('failed', 'failed', 'failed'))
        self.assertEqual(record['exitCode'], 1)
        self.assertIn(run.run_id, record['cleanupHint'])

    def test_cleanup_or_collection_failure_after_pass_is_nonzero(self):
        run = self.scripted(self.make(FakeCluster(fail=['delete'])), lambda: None, RESULT)
        record = run.execute()
        self.assertEqual((record['status'], record['exitCode']), ('passed-with-errors', 3))
        run = self.scripted(self.make(FakeCluster(fail=['events'])), lambda: None, RESULT)
        record = run.execute()
        self.assertEqual((record['status'], record['outcome']['collection'], record['exitCode']),
                         ('passed-with-errors', 'failed', 3))

    def test_cancellation_still_collects_and_cleans(self):
        def cancel():
            raise KeyboardInterrupt('Cancelled by signal 15')
        run = self.scripted(self.make(), cancel)
        record = run.execute()
        self.assertEqual((record['status'], record['exitCode'], record['outcome']['cleanup']), ('cancelled', 130, 'ok'))
        self.assertIn(('delete-owned-namespace', 120), run.cluster.commands)

    def test_no_namespace_means_no_cluster_deletion(self):
        def fail():
            raise ValueError('definition rejected')
        run = self.make()
        run.provision_and_run = fail
        record = run.execute()
        self.assertEqual((record['status'], record['outcome']['cleanup']), ('error', 'skipped'))
        self.assertFalse([c for c in run.cluster.commands if c[0] == 'delete-owned-namespace'])

    def test_generated_secrets_are_redacted_everywhere(self):
        def provision():
            run.redactions.append('s3cr3t-value')
            (run.directory / 'diagnostics.log').write_text('url=postgresql://u:s3cr3t-value@db/x')
            raise RuntimeError('failed with s3cr3t-value')
        run = self.scripted(self.make(), provision)
        run.provision_and_run = provision
        run.namespace_created = True
        record = run.execute()
        retained = (run.directory / 'diagnostics.log').read_text() + (run.directory / 'run.json').read_text()
        self.assertNotIn('s3cr3t-value', retained)
        self.assertIn('[REDACTED]', retained)
        self.assertEqual(record['status'], 'error')

    def test_two_runs_get_distinct_identity_and_paths(self):
        first, second = self.make(), self.make()
        self.assertNotEqual((first.run_id, first.directory), (second.run_id, second.directory))

    def test_existing_run_directory_is_never_reused(self):
        run = self.scripted(self.make(run_id='lt-fixed-0001'), lambda: None, RESULT)
        run.execute()
        with self.assertRaises(FileExistsError):
            self.scripted(self.make(run_id='lt-fixed-0001'), lambda: None, RESULT).execute()


class InterruptionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runs = Path(self.temporary.name)
        self.kubeconfig = self.runs / 'kubeconfig'
        self.kubeconfig.write_text('apiVersion: v1\n')

    def write(self, state='running'):
        directory = self.runs / 'lt-abandoned-0001'
        directory.mkdir()
        (directory / 'run.json').write_text(json.dumps({'runId': 'lt-abandoned-0001', 'namespace': 'lt-abandoned-0001',
            'state': state, 'phase': 'database', 'target': {'context': 'kind-test', 'kubeconfig': str(self.kubeconfig)}}))
        (directory / 'run.lock').write_text('')
        return directory

    def test_dead_run_is_reported_interrupted_and_live_run_active(self):
        directory = self.write()
        self.assertEqual(run_activity(self.runs, 'lt-abandoned-0001'), 'interrupted')
        holder = os.open(directory / 'run.lock', os.O_RDWR)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(run_activity(self.runs, 'lt-abandoned-0001'), 'active')
        with self.assertRaisesRegex(ValueError, 'still active'):
            cleanup_abandoned(self.runs, 'lt-abandoned-0001')

    def test_explicit_cleanup_deletes_only_through_the_ownership_check(self):
        directory = self.write()
        fake = mock.MagicMock()
        fake.command.return_value = 'namespace/lt-abandoned-0001'
        with mock.patch.object(run_module, 'Kubernetes', return_value=fake):
            record = cleanup_abandoned(self.runs, 'lt-abandoned-0001')
        fake.delete_owned_namespace.assert_called_once_with(120)
        self.assertTrue(record['interrupted'])
        self.assertEqual(json.loads((directory / 'run.json').read_text())['state'], 'finished')
        fake.delete_owned_namespace.side_effect = ValueError('Namespace is not owned by this active run')
        with mock.patch.object(run_module, 'Kubernetes', return_value=fake):
            with self.assertRaisesRegex(ValueError, 'not owned'):
                cleanup_abandoned(self.runs, 'lt-abandoned-0001')

    def test_cleanup_rejects_unsafe_run_ids(self):
        for value in ('../etc', 'default', 'lt-', 'lt-a/b'):
            with self.assertRaises(ValueError):
                cleanup_abandoned(self.runs, value)


class JobTests(unittest.TestCase):
    def test_suite_job_is_bounded_readonly_and_secret_free(self):
        job = suite_job_manifest(run_id='lt-a-1', image='registry/python-suite@sha256:' + 'a' * 64,
            script_config_map='scripts-123', profile_config_map_name='load-profile', base_url='http://app:8080',
            readiness_path='/readyz', request_timeout_seconds=3, deadline_seconds=90,
            resources={'requests': {'cpu': '200m', 'memory': '256Mi'}, 'limits': {'cpu': '1', 'memory': '512Mi'}})
        spec = job['spec']
        self.assertEqual((spec['backoffLimit'], spec['activeDeadlineSeconds']), (0, 90))
        pod = spec['template']['spec']
        self.assertEqual(pod['restartPolicy'], 'Never')
        self.assertFalse(pod['automountServiceAccountToken'])
        container = pod['containers'][0]
        self.assertTrue(all(m['readOnly'] for m in container['volumeMounts']))
        self.assertTrue(container['image'].endswith('@sha256:' + 'a' * 64))
        self.assertTrue(container['securityContext']['runAsNonRoot'])
        self.assertNotIn('secretKeyRef', json.dumps(job))
        self.assertEqual(job['metadata']['labels']['perf-runner/run-id'], 'lt-a-1')

    def test_seed_job_binds_secrets_by_reference_only(self):
        outputs = {'database.url': ('database-credentials', 'url'), 'database.host': 'database', 'database.port': 5432}
        job = seed_job_manifest(run_id='lt-a-1', image='registry/checkout@sha256:' + 'b' * 64,
            module='checkout.database', args=('--seed-file', 'fixtures/products.csv'),
            bindings={'DATABASE_URL': 'database.url', 'DB_HOST': 'database.host'}, outputs=outputs, timeout_seconds=60)
        container = job['spec']['template']['spec']['containers'][0]
        self.assertEqual(container['command'], ['python', '-m', 'checkout.database', '--seed-file', 'fixtures/products.csv'])
        by_name = {e['name']: e for e in container['env']}
        self.assertEqual(by_name['DATABASE_URL']['valueFrom']['secretKeyRef'], {'name': 'database-credentials', 'key': 'url'})
        self.assertEqual(by_name['DB_HOST']['value'], 'database')
        self.assertEqual(job['spec']['backoffLimit'], 0)
        init = job['spec']['template']['spec']['initContainers'][0]
        self.assertEqual(init['name'], 'wait-for-database')
        self.assertNotIn('secretKeyRef', json.dumps(init))
        self.assertEqual(job['spec']['template']['spec']['terminationGracePeriodSeconds'], 5)

    def cluster(self, job, pods):
        fake = mock.MagicMock()
        fake.command.side_effect = lambda *args, **kwargs: json.dumps(job if args[1].startswith('job/') else {'items': pods})
        return fake

    def test_wait_returns_terminal_condition(self):
        job = {'status': {'conditions': [{'type': 'Failed', 'status': 'True', 'reason': 'DeadlineExceeded'}]}}
        self.assertEqual(wait_for_job(self.cluster(job, []), 'x', deadline_seconds=5, poll_seconds=0),
                         ('failed', 'DeadlineExceeded'))

    def test_unpullable_image_fails_fast_and_never_hangs(self):
        pod = {'status': {'containerStatuses': [{'state': {'waiting': {'reason': 'ImagePullBackOff'}}}]}}
        with self.assertRaisesRegex(RuntimeError, 'ImagePullBackOff'):
            wait_for_job(self.cluster({}, [pod]), 'x', deadline_seconds=5, pull_grace_seconds=0, poll_seconds=0)

    def test_unfinished_job_hits_the_bounded_wait(self):
        with mock.patch('perf_runner.jobs.time.monotonic', side_effect=[0, 0, 10_000, 10_000, 10_000]):
            with self.assertRaises(TimeoutError):
                wait_for_job(self.cluster({}, []), 'x', deadline_seconds=1, poll_seconds=0)


if __name__ == '__main__':
    unittest.main()
