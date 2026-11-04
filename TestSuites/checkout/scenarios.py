"""Business journeys independent of Locust users, schedules, and deployment settings."""
import random
from uuid import UUID


from suite_runtime import require, payload


def browse(context):
    """View one product page, picked at random from the products the test data expects."""
    prices = context.fixture['productPrices']
    product_id = random.choice(sorted(prices))
    item = payload(context.client.get(f'/products/{product_id}', name='/products/{id}'), 200)
    require(item.get('id') == product_id and item.get('priceCents') == prices[product_id],
            f'Product {product_id} has an unexpected id or price')


def purchase(context):
    fixture = context.fixture
    order = payload(context.client.post('/orders', name='/orders',
        json={'productId': fixture['productId'], 'quantity': fixture['initialQuantity']}), 201)
    order_id = order.get('id')
    require(isinstance(order_id, str), 'Expected an order ID')
    UUID(order_id)
    context.record_id(order_id, namespace='order')
    require(order.get('status') == 'draft', 'New order is not draft')
    path = f'/orders/{order_id}'
    updated = payload(context.client.patch(path, name='/orders/{id}',
                      json={'quantity': fixture['finalQuantity']}), 200)
    amount = fixture['unitPriceCents'] * fixture['finalQuantity']
    require(updated.get('id') == order_id and updated.get('quantity') == fixture['finalQuantity']
            and updated.get('amountCents') == amount, 'Draft update did not persist expected values')
    paid = payload(context.client.post(path + '/checkout', name='/orders/{id}/checkout', json={}), 200)
    require(paid.get('id') == order_id and paid.get('status') == 'paid'
            and bool(paid.get('paymentId')) and paid.get('amountCents') == amount,
            'Checkout response is not the expected paid order')
    persisted = payload(context.client.get(path, name='/orders/{id}'), 200)
    require(persisted.get('id') == order_id and persisted.get('status') == 'paid'
            and persisted.get('quantity') == fixture['finalQuantity']
            and persisted.get('productId') == fixture['productId']
            and persisted.get('amountCents') == amount
            and persisted.get('paymentId') == paid['paymentId'], 'Persisted paid order differs')

