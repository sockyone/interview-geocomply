"""Opt-in real-engine HTTP checks using an isolated synthetic Checkout server."""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / '.runtime/test-suite/bin/python'


@unittest.skipUnless(os.environ.get('SUITE_LOCAL_TEST') == '1', 'opt-in suite HTTP/engine checks')
class SuiteLocalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.orders = {}
        self.lock = threading.Lock()
        self.mode = 'normal'
        self.requests = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def handle_request(self):
                if owner.mode == 'slow':
                    time.sleep(2)
                with owner.lock:
                    owner.requests.append((time.monotonic(), self.command, self.path))
                    if self.path == '/products':
                        value = {'products': [{'id': 'coffee', 'priceCents': 1 if owner.mode == 'bad-price' else 500}]}
                        code = 200
                    elif self.path.startswith('/products/'):
                        product_id = self.path.split('/')[2]
                        price = {'coffee': 500, 'tea': 300}[product_id]
                        value, code = {'id': product_id, 'priceCents': 1 if owner.mode == 'bad-price' else price}, 200
                    elif self.path == '/orders' and self.command == 'POST':
                        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                        order_id = str(uuid4())
                        owner.orders[order_id] = {'id': order_id, 'status': 'draft', 'quantity': body['quantity'],
                            'productId': body['productId'], 'amountCents': body['quantity'] * 500, 'paymentId': None}
                        value, code = owner.orders[order_id], 201
                    else:
                        order_id = self.path.split('/')[2]
                        row = owner.orders[order_id]
                        if self.command == 'PATCH':
                            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                            row.update(quantity=body['quantity'], amountCents=body['quantity'] * 500)
                        if self.path.endswith('/checkout'):
                            self.rfile.read(int(self.headers['Content-Length']))
                            row.update(status='paid', paymentId=str(uuid4()))
                        value, code = row, 200
                    raw = b'{' if owner.mode == 'invalid-json' else json.dumps(value).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            do_GET = do_POST = do_PATCH = handle_request
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = f'http://127.0.0.1:{self.server.server_port}'

    def command(self, *args):
        return [str(PYTHON), '-m', 'suite_runtime', '--suite', str(ROOT / 'TestSuites/checkout/suite.yaml'), '--base-url', self.base_url,
                '--run-id', 'local-check', '--result', str(self.directory / 'result.json'), *args]

    def environment(self):
        return {**os.environ, 'PYTHONPATH': str(ROOT / 'platform/runner'), 'PYTHONOPTIMIZE': '1'}

    def result(self):
        return json.loads((self.directory / 'result.json').read_text())

    def profile_command(self, profile='smoke', *, fail=False):
        import yaml
        from load_fixtures import profile as internal_profile
        document = internal_profile(profile)
        if fail:
            document['spec']['thresholds'][0]['max'] = 0
        path = self.directory / 'profile.yaml'
        path.write_text(yaml.safe_dump(document))
        return self.command('--profile', str(path))

    def test_live_progress_events_are_separate_from_final_result(self):
        result = subprocess.run(self.profile_command('smoke'),
            env={**self.environment(), 'PERF_RUN_PROGRESS': '1', 'AUTH_BEARER_TOKEN': 'do-not-log-me'},
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line.removeprefix('PERF_PROGRESS ')) for line in result.stdout.splitlines()
                  if line.startswith('PERF_PROGRESS ')]
        self.assertTrue(any(e['event'] == 'stage' for e in events))
        self.assertTrue(any(e['event'] == 'drain' for e in events))
        self.assertEqual(events[-1]['event'], 'finished')
        self.assertEqual([e['sequence'] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])['outcome'], 'passed')
        self.assertNotIn('do-not-log-me', result.stdout)

    def test_profiles_share_bundle_and_measure_stages(self):
        digests = []
        for name in ('smoke', 'baseline'):
            with self.subTest(profile=name):
                result = subprocess.run(self.profile_command(name), env=self.environment(),
                    capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stderr)
                record = self.result()
                self.assertEqual(record['executionOutcome'], 'completed')
                self.assertEqual(record['performanceOutcome'], 'passed')
                self.assertTrue(all(s['reachedTarget'] for s in record['stages']))
                self.assertGreater(record['stages'][0]['journey']['samples'], 0)
                self.assertEqual(record['measured']['journey']['samples'],
                    sum(s['journey']['samples'] for s in record['stages'] if s['measure']))
                self.assertTrue(all(t['outcome'] == 'passed' for t in record['thresholdOutcomes']))
                digests.append(record['scriptBundleSha256'])
                (self.directory / 'result.json').unlink()
        self.assertEqual(digests[0], digests[1])

    def test_each_user_browses_three_items_pauses_then_purchases(self):
        result = subprocess.run(self.profile_command('smoke'), env=self.environment(),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        # One user: the server sees browse x3, a >=2 s gap, then the purchase's 4 calls, repeated.
        steps = [(t, 'browse' if path.startswith('/products/') else 'purchase-start' if (method, path) == ('POST', '/orders')
                  else None) for t, method, path in self.requests]
        steps = [(t, name) for t, name in steps if name]
        names = [name for _, name in steps]
        cycle = ['browse', 'browse', 'browse', 'purchase-start']
        self.assertGreaterEqual(len(names), 8)
        self.assertEqual(names[:len(names) // 4 * 4], cycle * (len(names) // 4))
        for index in range(3, len(steps), 4):
            self.assertGreaterEqual(steps[index][0] - steps[index - 1][0], 1.95)
        journeys = self.result()['journeys']
        self.assertEqual(journeys['browse']['attempted'], names.count('browse'))
        self.assertEqual(journeys['purchase']['passed'], names.count('purchase-start'))

    def test_threshold_failure_is_separate_from_execution(self):
        result = subprocess.run(self.profile_command(fail=True), env=self.environment(),
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stderr)
        record = self.result()
        self.assertEqual(record['executionOutcome'], 'completed')
        self.assertEqual(record['performanceOutcome'], 'failed')
        self.assertEqual(record['thresholdOutcomes'][0]['outcome'], 'failed')

    def test_profile_cancellation_does_not_pass(self):
        self.mode = 'slow'
        process = subprocess.Popen(self.profile_command(), env=self.environment(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not (self.directory / 'result.json').exists() or not self.result().get('engineReady'):
                if time.monotonic() > deadline:
                    self.fail('Profile engine did not start')
                time.sleep(.05)
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 130, stderr)
            self.assertEqual(self.result()['performanceOutcome'], 'incomplete')
            self.assertTrue(all(t['outcome'] == 'incomplete' for t in self.result()['thresholdOutcomes']))
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_crossing_journeys_and_empty_http_thresholds(self):
        import yaml
        bundle = self.directory / 'crossing'
        bundle.mkdir()
        (bundle / 'suite.yaml').write_text("""apiVersion: perf-runner/v1alpha1
kind: TestSuite
metadata: {name: crossing}
spec:
  image: python-suite:0.1.0
  journeys:
    flow: {callable: flow:run}
""")
        (bundle / 'flow.py').write_text('import gevent\ndef run(context):\n    gevent.sleep(3.1)\n')
        command = self.profile_command()
        document = yaml.safe_load((self.directory / 'profile.yaml').read_text())
        document['spec']['flow'] = [{'journey': 'flow'}]
        # Engine timing check: a fixed 3 s + 3 s shape, independent of the user profile files.
        document['spec']['stages'] = [
            {'name': 'warmup', 'transition': 'hold', 'durationSeconds': 3, 'users': 1, 'measure': False},
            {'name': 'steady', 'transition': 'hold', 'durationSeconds': 3, 'users': 1, 'measure': True}]
        (self.directory / 'profile.yaml').write_text(yaml.safe_dump(document))
        command[command.index(str(ROOT / 'TestSuites/checkout/suite.yaml'))] = str(bundle / 'suite.yaml')
        result = subprocess.run(command, env=self.environment(), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stderr)
        record = self.result()
        self.assertEqual(record['executionOutcome'], 'completed')
        self.assertEqual([s['journey']['samples'] for s in record['stages']], [1, 1])
        self.assertEqual(record['measured']['journey']['samples'], 1)
        self.assertEqual(record['thresholdOutcomes'][-1]['outcome'], 'no-samples')
        self.assertIsNone(record['thresholdOutcomes'][-1]['actual'])
        # A forced drain leaves an interrupted attempt and invalidates all thresholds.
        (self.directory / 'result.json').unlink()
        document['spec']['drainTimeoutSeconds'] = .1
        (self.directory / 'profile.yaml').write_text(yaml.safe_dump(document))
        result = subprocess.run(command, env=self.environment(), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.result()['executionOutcome'], 'failed')
        self.assertEqual(self.result()['performanceOutcome'], 'incomplete')
        # Cancellation arriving during final drain must override completed state.
        (self.directory / 'result.json').unlink()
        (bundle / 'flow.py').write_text('import gevent\ndef run(context):\n    gevent.sleep(4.5)\n')
        document['spec']['drainTimeoutSeconds'] = 5
        (self.directory / 'profile.yaml').write_text(yaml.safe_dump(document))
        process = subprocess.Popen(command, env=self.environment(), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not (self.directory / 'result.json').exists() or not self.result().get('engineReady'):
                if time.monotonic() > deadline:
                    self.fail('Engine did not start')
                time.sleep(.05)
            time.sleep(6.2)
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 130, stderr)
            self.assertEqual(self.result()['state'], 'interrupted')
            self.assertEqual(self.result()['performanceOutcome'], 'incomplete')
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_generator_stall_past_profile_end_fails_execution(self):
        import yaml
        bundle = self.directory / 'stall'
        bundle.mkdir()
        definition = yaml.safe_load((ROOT / 'TestSuites/checkout/suite.yaml').read_text())
        definition['spec'].pop('fixtures')
        definition['spec']['journeys'] = {'flow': {'callable': 'flow:run'}}
        (bundle / 'suite.yaml').write_text(yaml.safe_dump(definition))
        (bundle / 'flow.py').write_text("""import gevent
from gevent.monkey import get_original
def run(context):
    gevent.sleep(1.2)
    get_original('time', 'sleep')(3.5)
""")
        command = self.profile_command()
        document = yaml.safe_load((self.directory / 'profile.yaml').read_text())
        document['spec']['flow'] = [{'journey': 'flow'}]
        document['spec']['stages'] = [dict(name='steady', transition='hold', durationSeconds=3, users=1, measure=True)]
        document['spec']['thresholds'] = []
        (self.directory / 'profile.yaml').write_text(yaml.safe_dump(document))
        command[command.index(str(ROOT / 'TestSuites/checkout/suite.yaml'))] = str(bundle / 'suite.yaml')
        result = subprocess.run(command, env=self.environment(), capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.result()['executionOutcome'], 'failed')
        self.assertEqual(self.result()['performanceOutcome'], 'incomplete')
        self.assertTrue(self.result()['executionErrors'])

    def test_concurrent_purchase_and_browse(self):
        result = subprocess.run(self.command('--iterations', '8', '--users', '4'),
                                env=self.environment(), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.result()
        self.assertEqual(record['outcome'], 'passed')
        self.assertEqual(record['journeys']['purchase']['passed'], 8)
        self.assertEqual(record['journeys']['browse']['passed'], 8)
        self.assertEqual(len(self.orders), 8)
        self.assertTrue(all(order['status'] == 'paid' for order in self.orders.values()))
        self.assertEqual(len({item['id'] for item in record['dataSamples']}), 8)
        self.assertEqual(sum(item['requests'] for item in record['http']), 40)
        self.assertTrue(record['journeys']['purchase']['timingMs']['samples'] >= 8)

    def test_user_flow_context_data_is_independent_between_concurrent_attempts(self):
        bundle = self.directory / 'bundle'
        bundle.mkdir()
        (bundle / 'suite.yaml').write_text("""apiVersion: perf-runner/v1alpha1
kind: TestSuite
metadata: {name: custom}
spec:
  image: python-suite:0.1.0
  journeys:
    flow: {callable: flow:run}
""")
        (bundle / 'flow.py').write_text("""from uuid import uuid4
def run(context):
    value = str(uuid4())
    context.data['attempt'] = value
    assert context.client.get('/products', name='/products').status_code == 200
    assert context.data['attempt'] == value
""")
        command = self.command('--journey', 'flow', '--iterations', '20', '--users', '4')
        command[command.index(str(ROOT / 'TestSuites/checkout/suite.yaml'))] = str(bundle / 'suite.yaml')
        result = subprocess.run(command, env=self.environment(), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.result()['journeys']['flow']['passed'], 20)
        self.assertEqual(self.result()['suite'], 'custom')

    def test_assertion_failure_produces_nonzero_exit_even_with_optimization(self):
        self.mode = 'bad-price'
        result = subprocess.run(self.command('--journey', 'browse'), env=self.environment(),
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.result()['journeys']['browse']['failed'], 1)
        self.assertEqual(self.result()['outcome'], 'failed')

    def test_request_timeout_fails_journey(self):
        self.mode = 'slow'
        result = subprocess.run(self.command('--journey', 'browse', '--request-timeout', '.1'),
                                env=self.environment(), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.result()['journeys']['browse']['failed'], 1)

    def test_exception_cannot_pass(self):
        self.mode = 'invalid-json'
        command = self.command('--journey', 'browse')
        result = subprocess.run(command, env=self.environment(), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.result()['outcome'], 'failed')

    def test_sigterm_and_deadline_do_not_pass(self):
        self.mode = 'slow'
        process = subprocess.Popen(self.command('--journey', 'browse', '--iterations', '100',
            '--request-timeout', '5', '--drain-timeout', '.1'), env=self.environment(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while True:
                path = self.directory / 'result.json'
                if path.exists() and json.loads(path.read_text()).get('engineReady'):
                    break
                if time.monotonic() > deadline:
                    self.fail('Engine did not become ready')
                time.sleep(.05)
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 130, stderr)
            self.assertEqual(self.result()['state'], 'interrupted')
            self.assertEqual(self.result()['outcome'], 'failed')
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        (self.directory / 'result.json').unlink()
        result = subprocess.run(self.command('--journey', 'browse', '--execution-timeout', '.1',
            '--drain-timeout', '.1', '--request-timeout', '5'), env=self.environment(),
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.result()['state'], 'error')

    def test_hard_kill_leaves_incomplete_result(self):
        self.mode = 'slow'
        process = subprocess.Popen(self.command('--journey', 'browse', '--iterations', '100'),
            env=self.environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not (self.directory / 'result.json').exists():
                if time.monotonic() > deadline:
                    self.fail('No started result')
                time.sleep(.05)
            process.kill()
            process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(self.result()['outcome'], 'incomplete')
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
