"""Observable lifecycle progress without exposing raw commands or flow output."""
import io
import json
from pathlib import Path
import tempfile
import time
import unittest

from perf_runner.progress import Progress, info
from perf_runner.jobs import format_suite_progress, read_suite_progress
import test_run
RESULT = test_run.RESULT


class ProgressTests(unittest.TestCase):
    def test_flush_heartbeat_duration_and_structured_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path, stream = Path(directory), io.StringIO()
            reporter = Progress(path, 'lt-logging-1', stream=stream, interval=.1)
            with reporter.activate():
                with reporter.operation('resolve', 'Resolving artifacts'):
                    info('Chart fetched', artifact='chart')
                    deadline = time.monotonic() + 2
                    while 'still working' not in stream.getvalue() and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertIn('Chart fetched', (path / 'progress.log').read_text())
                    self.assertIn('still working', stream.getvalue())
            events = [json.loads(line) for line in (path / 'events.jsonl').read_text().splitlines()]
            self.assertEqual(events[0]['event'], 'step-start')
            self.assertEqual(events[-1]['event'], 'step-complete')
            self.assertTrue(any(e.get('event') == 'heartbeat' for e in events))
            self.assertEqual((path / 'progress.log').stat().st_mode & 0o777, 0o600)
            self.assertEqual((path / 'events.jsonl').stat().st_mode & 0o777, 0o600)

    def test_redaction_control_characters_and_quiet_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            stream = io.StringIO()
            reporter = Progress(Path(directory), 'lt-redact-1', stream=stream, quiet=True,
                redact=lambda text: text.replace('generated-secret', '[REDACTED]'))
            with reporter.activate():
                with self.assertRaises(RuntimeError):
                    with reporter.operation('seed', 'Seed database'):
                        info('Bearer auth-token \x1b[31mRED\nforged line generated-secret', detail='generated-secret')
                        raise RuntimeError('postgresql://user:private@db/test generated-secret')
            self.assertEqual(stream.getvalue(), '')
            text = (Path(directory) / 'progress.log').read_text()
            raw = (Path(directory) / 'events.jsonl').read_text()
            for secret in ('generated-secret', 'auth-token', 'user:private', '\x1b'):
                self.assertNotIn(secret, text + raw)
            self.assertIn('forged line [REDACTED]', text)
            self.assertEqual(json.loads(raw.splitlines()[-1])['event'], 'step-failed')

    def test_interval_validation(self):
        for value in (0, -1, float('nan'), float('inf'), 61):
            with self.assertRaises(ValueError):
                Progress(Path('/tmp'), 'lt-test-1', interval=value)

    def test_runtime_messages_are_validated_and_deduplicated(self):
        event = dict(runId='lt-workload-1', sequence=1, event='stage', stage='steady', elapsedSeconds=5,
            durationSeconds=20, targetUsers=10, actualUsers=8, completedJourneys=40, failedJourneys=0, measure=True)
        class Cluster:
            run = type('Run', (), {'run_id': 'lt-workload-1'})()
            def command(self, *args, **kwargs):
                wrong = dict(event, runId='another-run', sequence=2)
                return 'private arbitrary app output\nPERF_PROGRESS ' + json.dumps(event) + '\nPERF_PROGRESS ' + json.dumps(wrong)
        with tempfile.TemporaryDirectory() as directory:
            reporter = Progress(Path(directory), 'lt-workload-1', quiet=True)
            seen = set()
            with reporter.activate():
                read_suite_progress(Cluster(), 'suite', seen)
                read_suite_progress(Cluster(), 'suite', seen)
            text = (Path(directory) / 'progress.log').read_text()
            self.assertEqual(text.count('Stage steady:'), 1)
            self.assertIn('users 8/10 target', text)
            self.assertNotIn('private arbitrary', text)
        self.assertIsNone(format_suite_progress(dict(event, targetUsers=float('nan'))))
        self.assertIsNone(format_suite_progress(dict(event, event='arbitrary')))


class LifecycleLoggingTests(unittest.TestCase):
    setUp, make, scripted = test_run.LifecycleTests.setUp, test_run.LifecycleTests.make, test_run.LifecycleTests.scripted
    def test_final_logging_and_json_stdout_separation(self):
        run = self.make(quiet=False)
        def body():
            with run.phase('validate'):
                info('Checking cluster')
        run = self.scripted(run, body, RESULT)
        stream = io.StringIO()
        run.progress.stream = stream
        record = run.execute()
        self.assertEqual(record['exitCode'], 0)
        self.assertIn('[1/11] Checking Kubernetes access', stream.getvalue())
        self.assertIn('PASSED | execution=completed', stream.getvalue())
        self.assertIn('[9/11] Removing run resources', stream.getvalue())
        self.assertGreaterEqual(record['phases'][0]['elapsedSeconds'], 0)
        self.assertEqual(record['logs']['progress'], 'progress.log')

    def test_log_and_report_failure_preserve_primary_failure(self):
        failed = dict(RESULT, outcome='failed', performanceOutcome='failed')
        run = self.scripted(self.make(quiet=True), lambda: None, failed, 'failed')
        def failure():
            run.progress.errors.append('progress.log')
            raise OSError('disk unavailable')
        run.write_report = failure
        record = run.execute()
        self.assertEqual((record['status'], record['exitCode']), ('failed', 1))
        self.assertEqual(record['outcome']['collection'], 'failed')

    def test_failed_log_retention_cannot_report_success(self):
        run = self.scripted(self.make(quiet=True), lambda: None, RESULT)
        original = run.write_report
        def failure():
            run.progress.errors.append('progress.log')
            original()
        run.write_report = failure
        record = run.execute()
        self.assertEqual((record['status'], record['exitCode'], record['outcome']['collection']),
                         ('passed-with-errors', 3, 'failed'))
        result = json.loads((run.directory / 'testresult.json').read_text())
        self.assertEqual(result['status'], 'passed-with-errors')
        self.assertIn('PASSED-WITH-ERRORS', (run.directory / 'report.md').read_text())
