"""Opt-in native Kubernetes Job execution of the suite image against real Checkout."""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import unittest

from perf_runner.database import RUN_LABEL, KubernetesTarget, RunContext, _save, _write_private
from perf_runner.environment import load_environment, prepare_environment
from perf_runner.kubernetes import Kubernetes
from perf_runner.suite_scripts import publish_suite_scripts
from perf_runner.load_profile import load_profile

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get('SUITE_KIND_TEST') == '1', 'opt-in suite image/kind integration')
class SuiteKindTests(unittest.TestCase):
    def test_image_journeys_failure_and_interruption(self):
        run_id = 'suite-check-' + secrets.token_hex(4)
        run = RunContext(run_id, run_id, ROOT / '.runtime/runs' / run_id)
        target = KubernetesTarget(ROOT / '.runtime/performance-test/kubeconfig', 'kind-performance-test')
        cluster = Kubernetes(target, run)
        cluster.command('create', '-f', '-', data={'apiVersion': 'v1', 'kind': 'Namespace',
            'metadata': {'name': run_id, 'labels': {RUN_LABEL: run_id}}})
        self.addCleanup(cluster.delete_owned_namespace)
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(['helm', 'package', str(ROOT / 'platform/charts/http-service'), '-d', directory],
                           check=True, capture_output=True)
            environment = prepare_environment(*load_environment(ROOT / 'docs/examples/local/environment.yaml'),
                chart=Path(directory) / 'http-service-0.1.0.tgz', target=target, run=run)
        nodes = subprocess.run([str(ROOT / '.runtime/bin/kind'), 'get', 'nodes', '--name', 'performance-test'],
                               check=True, capture_output=True, text=True).stdout.splitlines()
        bundle = publish_suite_scripts(ROOT / 'TestSuites/checkout/suite.yaml', ROOT / 'TestSuites/checkout', target=target, run=run)
        evidence = {'run_id': run_id, 'kind_nodes': nodes, 'cases': {}}

        def execute(name, journeys, iterations=1, users=1, cancel=False, script_bundle=bundle, profile=None, fail_threshold=False):
            job_name = 'suite-' + name
            suite_id = run_id + '-' + name
            arguments = ['--suite', '/suite-input/suite.yaml', '--result', '/results/result.json', '--print-result', '--iterations', str(iterations),
                         '--users', str(users), '--execution-timeout', '60', '--drain-timeout', '1']
            for journey in journeys:
                arguments += ['--journey', journey]
            profile_volume = []
            profile_mount = []
            job_deadline = 120
            if profile:
                import yaml
                from load_fixtures import profile as internal_profile
                profile_path = Path(tempfile.mkdtemp()) / 'profile.yaml'
                profile_path.write_text(yaml.safe_dump(internal_profile(profile)))
                parsed_profile = load_profile(profile_path, journeys)
                job_deadline = parsed_profile.job_deadline
                import yaml
                profile_document = yaml.safe_load(profile_path.read_text())
                if fail_threshold:
                    profile_document['spec']['thresholds'][0]['max'] = 0
                profile_cm = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'immutable': True,
                    'metadata': {'name': job_name + '-profile', 'labels': {RUN_LABEL: run_id}},
                    'data': {'profile.yaml': yaml.safe_dump(profile_document)}}
                cluster.command('create', '-f', '-', data=profile_cm)
                _save(run.artifacts_dir / ('profile-' + name + '.json'), profile_cm)
                arguments = ['--suite', '/suite-input/suite.yaml', '--profile', '/profile/profile.yaml',
                             '--result', '/results/result.json', '--print-result']
                profile_volume = [{'name': 'profile', 'configMap': {'name': job_name + '-profile'}}]
                profile_mount = [{'name': 'profile', 'mountPath': '/profile', 'readOnly': True}]
            job = {'apiVersion': 'batch/v1', 'kind': 'Job',
                'metadata': {'name': job_name, 'labels': {RUN_LABEL: run_id}},
                'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': job_deadline, 'template': {
                    'metadata': {'labels': {RUN_LABEL: run_id}}, 'spec': {'restartPolicy': 'Never',
                    'automountServiceAccountToken': False,
                    'volumes': [{'name': 'user-flows', 'configMap': {'name': script_bundle.config_map}}] + profile_volume,
                    'initContainers': [{'name': 'wait-for-api', 'image': 'python-suite:0.1.0',
                        'imagePullPolicy': 'Never', 'command': ['python', '-c', '''import time,urllib.request
url = %r + '/readyz'
deadline = time.monotonic() + 30
while True:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            assert response.status == 200
        break
    except OSError:
        if time.monotonic() >= deadline:
            raise
        time.sleep(.2)
''' % environment.application.url]}],
                    'containers': [{'name': 'suite', 'image': 'python-suite:0.1.0', 'imagePullPolicy': 'Never',
                        'volumeMounts': [{'name': 'user-flows', 'mountPath': '/suite-input', 'readOnly': True}] + profile_mount,
                        'args': arguments, 'env': [{'name': 'BASE_URL', 'value': environment.application.url},
                                                 {'name': 'RUN_ID', 'value': suite_id}],
                        'resources': {'requests': {'cpu': '200m', 'memory': '256Mi'},
                                      'limits': {'cpu': '1', 'memory': '512Mi'}}}]}}}}
            cluster.command('create', '-f', '-', data=job)
            deadline = time.monotonic() + job_deadline + 10
            signalled = False
            while time.monotonic() < deadline:
                current = json.loads(cluster.command('get', f'job/{job_name}', '-o', 'json'))
                pods = json.loads(cluster.command('get', 'pods', '-l', f'job-name={job_name}', '-o', 'json'))['items']
                if cancel and not signalled and pods and pods[0]['status']['phase'] == 'Running':
                    try:
                        cluster.command('exec', pods[0]['metadata']['name'], '-c', 'suite', '--', 'python', '-c',
                            "import json; assert json.load(open('/results/result.json')).get('engineReady')", timeout=5)
                    except RuntimeError:
                        pass
                    else:
                        cluster.command('exec', pods[0]['metadata']['name'], '-c', 'suite', '--', 'python', '-c',
                                        'import os,signal; os.kill(1,signal.SIGTERM)', timeout=5)
                        signalled = True
                conditions = current.get('status', {}).get('conditions', [])
                if any(item['type'] in ('Complete', 'Failed') and item['status'] == 'True' for item in conditions):
                    break
                time.sleep(.2)
            else:
                self.fail('Suite Job did not finalize')
            log = cluster.command('logs', f'job/{job_name}', '-c', 'suite', timeout=10)
            output_dir = run.artifacts_dir / 'suite' / name
            output_dir.mkdir(parents=True, mode=0o700)
            _write_private(output_dir / 'job.log', log)
            _save(output_dir / 'job.json', current)
            manifests = []
            for line in log.splitlines():
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict) and parsed.get('schemaVersion') == 'v1alpha1' and parsed.get('runId') == suite_id:
                    manifests.append(parsed)
            self.assertEqual(len(manifests), 1, log[-3000:])
            result = manifests[0]
            _save(output_dir / 'result.json', result)
            pods = json.loads(cluster.command('get', 'pods', '-l', f'job-name={job_name}', '-o', 'json'))['items']
            self.assertEqual(len(pods), 1)  # backoffLimit=0: no hidden traffic retry.
            self.assertIn(pods[0]['spec']['nodeName'], nodes)
            self.assertEqual(pods[0]['spec']['volumes'][0]['configMap']['name'], script_bundle.config_map)
            self.assertTrue(pods[0]['spec']['containers'][0]['volumeMounts'][0]['readOnly'])
            self.assertEqual(result['scriptBundleSha256'], script_bundle.sha256)
            container = pods[0]['status']['containerStatuses'][0]
            code = container['state']['terminated']['exitCode']
            evidence['cases'][name] = {'outcome': result['outcome'], 'state': result['state'],
                                      'exit_code': code, 'bundle_sha256': result['scriptBundleSha256'], 'image_id': container['imageID'],
                                      'node': pods[0]['spec']['nodeName']}
            self.assertEqual(container['restartCount'], 0)
            return result, code

        # The image must also support selecting a single journey.
        single, code = execute('single', ['purchase'])
        self.assertEqual((code, single['outcome']), (0, 'passed'))
        smoke, code = execute('concurrent', ['purchase', 'browse'], iterations=6, users=3)
        self.assertEqual((code, smoke['outcome']), (0, 'passed'))
        self.assertEqual(smoke['journeys']['purchase']['passed'], 6)
        self.assertEqual(smoke['journeys']['browse']['passed'], 6)
        self.assertEqual(len({item['id'] for item in smoke['dataSamples']}), 6)
        paid = cluster.command('exec', 'deployment/database', '--', 'psql', '-U', environment.database.user,
            '-d', environment.database.database, '-At', '-c', "SELECT count(*) FROM orders WHERE status='paid'")
        self.assertEqual(int(paid.strip()), 7)

        for profile_name in ('smoke', 'baseline'):
            profiled, code = execute('profile-' + profile_name, ['browse', 'purchase'], profile=profile_name)
            self.assertEqual((code, profiled['outcome']), (0, 'passed'))
            self.assertEqual(profiled['executionOutcome'], 'completed')
            self.assertTrue(all(stage['reachedTarget'] for stage in profiled['stages']))
            self.assertEqual(profiled['measured']['journey']['samples'],
                sum(stage['journey']['samples'] for stage in profiled['stages'] if stage['measure']))
            self.assertEqual(evidence['cases']['single']['image_id'],
                             evidence['cases']['profile-' + profile_name]['image_id'])
        threshold_failed, code = execute('profile-threshold', ['browse', 'purchase'],
            profile='smoke', fail_threshold=True)
        self.assertEqual((code, threshold_failed['executionOutcome'], threshold_failed['performanceOutcome']),
                         (1, 'completed', 'failed'))
        cancelled_profile, code = execute('profile-cancelled', ['browse', 'purchase'], profile='baseline', cancel=True)
        self.assertEqual((code, cancelled_profile['performanceOutcome']), (130, 'incomplete'))

        # Add a user-authored flow/helper to a new ConfigMap, without rebuilding the image.
        with tempfile.TemporaryDirectory() as directory:
            updated = Path(directory) / 'updated'
            shutil.copytree(ROOT / 'TestSuites/checkout', updated)
            import yaml
            definition_path = updated / 'suite.yaml'
            definition = yaml.safe_load(definition_path.read_text())
            definition['spec']['journeys']['healthcheck'] = {'callable': 'added_flow:healthcheck'}
            definition_path.write_text(yaml.safe_dump(definition))
            (updated / 'added_flow.py').write_text("from .helpers import expect_ok\ndef healthcheck(context):\n    expect_ok(context.client.get('/healthz', name='/healthz'))\n")
            (updated / 'helpers.py').write_text('def expect_ok(response):\n    assert response.status_code == 200\n')
            new_bundle = publish_suite_scripts(definition_path, updated, target=target, run=run)
            self.assertNotEqual(new_bundle.sha256, bundle.sha256)
            archived = run.artifacts_dir / 'suite-scripts' / new_bundle.sha256 / 'configmap.json'
            self.assertIn('added_flow.py', json.loads(archived.read_text())['data'])
            added, code = execute('added-flow', ['healthcheck'], script_bundle=new_bundle)
            self.assertEqual((code, added['journeys']['healthcheck']['passed']), (0, 1))
            self.assertEqual(evidence['cases']['single']['image_id'], evidence['cases']['added-flow']['image_id'])
        # Original immutable bundle is still available for the existing scenarios.
        original = json.loads(cluster.command('get', f'configmap/{bundle.config_map}', '-o', 'json'))
        self.assertTrue(original['immutable'])

        # Valid HTTP 200 with wrong business data must fail the suite/Job (every product, since browse picks one at random).
        cluster.command('exec', 'deployment/database', '--', 'psql', '-U', environment.database.user,
            '-d', environment.database.database, '-c', "UPDATE products SET price_cents=price_cents+225")
        failed, code = execute('assertion', ['browse'])
        self.assertEqual((code, failed['outcome'], failed['journeys']['browse']['failed']), (1, 'failed', 1))
        self.assertEqual(sum(item['failures'] for item in failed['http']), 0)
        cluster.command('exec', 'deployment/database', '--', 'psql', '-U', environment.database.user,
            '-d', environment.database.database, '-c', "UPDATE products SET price_cents=price_cents-225")
        cancelled, code = execute('cancelled', ['purchase'], iterations=1000, users=2, cancel=True)
        self.assertEqual((code, cancelled['state'], cancelled['outcome']), (130, 'interrupted', 'failed'))
        _save(run.artifacts_dir / 'suite-verification.json', evidence)
        print('Suite image evidence:', run.artifacts_dir / 'suite-verification.json')
