"""Default checks for resource-usage aggregation and the durable TestResult/report."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from perf_runner.metrics import (counter_rates, export_resource_usage, expected_roles, role_for_pod, summarize,
                                 within)
from perf_runner.report import build_test_result, render_report

SERVICES = {'payments': object()}


def series(pod, container, points):
    return {'metric': {'pod': pod, 'container': container}, 'values': [[t, str(v)] for t, v in points]}


class MetricsTests(unittest.TestCase):
    def test_pod_roles(self):
        names = {'database-seed-abc12': 'seed', 'database-5d7-x': 'database', 'suite-wcsk6': 'generator',
                 'application-http-service-1-x': 'application', 'payments-http-service-1-x': 'service:payments',
                 'mystery': 'other'}
        for pod, role in names.items():
            self.assertEqual(role_for_pod(pod, SERVICES), role)

    def test_cpu_rate_needs_only_two_scrapes_and_skips_resets(self):
        rates = counter_rates([series('p', 'c', [(0, 1.0), (15, 4.0), (30, 0.5), (45, 3.5)])])
        self.assertEqual([(t, float(v)) for t, v in rates[0]['values']], [(15.0, .2), (45.0, .2)])
        self.assertEqual(counter_rates([series('p', 'c', [(0, 1.0)])])[0]['values'], [])

    def test_window_filter(self):
        kept = within([series('p', 'c', [(0, 1), (10, 2), (20, 3)])], 5, 15)
        self.assertEqual(kept[0]['values'], [[10, '2']])

    def test_summary_totals_containers_and_names_missing_roles(self):
        raw = {'cpuCores': [series('application-http-service-1-x', 'a', [(15, .2), (30, .4)]),
                            series('application-http-service-1-x', 'sidecar', [(15, .1), (30, .1)])],
               'memoryBytes': [series('application-http-service-1-x', 'a', [(15, 100), (30, 300)]),
                               series('suite-x', 'suite', [(15, 50)])]}
        summary, missing = summarize(raw, SERVICES)
        self.assertAlmostEqual(summary['application']['cpuCores']['peak'], .5)
        self.assertAlmostEqual(summary['application']['cpuCores']['mean'], .4)
        self.assertEqual(summary['application']['memoryBytes']['peak'], 300)
        self.assertIsNone(summary['generator']['cpuCores'])
        self.assertEqual(set(missing), {'database', 'seed', 'service:payments'})
        self.assertEqual(set(summary), set(expected_roles(SERVICES)))

    def test_prometheus_failure_is_stated_not_raised(self):
        cluster = mock.MagicMock()
        cluster.command.side_effect = RuntimeError('boom')
        usage, series_ = export_resource_usage(cluster, 'lt-x', 0, 60, SERVICES)
        self.assertFalse(usage['available'])
        self.assertIn('RuntimeError', usage['reason'])
        self.assertEqual(series_, {})


class ReportTests(unittest.TestCase):
    def record(self, **outcome):
        return {'runId': 'lt-x-1', 'definition': 'x', 'status': 'failed', 'exitCode': 1, 'startedAt': 'a', 'finishedAt': 'b',
                'outcome': {'test': 'failed', 'execution': 'completed', 'performance': 'failed',
                            'collection': 'ok', 'cleanup': 'ok', **outcome}, 'errors': [],
                'artifacts': {'chart': {'resolved': 'r/c@sha256:1'}}}

    def stage(self):
        timing = {'samples': 3, 'p50': 5, 'p95': 9, 'p99': 9, 'max': 9, 'min': 1, 'mean': 4}
        block = {'samples': 3, 'failures': 1, 'timingMs': timing}
        return {'name': 'steady', 'transition': 'hold', 'durationSeconds': 5, 'users': 4, 'measure': True,
                'startedAt': 's', 'finishedAt': 'f', 'reachedTarget': True, 'journey': block, 'http': block}

    def test_failed_run_reports_stages_thresholds_and_evidence(self):
        suite = {'executionOutcome': 'completed', 'performanceOutcome': 'failed', 'startedAt': 's', 'finishedAt': 'f',
                 'stages': [self.stage()], 'measured': {'journey': self.stage()['journey'], 'http': self.stage()['http']},
                 'thresholdOutcomes': [{'scope': 'measured', 'metric': 'journeyP95Ms', 'max': 0, 'actual': 9,
                                        'samples': 3, 'outcome': 'failed'}],
                 'journeys': {'browse': {'attempted': 3, 'passed': 2, 'failed': 1, 'interrupted': 0, 'timingMs': None}}}
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'suite').mkdir()
            (Path(directory) / 'suite/result.json').write_text('{}')
            result = build_test_result(self.record(), suite, {'available': False, 'reason': 'Prometheus query failed'}, directory)
        self.assertEqual(result['stages'][0]['journey']['timingMs']['p95'], 9)
        self.assertEqual(result['evidence']['suiteResult'], 'suite/result.json')
        self.assertNotIn('seedLog', result['evidence'])
        self.assertTrue(any('Resource metrics unavailable' in text for text in result['limitations']))
        text = render_report(result)
        self.assertIn('FAILED', text)
        self.assertIn('journeyP95Ms', text)
        self.assertIn('r/c@sha256:1', text)
        json.dumps(result)  # strictly serialisable

    def test_single_scrape_pods_get_an_explicit_cpu_limitation(self):
        usage = {'available': True, 'missingRoles': [], 'roles': {
            'application': {'pods': ['a'], 'cpuCores': None, 'memoryBytes': {'samples': 1, 'peak': 1, 'mean': 1}}}}
        with tempfile.TemporaryDirectory() as directory:
            result = build_test_result(self.record(), None, usage, directory)
        self.assertTrue(any('CPU unavailable for application' in text for text in result['limitations']))

    def test_missing_suite_result_is_stated_not_invented(self):
        with tempfile.TemporaryDirectory() as directory:
            result = build_test_result(self.record(execution='not-run', performance='not-run', test='error'),
                                       None, None, directory)
        self.assertEqual(result['stages'], [])
        self.assertIsNone(result['measured'])
        self.assertTrue(any('performance is unknown' in text for text in result['limitations']))
        render_report(result)

    def test_cleanup_and_collection_problems_are_listed(self):
        record = self.record(cleanup='failed', collection='failed')
        record['collectionErrors'] = ['events.json: RuntimeError']
        with tempfile.TemporaryDirectory() as directory:
            result = build_test_result(record, None, None, directory)
        joined = ' '.join(result['limitations'])
        self.assertIn('cleanup failed', joined.lower())
        self.assertIn('events.json', joined)


if __name__ == '__main__':
    unittest.main()
