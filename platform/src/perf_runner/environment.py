"""Compose local database, supporting-service, and application preparation steps."""
from dataclasses import asdict, dataclass
import argparse
import json
from pathlib import Path
import re

import yaml

from perf_runner.application import (DeployedApplication, bind_application_values, deploy_application,
    validate_application, validate_chart, _helm)
from perf_runner.database import (DatabaseSpec, KubernetesTarget, PreparedDatabase,
    RunContext, SecretKeyRef, _now, _save, cancellation_signals, load_database_spec, prepare_database)
from perf_runner.kubernetes import Kubernetes
from perf_runner.services import PaymentStubSpec, deploy_payment_stub, payment_definition


@dataclass(frozen=True)
class PreparedEnvironment:
    database: PreparedDatabase
    services: dict[str, DeployedApplication]
    application: DeployedApplication


class EnvironmentPreparationError(RuntimeError):
    def __init__(self, phase, result_path, message):
        self.phase, self.result_path = phase, result_path
        super().__init__(f'Environment preparation failed during {phase}: {message}; result: {result_path}')


def database_outputs(database):
    return {'database.host': database.host, 'database.port': database.port,
            'database.database': database.database, 'database.user': database.user,
            'database.password': database.password_secret, 'database.url': database.url_secret}


def prepare_environment(database_spec, application_definition, services, bindings, *, chart, target, run):
    """Leave a ready environment for a caller-owned workload/cleanup lifecycle.

    This local composition is not a complete LoadTestRun. In particular, it
    performs no traffic execution, registry resolution, or namespace creation.
    """
    validate_application(application_definition)
    validate_chart(chart, application_definition['spec']['chart'])
    cluster = Kubernetes(target, run)
    cluster.assert_owned_namespace()
    if not isinstance(services, dict):
        raise ValueError('services must be a mapping')
    # Preflight binding names and effective Helm values before provisioning.
    outputs = {'database.host': 'database', 'database.port': 5432,
        'database.database': database_spec.name, 'database.user': 'user',
        'database.password': SecretKeyRef(run.namespace, 'database-credentials', 'password'),
        'database.url': SecretKeyRef(run.namespace, 'database-credentials', 'url')}
    for name, spec in services.items():
        if not isinstance(name, str) or name in {'application', 'database'} or not re.fullmatch(r'[a-z]([-a-z0-9]{0,51}[a-z0-9])?', name):
            raise ValueError('Service name must be a Helm release name; application/database are reserved')
        definition = payment_definition(name, spec)
        _helm(['template', name, str(chart), '-f', '-'], data=json.dumps(definition['spec']['values']))
        outputs.update({f'services.{name}.host': 'service', f'services.{name}.port': 8080,
                        f'services.{name}.url': 'http://service:8080'})
    effective = bind_application_values(application_definition, bindings, outputs,
                                       namespace=run.namespace, run_id=run.run_id)
    _helm(['template', 'application', str(chart), '-f', '-'], data=json.dumps(effective))
    directory = run.artifacts_dir.resolve() / 'environment'
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    result_path = directory / 'result.json'
    record = {'run_id': run.run_id, 'namespace': run.namespace, 'state': 'running',
              'started_at': _now(), 'phase': 'database'}
    _save(result_path, record)
    try:
        database = prepare_database(database_spec, target=target, run=run)
        outputs = database_outputs(database)
        record['phase'] = 'services'
        _save(result_path, record)
        deployed = {}
        for name, spec in services.items():
            service = deploy_payment_stub(name, spec, chart=chart, target=target, run=run)
            deployed[name] = service
            outputs.update({f'services.{name}.host': service.host,
                            f'services.{name}.port': service.port, f'services.{name}.url': service.url})
        record['phase'] = 'application'
        _save(result_path, record)
        application = deploy_application(application_definition, chart=chart, target=target,
            run=run, bindings=bindings, outputs=outputs)
        result = PreparedEnvironment(database, deployed, application)
        record.update(state='succeeded', outputs=asdict(result))
    except BaseException as error:
        record.update(state='failed' if isinstance(error, Exception) else 'cancelled',
                      error={'type': type(error).__name__, 'message': str(error)})
        if not isinstance(error, Exception):
            raise
        raise EnvironmentPreparationError(record['phase'], result_path, str(error)) from None
    finally:
        record['finished_at'] = _now()
        try:
            _save(result_path, record)
        except Exception:
            if record['state'] == 'succeeded':
                raise EnvironmentPreparationError('collection', result_path, 'Could not retain result') from None
    return result


def load_environment(path):
    """Load a local composition fragment; the full published schema comes later."""
    try:
        content = yaml.safe_load(path.read_text())['spec']
        if set(content) != {'database', 'application', 'services'}:
            raise ValueError('Expected database, application, and services in spec')
        application = content['application']
        if set(application) != {'definition', 'bindings'}:
            raise ValueError('Expected application definition and bindings')
        definition_path = path.resolve().parent / application['definition']
        definition = yaml.safe_load(definition_path.read_text())
        services = {}
        for name, spec in content['services'].items():
            if set(spec) - {'type', 'image', 'delayMs', 'outcome'} or spec['type'] != 'payment-stub':
                raise ValueError('Only payment-stub supporting services are implemented')
            services[name] = PaymentStubSpec(spec['image'], spec.get('delayMs', 50), spec.get('outcome', 'approved'))
        return load_database_spec(path), definition, services, application['bindings']
    except (KeyError, TypeError, AttributeError, yaml.YAMLError) as error:
        raise ValueError('Invalid local environment specification') from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--definition', type=Path, required=True)
    parser.add_argument('--chart', type=Path, required=True)
    parser.add_argument('--kubeconfig', type=Path, required=True)
    parser.add_argument('--context', required=True)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--artifacts', type=Path, required=True)
    options = parser.parse_args()
    try:
        with cancellation_signals():
            result = prepare_environment(*load_environment(options.definition), chart=options.chart,
                target=KubernetesTarget(options.kubeconfig, options.context),
                run=RunContext(options.run_id, options.namespace, options.artifacts))
        print(json.dumps(asdict(result), indent=2))
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(1, f'{error}\n')
    except KeyboardInterrupt:
        parser.exit(130, 'Environment preparation cancelled\n')


if __name__ == '__main__':
    main()
