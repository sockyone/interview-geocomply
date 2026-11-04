"""Bind resolved outputs and deploy a user ApplicationDefinition via a chart archive."""
from copy import deepcopy
from dataclasses import asdict, dataclass
import json
import math
import time
from pathlib import Path
import re
import secrets

import yaml

from perf_runner.progress import info
from perf_runner.database import RUN_LABEL, SecretKeyRef, _now, _positive, _save, _write_private
from perf_runner.kubernetes import Kubernetes, run_command


@dataclass(frozen=True)
class DeployedApplication:
    release: str
    deployment: str
    service: str
    host: str
    port: int
    url: str
    result_path: str


class ApplicationDeploymentError(RuntimeError):
    def __init__(self, phase, result_path, message):
        self.phase, self.result_path = phase, result_path
        super().__init__(f'Application deployment failed during {phase}: {message}; result: {result_path}')


def validate_application(definition):
    if not isinstance(definition, dict) or set(definition) != {'apiVersion', 'kind', 'metadata', 'spec'}:
        raise ValueError('Expected an ApplicationDefinition YAML document')
    if definition['apiVersion'] != 'perf-runner/v1alpha1' or definition['kind'] != 'ApplicationDefinition':
        raise ValueError('Unsupported ApplicationDefinition kind/version')
    spec = definition['spec']
    if not isinstance(spec, dict) or set(spec) != {'chart', 'values'}:
        raise ValueError('ApplicationDefinition spec requires chart and values')
    if spec['chart'] != {'name': 'http-service', 'version': '0.1.0'}:
        raise ValueError('Supported application chart: http-service 0.1.0')
    if not isinstance(spec['values'], dict):
        raise ValueError('ApplicationDefinition values must be a mapping')
    if not isinstance(definition['metadata'], dict) or not isinstance(definition['metadata'].get('name'), str):
        raise ValueError('ApplicationDefinition metadata.name is required')


def bind_application_values(definition, bindings, outputs, *, namespace, run_id):
    """Bindings map env names to explicit output keys; no expression interpolation."""
    validate_application(definition)
    if not isinstance(bindings, dict):
        raise ValueError('Application bindings must be a mapping')
    values = deepcopy(definition['spec']['values'])
    if not isinstance(values.get('env', {}), dict) or not isinstance(values.get('secretEnv', {}), dict):
        raise ValueError('env and secretEnv must be mappings')
    env = values.setdefault('env', {})
    secret_env = values.setdefault('secretEnv', {})
    for name, source in bindings.items():
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
            raise ValueError('Invalid environment binding name')
        if not isinstance(source, str) or source not in outputs:
            raise ValueError(f'Unknown output binding: {source}')
        value = outputs[source]
        if isinstance(value, SecretKeyRef):
            if value.namespace != namespace:
                raise ValueError('Cannot bind a Secret from another namespace')
            secret_env[name] = {'name': value.name, 'key': value.key}
            env.pop(name, None)
        elif type(value) in (str, int):
            env[name] = str(value)
            secret_env.pop(name, None)
        else:
            raise ValueError(f'Unsupported output type for {source}')
    values['runId'] = run_id
    return values


