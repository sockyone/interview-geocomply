"""Application binding and supporting-service contract checks without a cluster."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

import yaml

from perf_runner.application import bind_application_values, validate_application
from perf_runner.database import SecretKeyRef
from perf_runner.environment import load_environment, prepare_environment
from perf_runner.services import PaymentStubSpec, payment_definition

ROOT = Path(__file__).resolve().parents[2]


class ApplicationBindingTests(unittest.TestCase):
    def setUp(self):
        self.ad = yaml.safe_load((ROOT / 'ApplicationDefinitions/checkout.yaml').read_text())

    def test_binding_replaces_literal_or_secret_without_mutating_ad(self):
        original = deepcopy(self.ad)
        values = bind_application_values(self.ad,
            {'DATABASE_URL': 'database.url', 'PAYMENT_BASE_URL': 'services.payments.url', 'DB_PORT': 'database.port'},
            {'database.url': SecretKeyRef('run-1', 'generated', 'url'),
             'services.payments.url': 'http://generated:8080', 'database.port': 5432},
            namespace='run-1', run_id='run-1')
        self.assertEqual(values['secretEnv']['DATABASE_URL'], {'name': 'generated', 'key': 'url'})
        self.assertNotIn('DATABASE_URL', values['env'])
        self.assertEqual(values['env']['PAYMENT_BASE_URL'], 'http://generated:8080')
        self.assertEqual(values['env']['DB_PORT'], '5432')
        self.assertEqual(values['runId'], 'run-1')
        self.assertEqual(self.ad, original)
        values = bind_application_values(self.ad, {'DATABASE_URL': 'plain'}, {'plain': 'literal'},
                                        namespace='run-1', run_id='run-1')
        self.assertNotIn('DATABASE_URL', values['secretEnv'])
        self.assertEqual(values['env']['DATABASE_URL'], 'literal')

    def test_unknown_binding_cross_namespace_and_bad_types_fail(self):
        for binding, outputs in [({'VAR': 'missing'}, {}),
            ({'VAR': 'secret'}, {'secret': SecretKeyRef('other-run', 's', 'k')}),
            ({'bad name': 'value'}, {'value': 'literal'}),
            ({'VAR': 'value'}, {'value': {'untyped': 'secret'}})]:
            with self.subTest(binding=binding), self.assertRaises(ValueError):
                bind_application_values(self.ad, binding, outputs, namespace='run-1', run_id='run-1')

    def test_wrong_ad_version_or_chart_is_rejected(self):
        for key, value in [('apiVersion', 'unsupported'), ('kind', 'Pod')]:
            ad = deepcopy(self.ad)
            ad[key] = value
            with self.assertRaises(ValueError):
                validate_application(ad)
        ad = deepcopy(self.ad)
        ad['spec']['chart']['name'] = 'user-source-path'
        with self.assertRaises(ValueError):
            validate_application(ad)

    def test_service_configuration_and_local_example(self):
        db, application, services, bindings = load_environment(ROOT / 'docs/examples/local/environment.yaml')
        self.assertEqual(db.seed.working_directory, ROOT / 'sample-service')
        self.assertEqual(application, self.ad)
        self.assertEqual(bindings['DATABASE_URL'], 'database.url')
        ad = payment_definition('payments', services['payments'])
        self.assertEqual(ad['spec']['values']['env'], {'PAYMENT_DELAY_MS': '50', 'PAYMENT_OUTCOME': 'approved'})
        for spec in (PaymentStubSpec({}, 50, 'approved'), PaymentStubSpec({'repository': 'image'}, -1),
                     PaymentStubSpec({'repository': 'image'}, 1, 'unknown')):
            with self.assertRaises(ValueError):
                payment_definition('payments', spec)

    def test_command_timeout_stops_child_process_group(self):
        import os
        import subprocess
        import sys
        import tempfile
        from perf_runner.kubernetes import run_command
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / 'pid'
            script = "import os,time; from pathlib import Path; Path(%r).write_text(str(os.getpid())); time.sleep(60)" % str(pid_file)
            with self.assertRaises(subprocess.TimeoutExpired):
                run_command([sys.executable, '-c', script], timeout=.5)
            pid = int(pid_file.read_text())
            with self.assertRaises(ProcessLookupError):
                os.killpg(pid, 0)

    def test_preparation_failure_prevents_all_deployments(self):
        # Preflight succeeds; actual preparation failure must stop orchestration.
        db, application, services, bindings = load_environment(ROOT / 'docs/examples/local/environment.yaml')
        import tempfile
        from perf_runner.database import KubernetesTarget, RunContext
        from perf_runner.environment import EnvironmentPreparationError
        with tempfile.TemporaryDirectory() as directory:
            run = RunContext('run-1', 'run-1', Path(directory))
            with patch('perf_runner.environment.validate_chart'), \
                 patch('perf_runner.environment.Kubernetes', autospec=True), patch('perf_runner.environment._helm'), \
                 patch('perf_runner.environment.prepare_database', side_effect=RuntimeError('seed failed')), \
                 patch('perf_runner.environment.deploy_application') as deploy, \
                 patch('perf_runner.environment.deploy_payment_stub') as stub:
                with self.assertRaises(EnvironmentPreparationError):
                    prepare_environment(db, application, services, bindings,
                        chart=Path('archive.tgz'), target=KubernetesTarget(Path('config'), 'context'), run=run)
                deploy.assert_not_called()
                stub.assert_not_called()
