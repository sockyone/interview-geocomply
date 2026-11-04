"""Supported disposable services nested in the local test specification."""
from copy import deepcopy
from dataclasses import dataclass

from perf_runner.application import deploy_application


@dataclass(frozen=True)
class PaymentStubSpec:
    image: dict
    delay_ms: int = 50
    outcome: str = 'approved'


def payment_definition(name, spec, image=None):
    """``image`` is supplied by the platform at resolution; validation alone may omit it."""
    image = image if image is not None else spec.image
    if image is not None and (not isinstance(image, dict) or not image.get('repository')):
        raise ValueError('Payment stub image.repository is required')
    if type(spec.delay_ms) is not int or not 0 <= spec.delay_ms <= 60000:
        raise ValueError('Payment delayMs must be an integer between 0 and 60000')
    if spec.outcome not in ('approved', 'declined', 'error'):
        raise ValueError('Payment outcome must be approved, declined, or error')
    return {'apiVersion': 'perf-runner/v1alpha1', 'kind': 'ApplicationDefinition',
        'metadata': {'name': name}, 'spec': {
            'chart': {'name': 'http-service', 'version': '0.1.0'}, 'values': {
                'image': deepcopy(image) if image else {'repository': 'platform-supplied', 'tag': 'unresolved'}, 'port': 8080, 'service': {'port': 8080},
                'replicaCount': 1,
                'command': ['gunicorn', '--bind', '0.0.0.0:8080', '--workers', '2', '--keep-alive', '30',
                            '--threads', '4', '--access-logfile', '-', 'checkout.payments:app'],
                'probes': {'liveness': {'path': '/healthz'}, 'readiness': {'path': '/healthz'}},
                'env': {'PAYMENT_DELAY_MS': str(spec.delay_ms), 'PAYMENT_OUTCOME': spec.outcome}}}}


def deploy_payment_stub(name, spec, *, chart, target, run, timeout_seconds=180):
    # Names are also used as Helm releases; application is reserved by the caller.
    return deploy_application(payment_definition(name, spec), chart=chart, target=target,
                              run=run, release=name, timeout_seconds=timeout_seconds)
