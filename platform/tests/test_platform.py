"""Default checks for name-based references and the platform configuration."""
from pathlib import Path
import tempfile
import unittest

import yaml

from perf_runner.platform import load_platform, parse_named

ROOT = Path(__file__).resolve().parents[2]


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.platform = load_platform(ROOT / 'platform/config/local.yaml')

    def test_names_resolve_inside_the_platform_registry(self):
        self.assertEqual(self.platform.application('checkout'),
                         ('checkout', 'localhost:5001/performance/applications/checkout:latest'))
        self.assertEqual(self.platform.suite('checkout:0.3.0')[1], 'localhost:5001/performance/suites/checkout:0.3.0')
        self.assertEqual(self.platform.chart('http-service', '0.1.0'), 'localhost:5001/performance/charts/http-service:0.1.0')

    def test_short_images_use_the_platform_registry_and_full_references_are_kept(self):
        self.assertEqual(self.platform.image('checkout:0.3.0'), 'localhost:5001/performance/checkout:0.3.0')
        self.assertEqual(self.platform.image('ghcr.io/acme/checkout:1.4'), 'ghcr.io/acme/checkout:1.4')
        self.assertEqual(self.platform.image_values({'repository': 'checkout', 'tag': '1'})['repository'],
                         'localhost:5001/performance/checkout')
        self.assertEqual(self.platform.image_values({'repository': 'ghcr.io/acme/checkout', 'tag': '1'})['repository'],
                         'ghcr.io/acme/checkout')

    def test_platform_supplies_runner_database_and_stubs(self):
        self.assertEqual(self.platform.runner_image, 'localhost:5001/performance/python-suite:0.3.0')
        self.assertEqual(self.platform.database_image('postgres', '17.6'), 'localhost:5001/performance/postgres:17.6')
        with self.assertRaisesRegex(ValueError, 'does not provide postgres 18'):
            self.platform.database_image('postgres', '18')
        with self.assertRaisesRegex(ValueError, 'does not provide a kafka'):
            self.platform.service_image('kafka')
        self.assertEqual(self.platform.http_registries, ['localhost:5001'])

    def test_names_reject_links_and_paths(self):
        for value in ('localhost:5001/performance/checkout-ad:1', '../checkout.yaml', 'Checkout', 'oci://x/y:1', ''):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_named(value, 'application.definition')
        self.assertEqual(parse_named('checkout', 'x'), ('checkout', 'latest'))

    def test_invalid_platform_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'platform.yaml'
            document = yaml.safe_load((ROOT / 'platform/config/local.yaml').read_text())
            document['spec']['unexpected'] = True
            path.write_text(yaml.safe_dump(document))
            with self.assertRaises(ValueError):
                load_platform(path)
            with self.assertRaisesRegex(ValueError, 'not found'):
                load_platform(Path(directory) / 'missing.yaml')


if __name__ == '__main__':
    unittest.main()
