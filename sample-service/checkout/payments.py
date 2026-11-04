"""Deterministic external payment API substitute, for synthetic tests only."""
import os
import time
import uuid

from flask import Flask, jsonify, request

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 65536
DELAY_MS = int(os.environ.get('PAYMENT_DELAY_MS', '50'))
OUTCOME = os.environ.get('PAYMENT_OUTCOME', 'approved')
if DELAY_MS < 0 or OUTCOME not in {'approved', 'declined', 'error'}:
    raise ValueError('Invalid payment stub configuration')


@app.get('/healthz')
def health():
    return jsonify(status='ok')


@app.post('/charges')
def charge():
    data = request.get_json(silent=True)
    if (not isinstance(data, dict) or not isinstance(data.get('orderId'), str)
            or type(data.get('amountCents')) is not int or data['amountCents'] <= 0
            or data.get('currency') != 'USD'):
        return jsonify(error='invalid_charge'), 400
    time.sleep(DELAY_MS / 1000)
    if OUTCOME == 'declined':
        return jsonify(error='payment_declined'), 402
    if OUTCOME == 'error':
        return jsonify(error='payment_unavailable'), 503
    return jsonify(id=str(uuid.uuid5(uuid.NAMESPACE_URL, 'payment:' + data['orderId'])),
                   status='approved'), 201