def _helm(args, *, timeout=30, data=None):
    result = run_command(['helm', *args], data=data, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout


def validate_chart(chart, selection):
    chart = Path(chart)
    if not chart.is_file() or not chart.name.endswith('.tgz'):
        raise ValueError('Provide the packaged supported chart (.tgz)')
    metadata = yaml.safe_load(_helm(['show', 'chart', str(chart)]))
    if any(metadata.get(key) != selection[key] for key in ('name', 'version')):
        raise ValueError('Packaged chart does not match the AD selection')


def deploy_application(definition, *, chart, target, run, bindings=None, outputs=None,
                       release='application', timeout_seconds=180):
    """Install/upgrade only in a run-owned namespace; retain each deployment attempt."""
    _positive(timeout_seconds, 'Application readiness timeout')
    if not isinstance(release, str) or not re.fullmatch(r'[a-z]([-a-z0-9]{0,51}[a-z0-9])?', release):
        raise ValueError('Invalid Helm release name (maximum 53 characters)')
    values = bind_application_values(definition, bindings or {}, outputs or {},
                                    namespace=run.namespace, run_id=run.run_id)
    validate_chart(chart, definition['spec']['chart'])
    cluster = Kubernetes(target, run)
    cluster.assert_owned_namespace()
    scope = ['--kubeconfig', str(target.kubeconfig.resolve()), '--kube-context', target.context,
             '--namespace', run.namespace]
    releases = json.loads(_helm(['list', *scope, '--deployed', '--failed', '--pending', '--uninstalling',
                                 '--uninstalled', '--superseded', '--filter', f'^{release}$', '-o', 'json']))
    if releases:
        previous = json.loads(_helm(['get', 'values', release, *scope, '-o', 'json']))
        if (not isinstance(previous, dict) or previous.get('runId') != run.run_id
                or releases[0]['chart'] != 'http-service-0.1.0'):
            raise ValueError('Refusing to replace a Helm release not owned by this run/chart')
    name = (release + '-http-service')[:63].rstrip('-')
    existing = cluster.command('get', f'deployment/{name}', '--ignore-not-found', '-o', 'json')
    if existing and json.loads(existing)['metadata'].get('labels', {}).get(RUN_LABEL) != run.run_id:
        raise ValueError('Refusing to replace a Deployment not owned by this run')
    directory = run.artifacts_dir.resolve() / 'applications' / release / secrets.token_hex(6)
    directory.mkdir(parents=True, mode=0o700)
    values_path, result_path = directory / 'values.json', directory / 'result.json'
    _write_private(values_path, json.dumps(values, indent=2))
    record = {'run_id': run.run_id, 'namespace': run.namespace, 'release': release,
              'chart': definition['spec']['chart'], 'started_at': _now(),
              'state': 'running', 'phase': 'render', 'collection_errors': []}
    _save(result_path, record)
    try:
        info(f'{release}: rendering the resolved chart')
        # Helm's schema and templates validate effective values before mutation.
        _helm(['template', release, str(chart), '-f', str(values_path)])
        record['phase'] = 'deploy'
        _save(result_path, record)
        deadline = time.monotonic() + timeout_seconds
        info(f'{release}: installing release and waiting for ready replicas (up to {timeout_seconds}s)')
        _helm(['upgrade', '--install', release, str(chart), '-f', str(values_path),
               '--kubeconfig', str(target.kubeconfig.resolve()), '--kube-context', target.context,
               '--namespace', run.namespace, '--wait', '--timeout', f'{timeout_seconds}s'],
               timeout=timeout_seconds + 15)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Application rollout readiness deadline exceeded')
        cluster.command('rollout', 'status', f'deployment/{name}',
                        f'--timeout={math.ceil(remaining)}s', timeout=remaining + 5)
        actual = json.loads(cluster.command('get', f'deployment/{name}', '-o', 'json'))
        if actual['metadata']['labels'].get(RUN_LABEL) != run.run_id:
            raise RuntimeError('Deployment ownership label missing')
        port = values.get('service', {}).get('port', 8080)
        host = f'{name}.{run.namespace}.svc.cluster.local'
        result = DeployedApplication(release, name, name, host, port, f'http://{host}:{port}', str(result_path))
        record.update(state='succeeded', outputs=asdict(result))
        info(f'{release}: deployment ready, service port {port}')
    except BaseException as error:
        record.update(state='failed' if isinstance(error, Exception) else 'cancelled',
                      error={'type': type(error).__name__, 'message': str(error)})
        for filename, args in [('resources.json', ['get', 'pods,deployments,services', '-l',
                f'app.kubernetes.io/instance={release}', '-o', 'json']),
                ('application.log', ['logs', f'deployment/{name}', '--all-containers=true', '--tail=200'])]:
            try:
                _write_private(directory / filename, cluster.command(*args, timeout=10))
            except Exception as collection_error:
                record['collection_errors'].append(type(collection_error).__name__)
        if not isinstance(error, Exception):
            raise
        raise ApplicationDeploymentError(record['phase'], result_path, str(error)) from None
    finally:
        record['finished_at'] = _now()
        try:
            _save(result_path, record)
        except Exception:
            if record['state'] == 'succeeded':
                raise ApplicationDeploymentError('collection', result_path, 'Could not retain result') from None
    return result
