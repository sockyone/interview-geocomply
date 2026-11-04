"""Opt-in local OCI publication and resolution from an installed wheel, no source inputs."""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import unittest

import yaml

from perf_runner.database import _save, _write_private
from perf_runner.registry import Registry, AD_CONFIG, AD_LAYER

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / '.runtime/artifact-client/bin/python'


@unittest.skipUnless(os.environ.get('ARTIFACT_REGISTRY_TEST') == '1', 'opt-in local OCI registry checks')
class RegistryIntegrationTests(unittest.TestCase):
    def test_publish_and_clean_client_resolution(self):
        run_id = 'artifact-check-' + secrets.token_hex(4)
        directory = ROOT / '.runtime/runs' / run_id
        directory.mkdir(mode=0o700)
        registry_authority = os.environ.get('ARTIFACT_TEST_REGISTRY', 'localhost:5001')
        publisher = ROOT / '.runtime/perf-runner/bin/python'
        platform = ROOT / 'platform/config/local.yaml'
        publish = [str(publisher), '-m', 'perf_runner.publication', '--platform', str(platform)]
        logs = ''
        for action in (['platform'], ['checkout', '--version', '0.3.0']):
            result = subprocess.run([*publish, *action], cwd=ROOT, capture_output=True, text=True, timeout=900)
            logs += result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, result.stderr)
        _write_private(directory / 'publication.log', logs)
        # A clean client needs only the definition (names) and the platform config.
        clean = directory / 'clean-client'
        clean.mkdir(mode=0o700)
        shutil.copyfile(ROOT / 'LoadTestDefinitions/checkout.yaml', clean / 'definition.yaml')
        shutil.copyfile(platform, clean / 'platform.yaml')
        installed = subprocess.run([str(PYTHON), '-I', '-c', 'import perf_runner; print(perf_runner.__file__)'],
            cwd=clean, capture_output=True, text=True, check=True).stdout.strip()
        self.assertIn('site-packages', installed)
        self.assertNotIn('/platform/src/perf_runner', installed)
        base = [str(PYTHON), '-I', '-m', 'perf_runner.resolution', '--platform', 'platform.yaml']
        result = subprocess.run([*base, '--definition', 'definition.yaml', '--output', '../resolved'],
                                cwd=clean, capture_output=True, text=True, timeout=180)
        _write_private(directory / 'resolution.log', result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = json.loads((directory / 'resolved/resolved.json').read_text())
        self.assertEqual(snapshot['state'], 'resolved')
        identities = snapshot['artifacts']
        self.assertEqual(set(identities), {'applicationDefinition', 'chart', 'application', 'database', 'setup',
                                         'service.payments', 'suiteRuntime', 'scripts'})
        self.assertTrue(all('@sha256:' in role['resolved'] for role in identities.values()))
        self.assertEqual(identities['application']['resolved'], identities['setup']['resolved'])
        self.assertEqual(identities['application']['resolved'], identities['service.payments']['resolved'])
        # Resolving an unpublished name fails, leaves no ready snapshot and starts no workload.
        missing = yaml.safe_load((clean / 'definition.yaml').read_text())
        missing['spec']['suite'] = 'missing-' + run_id
        (clean / 'missing.yaml').write_text(yaml.safe_dump(missing))
        failed = subprocess.run([*base, '--definition', 'missing.yaml', '--output', '../missing'],
                                cwd=clean, capture_output=True, text=True, timeout=180)
        self.assertEqual(failed.returncode, 1)
        self.assertFalse((directory / 'missing/resolved.json').exists())
        self.assertEqual(json.loads((directory / 'missing/resolution.json').read_text())['state'], 'failed')
        # A moved tag changes future resolution while the previously returned digest remains retrievable.
        registry = Registry(http_registries=[registry_authority])
        probe = registry_authority + '/performance/' + run_id + ':0.1.0'
        first = registry.publish(probe, b'first-version', AD_CONFIG, AD_LAYER)
        second = registry.publish(probe, b'second-version', AD_CONFIG, AD_LAYER)
        self.assertNotEqual(first, second)
        self.assertEqual(registry.artifact(first, AD_CONFIG, AD_LAYER)[1], b'first-version')
        self.assertEqual(registry.artifact(probe, AD_CONFIG, AD_LAYER)[0], second)
        _save(directory / 'verification.json', {'state': 'passed', 'runId': run_id,
            'registry': registry_authority, 'clientPackage': installed, 'artifactRoles': identities,
            'missingArtifactRejected': True, 'tagMutationPreservedDigest': True,
            'scriptBundleSha256': snapshot['suite']['scriptBundleSha256']})
        print('Artifact evidence:', directory / 'verification.json')
