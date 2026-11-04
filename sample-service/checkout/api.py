"""HTTP API: draft orders, updates, checkout and persisted reads."""
import json
import os
import socket
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import psycopg
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from checkout.database import connect

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 65536
PAYMENT_URL = os.environ.get('PAYMENT_BASE_URL', 'http://payments:8080').rstrip('/')
PAYMENT_TIMEOUT = float(os.environ.get('PAYMENT_TIMEOUT_MS', '1000')) / 1000
if PAYMENT_TIMEOUT <= 0:
    raise ValueError('PAYMENT_TIMEOUT_MS must be positive')


class APIError(Exception):
    def __init__(self, code, status):
        self.code, self.status = code, status


@app.errorhandler(APIError)
def api_error(error):
    return jsonify(error=error.code), error.status


@app.errorhandler(HTTPException)
def http_error(error):
    return jsonify(error=error.name.lower().replace(' ', '_')), error.code


@app.errorhandler(psycopg.Error)
def database_error(error):
    app.logger.warning('Database operation failed: %s', type(error).__name__)
    return jsonify(error='database_unavailable'), 503


def body():
    value = request.get_json()
    if not isinstance(value, dict):
        raise APIError('invalid_body', 400)
    return value


def quantity(value):
    if type(value) is not int or not 1 <= value <= 100:
        raise APIError('quantity_must_be_integer_1_to_100', 400)
    return value


def order_json(row):
    return dict(id=str(row['id']), productId=row['product_id'], quantity=row['quantity'],
                amountCents=row['quantity'] * row['unit_price_cents'],
                status=row['status'], paymentId=row['payment_id'])


def find_order(db, order_id, lock=False):
    row = db.execute('SELECT * FROM orders WHERE id=%s' + (' FOR UPDATE' if lock else ''),
                     (order_id,)).fetchone()
    if row is None:
        raise APIError('order_not_found', 404)
    return row


@app.get('/healthz')
def health():
    return jsonify(status='ok')


@app.get('/readyz')
def ready():
    with connect() as db:
        db.execute('SELECT id FROM products LIMIT 1')
        db.execute('SELECT id FROM orders LIMIT 1')
    return jsonify(status='ready')


@app.get('/products')
def products():
    with connect() as db:
        rows = db.execute('SELECT * FROM products ORDER BY id').fetchall()
    return jsonify(products=[dict(id=r['id'], name=r['name'], priceCents=r['price_cents'])
                             for r in rows])


@app.get('/products/<product_id>')
def product(product_id):
    with connect() as db:
        row = db.execute('SELECT * FROM products WHERE id = %s', (product_id,)).fetchone()
    if row is None:
        raise APIError('product_not_found', 404)
    return jsonify(id=row['id'], name=row['name'], priceCents=row['price_cents'])


@app.post('/orders')
def create_order():
    data = body()
    count = quantity(data.get('quantity'))
    product = data.get('productId')
    if not isinstance(product, str):
        raise APIError('product_id_required', 400)
    with connect() as db:
        item = db.execute('SELECT * FROM products WHERE id=%s', (product,)).fetchone()
        if item is None:
            raise APIError('product_not_found', 404)
        row = db.execute('''INSERT INTO orders (id,product_id,quantity,unit_price_cents)
            VALUES (%s,%s,%s,%s) RETURNING *''',
            (uuid.uuid4(), product, count, item['price_cents'])).fetchone()
    return jsonify(order_json(row)), 201


@app.get('/orders/<uuid:order_id>')
def get_order(order_id):
    with connect() as db:
        row = find_order(db, order_id)
    return jsonify(order_json(row))


@app.patch('/orders/<uuid:order_id>')
def update_order(order_id):
    count = quantity(body().get('quantity'))
    with connect() as db:
        row = find_order(db, order_id, lock=True)
        if row['status'] != 'draft':
            raise APIError('order_already_paid', 409)
        row = db.execute('UPDATE orders SET quantity=%s WHERE id=%s RETURNING *',
                         (count, order_id)).fetchone()
    return jsonify(order_json(row))


def charge(row):
    payload = dict(orderId=str(row['id']), amountCents=row['quantity'] * row['unit_price_cents'],
                   currency='USD')
    req = Request(PAYMENT_URL + '/charges', json.dumps(payload).encode(),
                  headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urlopen(req, timeout=PAYMENT_TIMEOUT) as response:
            result = json.load(response)
            if response.status != 201 or result.get('status') != 'approved' or not result.get('id'):
                raise APIError('invalid_payment_response', 502)
            return result['id']
    except HTTPError as error:
        raise APIError('payment_declined' if error.code == 402 else 'payment_unavailable',
                       422 if error.code == 402 else 502) from error
    except (TimeoutError, socket.timeout) as error:
        raise APIError('payment_timeout', 504) from error
    except URLError as error:
        raise APIError('payment_timeout' if isinstance(error.reason, TimeoutError)
                       else 'payment_unavailable',
                       504 if isinstance(error.reason, TimeoutError) else 502) from error
    except (ValueError, AttributeError) as error:
        raise APIError('invalid_payment_response', 502) from error


@app.post('/orders/<uuid:order_id>/checkout')
def checkout(order_id):
    # Serializes checkout/update of the same order. Payment must honor orderId
    # idempotency: payment may succeed even if our subsequent DB commit fails.
    with connect() as db:
        row = find_order(db, order_id, lock=True)
        if row['status'] != 'paid':
            payment_id = charge(row)
            row = db.execute("UPDATE orders SET status='paid', payment_id=%s WHERE id=%s RETURNING *",
                             (payment_id, order_id)).fetchone()
    return jsonify(order_json(row))
