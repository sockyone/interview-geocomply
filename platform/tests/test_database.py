"""Local validation and subprocess lifecycle checks; no cluster needed."""
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from perf_runner.database import (KubernetesTarget, RunContext,
                                  _stop, prepare_database, load_database_spec)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'sample-service'))
from checkout.database import read_products, prepare


class SeedValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'products.csv'

    def test_fixture_and_changed_file(self):
        self.assertEqual(read_products(ROOT / 'sample-service/fixtures/products.csv'),
                         [('coffee', 'Coffee', 500), ('tea', 'Tea', 300)])
        self.path.write_text('id,name,price_cents\ncoffee,Local coffee,725\n')
        self.assertEqual(read_products(self.path), [('coffee', 'Local coffee', 725)])

    def test_bad_csv_is_rejected_before_connecting(self):
        for contents in ('id,name,price_cents\n', 'id,name\na,b\n',
                         'id,name,price_cents\nx,X,0\n',
                         'id,name,price_cents\nx,X,1\nx,X,2\n',
                         'id,name,price_cents\nx,,1\n',
                         'id,name,price_cents\nx,X,1,extra\n',
                         'id,name,price_cents\nx,X,2147483648\n'):
            with self.subTest(contents=contents), patch('checkout.database.connect') as connect:
                self.path.write_text(contents)
                with self.assertRaises(ValueError):
                    prepare(seed_file=self.path)
                connect.assert_not_called()


class DatabaseContractTests(unittest.TestCase):
    def test_local_definition_paths_and_invalid_inputs(self):
        spec = load_database_spec(ROOT / 'docs/examples/local/database.yaml')
        self.assertEqual(spec.seed.working_directory, ROOT / 'sample-service')
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config'
            config.touch()
            target = KubernetesTarget(config, 'kind-performance-test')
            run = RunContext('run-1', 'run-1', Path(directory) / 'artifacts')
            for invalid in (replace(spec, type='mysql'), replace(spec, version='latest'),
                            replace(spec, name='bad/name'),
                            replace(spec, seed=replace(spec.seed, timeout_seconds=float('nan'))),
                            replace(spec, readiness_timeout_seconds=0),
                            replace(spec, seed=replace(spec.seed, module='bad-module'))):
                with self.subTest(spec=invalid), patch('perf_runner.database.subprocess.run') as command:
                    with self.assertRaises(ValueError):
                        prepare_database(invalid, target=target, run=run)
                    command.assert_not_called()
            with patch('perf_runner.database.subprocess.run') as command:
                command.return_value = subprocess.CompletedProcess([], 0,
                    '{"metadata":{"labels":{}}}', '')
                with self.assertRaisesRegex(ValueError, 'not owned'):
                    prepare_database(spec, target=target, run=run)
                self.assertFalse(run.artifacts_dir.exists())

    def test_bad_definition_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'definition.yaml'
            for content in ('spec: {}', 'spec: [',
                            'spec: {database: {seed: {args: not-a-list, workingDirectory: .}}}',
                            'spec: {database: {seed: {}, unknown: true}}'):
                path.write_text(content)
                with self.subTest(content=content), self.assertRaises(ValueError):
                    load_database_spec(path)

    def test_stop_reaps_process_group(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                   start_new_session=True)
        try:
            _stop(process)
            self.assertIsNotNone(process.poll())
            with self.assertRaises(ProcessLookupError):
                os.killpg(process.pid, 0)
        finally:
            _stop(process)
