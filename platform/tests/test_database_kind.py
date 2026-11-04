"""Opt-in real PostgreSQL/local-file lifecycle checks in isolated kind namespaces."""
from dataclasses import replace
import json
import os
from pathlib import Path
import secrets
import signal
import time
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from perf_runner.database import (RUN_LABEL, DatabasePreparationError, DatabaseSpec,
    KubernetesTarget, RunContext, SeedSpec, prepare_database)

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get('DATABASE_KIND_TEST') == '1', 'opt-in database kind integration')
class DatabaseKindTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='database-kind-')
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.namespace = 'database-test-' + secrets.token_hex(4)
        self.target = KubernetesTarget(ROOT / '.runtime/performance-test/kubeconfig', 'kind-performance-test')
        self.run = RunContext(self.namespace, self.namespace, self.work / 'artifacts')
        self.base = ['kubectl', '--kubeconfig', str(self.target.kubeconfig),
                     '--context', self.target.context, '-n', self.namespace]
        manifest = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {
                    'name': self.namespace, 'labels': {RUN_LABEL: self.run.run_id}}}
        self.kubectl('create', '-f', '-', data=manifest)
        self.addCleanup(self.kubectl, 'delete', 'namespace', self.namespace, '--wait=true', '--timeout=120s')
        self.spec = DatabaseSpec('postgres', '17.6', 'checkout',
            SeedSpec('checkout.database', ROOT / 'sample-service',
                     ('--seed-file', str(ROOT / 'sample-service/fixtures/products.csv'))))

    def kubectl(self, *args, data=None):
        result = subprocess.run(self.base + list(args), input=json.dumps(data) if data else None,
                                capture_output=True, text=True, timeout=150)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def query(self, sql):
        deployment = json.loads(self.kubectl('get', 'deployment/database', '-o', 'json'))
        env = deployment['spec']['template']['spec']['containers'][0]['env']
        user = next(item['value'] for item in env if item['name'] == 'POSTGRES_USER')
        return self.kubectl('exec', 'deployment/database', '--', 'psql', '-U', user,
                           '-d', 'checkout', '-At', '-c', sql).strip()

    def result(self):
        return json.loads((self.run.artifacts_dir / 'database/result.json').read_text())

    def assert_processes_stopped(self):
        result = self.result()
        for pid in (result.get('seed_pid'), result.get('forward_pid')):
            if pid:
                with self.assertRaises(ProcessLookupError):
                    os.killpg(pid, 0)
        self.assertEqual(result['cleanup_errors'], [])

    def module(self, code):
        (self.work / 'custom_seed.py').write_text(code)
        return replace(self.spec, seed=SeedSpec('custom_seed', self.work, timeout_seconds=10))

    def test_real_local_csv_and_modified_data_and_no_reseed(self):
        # Change real local data; generic runner must not manufacture sample rows.
        csv = self.work / 'products.csv'
        csv.write_text('id,name,price_cents\ncoffee,Local coffee,725\ncocoa,Cocoa,425\n')
        spec = replace(self.spec, seed=replace(self.spec.seed, args=('--seed-file', str(csv))))
        outputs = prepare_database(spec, target=self.target, run=self.run)
        self.assertEqual(self.query('SELECT id || \'|\' || name || \'|\' || price_cents FROM products ORDER BY id'),
                         'cocoa|Cocoa|425\ncoffee|Local coffee|725')
        self.assertEqual(outputs.host, f'database.{self.namespace}.svc.cluster.local')
        self.assertEqual(outputs.url_secret.key, 'url')
        self.assertEqual(self.result()['state'], 'succeeded')
        secret = json.loads(self.kubectl('get', 'secret/database-credentials', '-o', 'json'))
        import base64
        password = base64.b64decode(secret['data']['password']).decode()
        self.assertNotIn(password, ''.join(p.read_text() for p in (self.run.artifacts_dir / 'database').iterdir()))
        with self.assertRaisesRegex(ValueError, 'already exist'):
            prepare_database(spec, target=self.target, run=self.run)
        self.assert_processes_stopped()

    def test_original_fixture(self):
        prepare_database(self.spec, target=self.target, run=self.run)
        self.assertEqual(self.query('SELECT id || \'|\' || price_cents FROM products ORDER BY id'),
                         'coffee|500\ntea|300')
        self.assert_processes_stopped()

    def test_invalid_csv_fails_without_schema(self):
        csv = self.work / 'bad.csv'
        csv.write_text('id,name,price_cents\ncoffee,Coffee,-1\n')
        spec = replace(self.spec, seed=replace(self.spec.seed, args=('--seed-file', str(csv))))
        with self.assertRaises(DatabasePreparationError) as error:
            prepare_database(spec, target=self.target, run=self.run)
        self.assertEqual(error.exception.phase, 'seed')
        self.assertEqual(self.query("SELECT to_regclass('public.products') IS NULL"), 't')
        self.assertEqual(self.result()['state'], 'failed')
        self.assert_processes_stopped()

    def test_sql_failure_rolls_back_and_redacts_credentials(self):
        spec = self.module('''import os, psycopg
print(os.environ['DATABASE_URL'], flush=True)
with psycopg.connect(os.environ['DATABASE_URL']) as db:
    db.execute('CREATE TABLE marker (id integer)')
    db.execute('INSERT INTO marker VALUES (1)')
    db.execute('SELECT 1/0')
''')
        with self.assertRaises(DatabasePreparationError):
            prepare_database(spec, target=self.target, run=self.run)
        self.assertEqual(self.query("SELECT to_regclass('public.marker') IS NULL"), 't')
        self.assertEqual(self.result()['seed']['exit_code'], 1)
        self.assertIn('[REDACTED]', (self.run.artifacts_dir / 'database/seed.log').read_text())
        self.assert_processes_stopped()

    def test_timeout_kills_seed_and_forward(self):
        spec = self.module('import time; time.sleep(60)\n')
        spec = replace(spec, seed=replace(spec.seed, timeout_seconds=.5))
        with self.assertRaisesRegex(DatabasePreparationError, 'timed out'):
            prepare_database(spec, target=self.target, run=self.run)
        self.assertEqual(self.result()['error']['type'], 'TimeoutError')
        self.assert_processes_stopped()

    def test_unavailable_database_cannot_report_success(self):
        spec = self.module('''import os, psycopg
with psycopg.connect(host='127.0.0.1', port=1, dbname='checkout', connect_timeout=1):
    pass
''')
        with self.assertRaises(DatabasePreparationError):
            prepare_database(spec, target=self.target, run=self.run)
        self.assertEqual(self.result()['state'], 'failed')
        self.assert_processes_stopped()

    def test_cli_cancellation_stops_processes_and_retains_failure(self):
        (self.work / 'custom_seed.py').write_text('import time; time.sleep(60)\n')
        import yaml
        definition = self.work / 'database.yaml'
        definition.write_text(yaml.safe_dump({'spec': {'database': {
            'type': 'postgres', 'version': '17.6', 'name': 'checkout',
            'seed': {'module': 'custom_seed', 'workingDirectory': '.', 'timeoutSeconds': 60}}}}))
        process = subprocess.Popen([sys.executable, '-m', 'perf_runner.database',
            '--definition', str(definition), '--kubeconfig', str(self.target.kubeconfig),
            '--context', self.target.context, '--namespace', self.namespace,
            '--run-id', self.run.run_id, '--artifacts', str(self.run.artifacts_dir)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                path = self.run.artifacts_dir / 'database/result.json'
                if path.exists() and 'seed_pid' in json.loads(path.read_text()):
                    break
                if process.poll() is not None:
                    self.fail(process.communicate()[1])
                time.sleep(.1)
            else:
                self.fail('Seed did not start')
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 130, stderr)
            self.assertEqual(self.result()['state'], 'cancelled')
            self.assert_processes_stopped()
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_database_never_ready_is_bounded(self):
        with patch.dict('perf_runner.database.POSTGRES_IMAGES', {'17.6': 'registry.invalid/postgres:missing'}):
            spec = replace(self.spec, readiness_timeout_seconds=5)
            with self.assertRaises(DatabasePreparationError) as error:
                prepare_database(spec, target=self.target, run=self.run)
        self.assertEqual(error.exception.phase, 'readiness')
        self.assertNotIn('seed_pid', self.result())
        self.assert_processes_stopped()
