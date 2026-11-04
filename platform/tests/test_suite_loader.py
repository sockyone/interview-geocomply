"""User-authored script loading, snapshots, validation, and publication boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'platform/runner'))
from suite_runtime.loader import load_suite
from perf_runner.suite_scripts import bundle_files


class UserSuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.definition = {'apiVersion': 'perf-runner/v1alpha1', 'kind': 'TestSuite',
            'metadata': {'name': 'user-example'}, 'spec': {
                'image': 'python-suite:0.1.0', 
                'journeys': {'my-flow': {'callable': 'flows:run'}}}}
        self.write_definition()
        (self.root / 'flows.py').write_text('from .helpers import value\ndef run(context):\n    return value()\n')
        (self.root / 'helpers.py').write_text('def value():\n    return 42\n')

    def write_definition(self):
        (self.root / 'suite.yaml').write_text(yaml.safe_dump(self.definition))

    def test_user_flow_relative_imports_and_content_hash_change(self):
        with load_suite(self.root / 'suite.yaml') as first:
            self.assertEqual(first.journeys['my-flow'](None), 42)
            first_hash = first.digest
            # The loaded snapshot is isolated from subsequent authoring changes.
            (self.root / 'helpers.py').write_text('def value():\n    return 99\n')
            self.assertEqual(first.journeys['my-flow'](None), 42)
        with load_suite(self.root / 'suite.yaml') as second:
            self.assertEqual(second.journeys['my-flow'](None), 99)
            self.assertNotEqual(second.digest, first_hash)

    def test_projected_configmap_symlinks_are_supported(self):
        revision = self.root / '.revision'
        revision.mkdir()
        for path in list(self.root.iterdir()):
            if path.is_file():
                path.rename(revision / path.name)
                path.symlink_to(Path('.revision') / path.name)
        with load_suite(self.root / 'suite.yaml') as suite:
            self.assertEqual(suite.journeys['my-flow'](None), 42)

    def test_missing_async_and_invalid_entries_are_rejected(self):
        for reference in ('../flows:run', 'missing:run', 'flows:missing'):
            self.definition['spec']['journeys']['my-flow']['callable'] = reference
            self.write_definition()
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                with load_suite(self.root / 'suite.yaml'):
                    pass
        self.definition['spec']['journeys']['my-flow']['callable'] = 'flows:run'
        self.write_definition()
        (self.root / 'flows.py').write_text('async def run(context):\n    return None\n')
        with self.assertRaises(ValueError):
            with load_suite(self.root / 'suite.yaml'):
                pass

    def test_user_and_helper_assertions_remain_active_with_optimized_python(self):
        (self.root / 'helpers.py').write_text('def value():\n    assert False, "user assertion"\n')
        code = '''from pathlib import Path
import sys
from suite_runtime.loader import load_suite
with load_suite(Path(sys.argv[1])) as suite:
    try:
        suite.journeys['my-flow'](None)
    except AssertionError:
        sys.exit(0)
    sys.exit(1)
'''
        result = subprocess.run([sys.executable, '-O', '-c', code, str(self.root / 'suite.yaml')],
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'platform/runner')},
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_oversize_bundle_and_missing_source_fail_before_publication(self):
        (self.root / 'large.json').write_text('x' * (901 * 1024))
        with self.assertRaisesRegex(ValueError, '900 KiB'):
            bundle_files(self.root / 'suite.yaml', self.root)
        (self.root / 'large.json').unlink()
        (self.root / 'flows.py').unlink()
        with self.assertRaisesRegex(ValueError, 'Python module'):
            bundle_files(self.root / 'suite.yaml', self.root)
