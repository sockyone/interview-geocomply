"""Default checks for the run dashboard and the Prometheus CSV export (no cluster needed)."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from perf_runner.dashboard import progress_events, throughput
from perf_runner.metrics import series_csv
from perf_runner.report import render_report

START, MID, END = '2026-11-04T17:41:39+00:00', '2026-11-04T17:41:49+00:00', '2026-11-04T17:42:19+00:00'


def timing(p95):
    return {'samples': 10, 'p50': p95 / 2, 'p95': p95, 'p99': p95 * 1.2, 'max': p95 * 1.5}


def write_run(directory):
    result = {'runId': 'lt-demo-1', 'definition': 'demo', 'status': 'passed', 'exitCode': 0,
              'outcome': {'execution': 'completed', 'performance': 'passed', 'collection': 'ok', 'cleanup': 'ok'},
              'timestamps': {'runStartedAt': START, 'runFinishedAt': END, 'suiteStartedAt': START, 'suiteFinishedAt': END},
              'stages': [{'name': 'ramp-up', 'transition': 'ramp', 'durationSeconds': 10, 'users': 2, 'measured': False,
                          'startedAt': START, 'finishedAt': MID, 'journey': {'samples': 1, 'failures': 0, 'timingMs': timing(10)},
                          'http': {'samples': 1, 'failures': 0, 'timingMs': timing(5)}},
                         {'name': 'steady', 'transition': 'hold', 'durationSeconds': 30, 'users': 2, 'measured': True,
                          'startedAt': MID, 'finishedAt': END, 'journey': {'samples': 5, 'failures': 0, 'timingMs': timing(12)},
                          'http': {'samples': 9, 'failures': 0, 'timingMs': timing(6)}}],
              'thresholds': [{'scope': 'measured', 'metric': 'journeyP95Ms', 'max': 100, 'actual': 12, 'samples': 6,
                              'outcome': 'passed'}],
              'journeys': {'browse': {'attempted': 6, 'passed': 6, 'failed': 0, 'interrupted': 0, 'timingMs': timing(12)}},
              'http': [{'method': 'GET', 'name': '/products/{id}', 'requests': 10, 'failures': 0, 'timingMs': timing(6)}],
              'resourceUsage': None, 'errors': [], 'limitations': [], 'artifacts': {}, 'evidence': {}}
    (directory / 'testresult.json').write_text(json.dumps(result))
    (directory / 'suite').mkdir()
    lines = [f'PERF_PROGRESS {json.dumps(e)}' for e in (
        {'event': 'stage', 'elapsedSeconds': 0, 'targetUsers': 1, 'actualUsers': 0, 'completedJourneys': 0, 'failedJourneys': 0},
        {'event': 'tick', 'elapsedSeconds': 5, 'targetUsers': 2, 'actualUsers': 2, 'completedJourneys': 10, 'failedJourneys': 0},
        {'event': 'tick', 'elapsedSeconds': 10, 'targetUsers': 2, 'actualUsers': 2, 'completedJourneys': 30, 'failedJourneys': 1})]
    (directory / 'suite/suite.log').write_text('noise\n' + '\n'.join(lines) + '\nnot json PERF_PROGRESS {\n')
    (directory / 'metrics').mkdir()
    pod = 'application-http-service-abc'
    (directory / 'metrics/resource-usage.json').write_text(json.dumps({'roles': {'application': {'pods': [pod]}}}))
    (directory / 'metrics/series.json').write_text(json.dumps({
        'cpuCores': [{'metric': {'pod': pod, 'container': 'application'}, 'values': [[1793900500, '0.05'], [1793900505, '0.07']]}],
        'memoryBytes': [{'metric': {'pod': pod, 'container': 'application'}, 'values': [[1793900500, '73400320']]}]}))
    return result


class DashboardDataTests(unittest.TestCase):
    def test_progress_events_and_throughput_come_from_the_generator_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_run(directory)
            events = progress_events(directory)
            self.assertEqual([e['elapsedSeconds'] for e in events], [0, 5, 10])  # malformed lines skipped
            self.assertEqual(throughput(events), [(2.5, 2.0, 0.0), (7.5, 4.0, 0.2)])

    def test_csv_has_one_row_per_sample_with_role_and_offset(self):
        raw = {'cpuCores': [{'metric': {'pod': 'database-1', 'container': 'postgres'}, 'values': [[100.0, '0.5']]}]}
        lines = series_csv(raw, {}, origin=90.0).splitlines()
        self.assertEqual(lines[0], 'timestamp_utc,seconds_since_load_start,role,pod,container,metric,value')
        self.assertEqual(lines[1], '1970-01-01T00:01:40Z,10.0,database,database-1,postgres,cpuCores,0.5')

    def test_report_embeds_the_dashboard_when_it_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = write_run(Path(temporary))
            self.assertNotIn('![Dashboard', render_report(result))
            result['evidence']['dashboard'] = 'dashboard.png'
            self.assertIn('![Dashboard for lt-demo-1](dashboard.png)', render_report(result))


@unittest.skipUnless(importlib.util.find_spec('matplotlib'), 'matplotlib not installed (platform[charts])')
class DashboardRenderTests(unittest.TestCase):
    def test_renders_a_png_from_retained_files_only(self):
        from perf_runner.dashboard import render
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_run(directory)
            path = Path(render(directory))
            self.assertEqual(path, directory / 'dashboard.png')
            self.assertEqual(path.read_bytes()[:8], b'\x89PNG\r\n\x1a\n')

    def test_a_run_without_stages_has_nothing_to_draw(self):
        from perf_runner.dashboard import render
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / 'testresult.json').write_text(json.dumps({'stages': []}))
            with self.assertRaisesRegex(ValueError, 'nothing to chart'):
                render(directory)


if __name__ == '__main__':
    unittest.main()
