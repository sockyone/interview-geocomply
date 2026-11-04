"""Journey correctness and fail-closed result contracts without a load engine."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'platform/runner'))
from suite_runtime.loader import load_suite
from suite_runtime.results import create_result, save_result
from suite_runtime.client import JourneyClient


class Response:
    def __init__(self, value, status=200):
        self.status_code, self.value = status, value

    def json(self):
        return deepcopy(self.value)


class SuiteContractTests(unittest.TestCase):
    def test_descriptor_matches_packaged_catalog_and_has_no_load_settings(self):
        definition = yaml.safe_load((ROOT / 'TestSuites/checkout/suite.yaml').read_text())
        self.assertEqual(definition['kind'], 'TestSuite')
        self.assertEqual(set(definition['spec']), {'fixtures', 'journeys'})  # runner image is platform-owned
        with load_suite(ROOT / 'TestSuites/checkout/suite.yaml') as suite:
            self.assertEqual({key: value['description'] for key, value in definition['spec']['journeys'].items()}, suite.descriptions)
            self.assertEqual(set(suite.journeys), {'purchase', 'browse'})

    def test_persisted_state_mismatch_is_a_business_failure(self):
        from types import SimpleNamespace
        fixture = json.loads((ROOT / 'TestSuites/checkout/fixtures.json').read_text())
        order_id = str(uuid4())
        paid = {'id': order_id, 'status': 'paid', 'quantity': 3, 'productId': 'coffee',
                'amountCents': 1500, 'paymentId': 'payment-1'}
        class Client:
            def post(self, path, **kwargs):
                return Response({'id': order_id, 'status': 'draft'}, 201) if path == '/orders' else Response(paid)
            def patch(self, path, **kwargs):
                return Response(paid)
            def get(self, path, **kwargs):
                return Response({**paid, 'status': 'draft'})
        with load_suite(ROOT / 'TestSuites/checkout/suite.yaml') as suite:
            with self.assertRaisesRegex(AssertionError, 'Persisted'):
                suite.journeys['purchase'](SimpleNamespace(client=Client(), fixture=fixture, record_id=lambda *args, **kwargs: None))

    def test_client_has_timeouts_trace_headers_and_no_redirects(self):
        from unittest.mock import Mock
        session = Mock()
        client = JourneyClient(session, run_id='run-1', timeout=2, bearer_token='synthetic-token')
        client.post('/orders', name='/orders', json={'quantity': 2})
        options = session.request.call_args.kwargs
        self.assertEqual(options['timeout'], (2, 2))
        self.assertFalse(options['allow_redirects'])
        self.assertEqual(options['headers']['X-Perf-Run-Id'], 'run-1')
        self.assertEqual(session.request.call_count, 1)

    def test_started_result_is_incomplete_and_existing_result_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'result.json'
            result = create_result(path, 'run-1', ('purchase',), 1)
            self.assertEqual(json.loads(path.read_text())['outcome'], 'incomplete')
            with self.assertRaises(FileExistsError):
                create_result(path, 'run-1', ('purchase',), 1)
            result['state'], result['outcome'] = 'interrupted', 'failed'
            save_result(path, result)
            self.assertEqual(json.loads(path.read_text())['outcome'], 'failed')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
