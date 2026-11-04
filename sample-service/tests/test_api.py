"""Black-box checks against a running application and real PostgreSQL."""
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = os.environ.get('APP_URL', 'http://127.0.0.1:18080')


def call(method, path, data=None):
    req = Request(BASE + path, method=method,
                  data=json.dumps(data).encode() if data is not None else None,
                  headers={'Content-Type': 'application/json'})
    try:
        response = urlopen(req, timeout=10)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


class CheckoutTests(unittest.TestCase):
    def draft(self):
        status, order = call('POST', '/orders', {'productId': 'coffee', 'quantity': 2})
        self.assertEqual(status, 201)
        return order

    def test_complete_journey(self):
        order = self.draft()
        path = '/orders/' + order['id']
        status, updated = call('PATCH', path, {'quantity': 3})
        self.assertEqual((status, updated['amountCents']), (200, 1500))
        status, paid = call('POST', path + '/checkout', {})
        self.assertEqual((status, paid['status']), (200, 'paid'))
        self.assertTrue(paid['paymentId'])
        self.assertEqual(call('GET', path), (200, paid))
        self.assertEqual(call('POST', path + '/checkout', {}), (200, paid))
        self.assertEqual(call('PATCH', path, {'quantity': 1})[0], 409)

    def test_concurrent_checkout_is_consistent(self):
        path = '/orders/' + self.draft()['id'] + '/checkout'
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: call('POST', path, {}), range(4)))
        self.assertTrue(all(status == 200 for status, _ in results))
        self.assertEqual(len({row['paymentId'] for _, row in results}), 1)

    def test_input_validation(self):
        for count in (0, -1, True, '2', 101):
            self.assertEqual(call('POST', '/orders', {'productId': 'coffee', 'quantity': count})[0], 400)
        self.assertEqual(call('POST', '/orders', {'productId': 'missing', 'quantity': 1})[0], 404)

    def test_health_and_catalog(self):
        self.assertEqual(call('GET', '/healthz')[0], 200)
        self.assertEqual(call('GET', '/readyz')[0], 200)
        self.assertEqual(len(call('GET', '/products')[1]['products']), 2)
        status, item = call('GET', '/products/tea')
        self.assertEqual((status, item['id'], item['priceCents']), (200, 'tea', 300))
        self.assertEqual(call('GET', '/products/missing')[0], 404)


if __name__ == '__main__':
    unittest.main()
