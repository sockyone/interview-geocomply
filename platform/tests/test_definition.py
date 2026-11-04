"""Published contract, OCI integrity and offline resolution through real Helm."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

from perf_runner.definition import load_definition, parse_definition
from perf_runner.platform import load_platform
from perf_runner.publication import publish_application, publish_suite
from perf_runner.registry import (Registry, ArtifactRef, MANIFEST, HELM_CONFIG, HELM_LAYER,
    AD_CONFIG, AD_LAYER, SUITE_CONFIG, SUITE_LAYER, checksum)
from perf_runner.resolution import resolve_definition, validate_bundle
from perf_runner.suite_scripts import bundle_files
from suite_runtime.loader import bundle_digest

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = load_platform(ROOT / 'platform/config/local.yaml')


class MemoryRegistry(Registry):
    def __init__(self):
        super().__init__()
        self.manifests, self.blobs, self.calls = {}, {}, []

    def request(self, ref, path, *, method='GET', data=None, **kwargs):
        self.calls.append((method, path))
        if method == 'POST':
            return b'', {'Location': '/upload'}
        if method == 'PUT' and path.startswith('/upload?digest='):
            digest = path.split('=', 1)[1]
            self.blobs[digest] = data
            return b'', {}
        if method == 'PUT' and '/manifests/' in path:
            self.manifests[path] = data
            self.manifests[path.rsplit('/', 1)[0] + '/' + checksum(data)] = data
            return b'', {}
        if '/manifests/' in path:
            try:
                body = self.manifests[path]
            except KeyError:
                raise RuntimeError('Registry request failed with HTTP 404') from None
            return body, {'Docker-Content-Digest': checksum(body), 'Content-Type': MANIFEST}
        if '/blobs/' in path:
            return self.blobs[path.rsplit('/', 1)[1]], {}
        raise AssertionError(path)


class DefinitionTests(unittest.TestCase):
    def setUp(self):
        self.doc = yaml.safe_load((ROOT / 'LoadTestDefinitions/checkout.yaml').read_text())

    def test_typed_example_and_deadline(self):
        definition = parse_definition(self.doc)
        self.assertEqual(definition.profile.duration, 40)
        self.assertEqual(definition.database.seed.module, 'checkout.database')
        self.assertEqual(definition.job_deadline, 115)  # 40 s load + 5 s drain + 10 s margin + 60 s startup
        self.assertEqual(definition.application.bindings['DATABASE_URL'], 'database.url')

    def test_invalid_contracts(self):
        changes = [
            lambda s: s['application'].update(definition='../../application.yaml'),
            lambda s: s['application'].update(definition='localhost:5001/performance/checkout-ad:1'),  # a name, not a link
            lambda s: s['application'].update(chart='http-service:0.1.0'),  # the AD names its chart
            lambda s: s['application']['bindings'].update(DATABASE_URL='database.missing'),
            lambda s: s['database']['seed']['bindings'].update(DATABASE_URL='services.payments.url'),
            lambda s: s['database'].update(password='must-not-be-stored'),
            lambda s: s['database'].update(version='18'),
            lambda s: s['database']['seed'].update(workingDirectory='/tmp/local'),
            lambda s: s['database']['seed'].update(timeoutSeconds=True),
            lambda s: s['services']['payments'].update(image={'repository': 'checkout'}),  # platform-supplied
            lambda s: s['database'].update(image='postgres:17.6'),                    # platform-supplied
            lambda s: s['services']['payments'].update(outcome='unknown'),
            lambda s: s.update(suite='https://user:password@registry.example/bundle'),
            lambda s: s.update(suite={'bundle': 'localhost:5001/performance/checkout-suite:1'}),
            lambda s: s['execution']['resources']['requests'].update(cpu='2000m'),
            lambda s: s['execution']['resources']['limits'].update(memory='32Gi'),
            lambda s: s['execution'].update(requestTimeoutSeconds=float('nan')),
            lambda s: s['cleanup'].update(policy='keep'),
            lambda s: s['load'].update(stages=[]),
            lambda s: s.update(load={}),
            lambda s: s['load'].update(model='rps'),
            lambda s: s['load'].update(journeys={'browse': 3}),            # old weights
            lambda s: s.update(loadProfile='baseline'),                    # old separate profile
            lambda s: s['load']['flow'].append({'journey': 'Bad Name'}),
        ]
        for change in changes:
            with self.subTest(change=change):
                doc = deepcopy(self.doc)
                change(doc['spec'])
                with self.assertRaises(ValueError):
                    parse_definition(doc)
        for key, value in (('kind', 'Test'), ('apiVersion', 'v99')):
            doc = deepcopy(self.doc)
            doc[key] = value
            with self.assertRaises(ValueError):
                parse_definition(doc)

    def test_reference_validation(self):
        for value in ('checkout:0.1.0', '/tmp/image', 'oci://host/repo:1', 'host/repo:1',
                      'registry.example/repo', 'registry.example/repo:latest?password=x'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ArtifactRef.parse(value)
        with self.assertRaises(ValueError):
            Registry(http_registries=['registry.example'])

    def artifacts(self, registry, directory):
        """Publish the sample exactly as a team and the platform would, through the real publish code."""
        config = b'{"architecture":"arm64","os":"linux"}'
        for image in ('checkout:0.3.0', PLATFORM.runner_image, PLATFORM.database_image('postgres', '17.6')):
            registry.publish(PLATFORM.image(image), b'image-filesystem',
                'application/vnd.oci.image.config.v1+json', 'application/vnd.oci.image.layer.v1.tar+gzip', config=config)
        subprocess.run(['helm', 'package', str(ROOT / 'platform/charts/http-service'), '-d', str(directory)],
                       check=True, capture_output=True)
        chart = directory / 'http-service-0.1.0.tgz'
        registry.publish(PLATFORM.chart('http-service', '0.1.0'), chart.read_bytes(), HELM_CONFIG, HELM_LAYER)
        publish_application(ROOT / 'ApplicationDefinitions/checkout.yaml', '0.3.0', platform=PLATFORM, registry=registry)
        publish_suite(ROOT / 'TestSuites/checkout', '0.3.0', platform=PLATFORM, registry=registry)
        _, bundle = bundle_files(ROOT / 'TestSuites/checkout/suite.yaml', ROOT / 'TestSuites/checkout')
        return bundle

    def test_offline_resolution_pins_every_role_and_preserves_bundle(self):
        registry = MemoryRegistry()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            bundle = self.artifacts(registry, directory)
            with patch('perf_runner.kubernetes.Kubernetes', side_effect=AssertionError('No provisioning')):
                resolved = resolve_definition(parse_definition(self.doc), directory / 'resolved', platform=PLATFORM, registry=registry)
            snapshot = resolved.snapshot
            self.assertEqual(snapshot['state'], 'resolved')
            self.assertEqual(set(snapshot['artifacts']), {'applicationDefinition', 'chart', 'application', 'database',
                'setup', 'service.payments', 'suiteRuntime', 'scripts'})
            self.assertTrue(all('@sha256:' in v['resolved'] for v in snapshot['artifacts'].values()))
            self.assertEqual(snapshot['application']['spec']['values']['image']['digest'][:7], 'sha256:')
            _, retained = bundle_files(resolved.suite_directory / 'suite.yaml', resolved.suite_directory)
            self.assertEqual(retained, bundle)
            self.assertEqual(bundle_digest({k:v.encode() for k,v in retained.items()}), snapshot['suite']['scriptBundleSha256'])
            self.assertEqual((directory / 'resolved/resolved.json').stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                resolve_definition(parse_definition(self.doc), directory / 'resolved', platform=PLATFORM, registry=registry)

    def test_missing_artifact_and_unknown_journey_never_resolve(self):
        registry = MemoryRegistry()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with self.assertRaises(RuntimeError):
                resolve_definition(parse_definition(self.doc), directory / 'missing', platform=PLATFORM, registry=registry)
            self.assertFalse((directory / 'missing/resolved.json').exists())
            self.assertEqual(json.loads((directory / 'missing/resolution.json').read_text())['state'], 'failed')
            self.artifacts(registry, directory)
            self.doc['spec']['load']['flow'].append({'journey': 'unknown'})
            with self.assertRaises(ValueError):
                resolve_definition(parse_definition(self.doc), directory / 'unknown', platform=PLATFORM, registry=registry)
            self.assertFalse((directory / 'unknown/resolved.json').exists())

    def test_integrity_media_types_and_no_code_execution(self):
        registry = MemoryRegistry()
        reference = 'localhost:5001/performance/blob:1'
        pinned = registry.publish(reference, b'hello', AD_CONFIG, AD_LAYER)
        self.assertEqual(registry.artifact(pinned, AD_CONFIG, AD_LAYER)[1], b'hello')
        with self.assertRaises(ValueError):
            registry.artifact(pinned, SUITE_CONFIG, SUITE_LAYER)
        registry.blobs[checksum(b'hello')] = b'corrupt'
        with self.assertRaises(ValueError):
            registry.artifact(pinned, AD_CONFIG, AD_LAYER)
        with tempfile.TemporaryDirectory() as temporary:
            sentinel = Path(temporary) / 'executed'
            bundle = {'suite.yaml': yaml.safe_dump({'apiVersion':'perf-runner/v1alpha1','kind':'TestSuite',
                'metadata':{'name':'safe'},'spec':{'image':'localhost:5001/image:1',
                'journeys':{'flow':{'callable':'flow:run'}}}}),
                'flow.py': f'from pathlib import Path\nPath({str(sentinel)!r}).touch()\ndef run(context): pass\n'}
            validate_bundle(bundle)
            self.assertFalse(sentinel.exists())
            for filename in ('../escape.py', '/absolute.py', '.hidden.py'):
                with self.assertRaises(ValueError):
                    validate_bundle({**bundle, filename: 'pass'})

    def test_registry_auth_stays_on_selected_origin_and_errors_are_redacted(self):
        from urllib.error import HTTPError
        from unittest.mock import Mock
        registry = Registry(authorization={'registry.example': 'Bearer synthetic-secret'})
        ref = ArtifactRef.parse('registry.example/project/app:1')
        with self.assertRaises(ValueError):
            registry.request(ref, 'https://other.example/upload?token=synthetic-secret', method='PUT', data=b'x')
        with patch.object(registry.opener, 'open', side_effect=HTTPError(
                'https://registry.example/?token=synthetic-secret', 401, 'synthetic-secret', {}, None)) as request:
            with self.assertRaises(RuntimeError) as error:
                registry.request(ref, '/v2/project/app/manifests/1')
            self.assertNotIn('synthetic-secret', str(error.exception))
            self.assertEqual(request.call_args.args[0].get_header('Authorization'), 'Bearer synthetic-secret')
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{}'
        response.headers = {}
        with patch.object(registry.opener, 'open', return_value=response) as request:
            registry.request(ArtifactRef.parse('other.example/project/app:1'), '/v2/')
            self.assertIsNone(request.call_args.args[0].get_header('Authorization'))

    def test_helm_manifest_can_omit_body_media_type(self):
        registry = MemoryRegistry()
        ref = 'localhost:5001/performance/chart:1'
        registry.publish(ref, b'chart', HELM_CONFIG, HELM_LAYER)
        path = '/v2/performance/chart/manifests/1'
        manifest = json.loads(registry.manifests[path])
        manifest.pop('mediaType')
        registry.manifests[path] = json.dumps(manifest).encode()
        pinned, content = registry.artifact(ref, HELM_CONFIG, HELM_LAYER)
        self.assertEqual(content, b'chart')
        self.assertTrue(pinned.endswith(checksum(registry.manifests[path])))

