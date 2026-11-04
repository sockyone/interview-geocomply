"""Prepare an isolated PostgreSQL database and seed it from local Python/data."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from urllib.parse import quote

RUN_LABEL = 'perf-runner/run-id'
POSTGRES_IMAGES = {'17.6': 'postgres:17.6'}


@dataclass(frozen=True)
class KubernetesTarget:
    kubeconfig: Path
    context: str


@dataclass(frozen=True)
class RunContext:
    run_id: str
    namespace: str
    artifacts_dir: Path


@dataclass(frozen=True)
class SeedSpec:
    module: str
    working_directory: Path
    args: tuple[str, ...] = ()
    timeout_seconds: float = 60
    python_executable: str = sys.executable


@dataclass(frozen=True)
class DatabaseSpec:
    type: str
    version: str
    name: str
    seed: SeedSpec
    readiness_timeout_seconds: float = 150


@dataclass(frozen=True)
class SecretKeyRef:
    namespace: str
    name: str
    key: str


@dataclass(frozen=True)
class SeedResult:
    pid: int
    started_at: str
    finished_at: str
    exit_code: int
    log_path: str


@dataclass(frozen=True)
class PreparedDatabase:
    host: str
    port: int
    database: str
    user: str
    password_secret: SecretKeyRef
    url_secret: SecretKeyRef
    seed: SeedResult


class DatabasePreparationError(RuntimeError):
    def __init__(self, phase, result_path, message):
        self.phase, self.result_path = phase, result_path
        super().__init__(f'Database preparation failed during {phase}: {message}; result: {result_path}')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _write_private(path, text):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(text)


def _save(path, record):
    temporary = path.with_suffix('.tmp')
    _write_private(temporary, json.dumps(record, indent=2) + '\n')
    temporary.replace(path)


def _stop(process):
    """Stop the entire child session, including descendants whose parent exited."""
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=2)


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 3600:
        raise ValueError(f'{name} must be positive and at most 3600 seconds')


def database_resources(run_id, *, image, database, user, password, url):
    """Native PostgreSQL resources shared by local and published-seed preparation."""
    return [
        ('Secret', 'database-credentials', {'stringData': {'password': password, 'url': url}}),
        ('Service', 'database', {'spec': {'selector': {RUN_LABEL: run_id, 'app': 'database'},
            'ports': [{'name': 'postgres', 'port': 5432, 'targetPort': 5432}]}}),
        ('Deployment', 'database', {'spec': {
            'replicas': 1, 'selector': {'matchLabels': {RUN_LABEL: run_id, 'app': 'database'}},
            'template': {'metadata': {'labels': {RUN_LABEL: run_id, 'app': 'database'}}, 'spec': {
                'containers': [{'name': 'postgres', 'image': image,
                    'ports': [{'containerPort': 5432}],
                    'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'},
                                  'limits': {'cpu': '1', 'memory': '512Mi'}},
                    'env': [{'name': 'POSTGRES_DB', 'value': database},
                            {'name': 'POSTGRES_USER', 'value': user},
                            {'name': 'POSTGRES_PASSWORD', 'valueFrom': {'secretKeyRef': {
                                'name': 'database-credentials', 'key': 'password'}}}],
                    'readinessProbe': {'exec': {'command': ['pg_isready', '-h', '127.0.0.1',
                                       '-U', user, '-d', database]}, 'periodSeconds': 2, 'timeoutSeconds': 2},
                    'volumeMounts': [{'name': 'data', 'mountPath': '/var/lib/postgresql/data'}]}],
                'volumes': [{'name': 'data', 'emptyDir': {}}]}}}}),
    ]


def _validate(spec, target, run):
    if spec.type != 'postgres' or not isinstance(spec.version, str) or spec.version not in POSTGRES_IMAGES:
        raise ValueError('Supported database engine: postgres 17.6')
    if not isinstance(spec.name, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', spec.name):
        raise ValueError('Invalid database name')
    for value in (run.run_id, run.namespace):
        if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?', value):
            raise ValueError('Run ID and namespace must be DNS labels, at most 63 characters')
    if not target.kubeconfig.is_file() or not target.context or target.context.startswith('-'):
        raise ValueError('Explicit kubeconfig file and context required')
    if not spec.seed.working_directory.is_dir():
        raise ValueError('Seed working directory does not exist')
    if not isinstance(spec.seed.module, str) or not re.fullmatch(r'[A-Za-z_]\w*(\.[A-Za-z_]\w*)*', spec.seed.module):
        raise ValueError('Invalid Python seed module')
    if not all(isinstance(arg, str) and '\0' not in arg for arg in spec.seed.args):
        raise ValueError('Seed args must contain strings')
    if not shutil.which(spec.seed.python_executable):
        raise ValueError('Seed Python executable does not exist')
    _positive(spec.seed.timeout_seconds, 'Seed timeout')
    _positive(spec.readiness_timeout_seconds, 'Readiness timeout')


def prepare_database(spec: DatabaseSpec, *, target: KubernetesTarget,
                     run: RunContext) -> PreparedDatabase:
    """Provision and seed once; caller owns namespace creation and final cleanup.

    The namespace must carry RUN_LABEL=run.run_id. A retained attempt directory
    prevents accidental reseeding, including after a crash. Published/remote seed
    execution is deliberately outside this local-development implementation.
    """
    _validate(spec, target, run)
    directory = run.artifacts_dir.resolve() / 'database'
    result_path = directory / 'result.json'
    phase = 'validate'
    password = secrets.token_urlsafe(32)
    user = 'user_' + secrets.token_hex(8)
    host = f'database.{run.namespace}.svc.cluster.local'
    base = ['kubectl', '--kubeconfig', str(target.kubeconfig.resolve()),
            '--context', target.context, '--namespace', run.namespace]

    def redact(text):
        return text.replace(password, '[REDACTED]').replace(quote(password, safe=''), '[REDACTED]')

    def kubectl(*args, data=None, timeout=30):
        result = subprocess.run(base + list(args), input=json.dumps(data) if data else None,
                                capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(redact(result.stderr.strip()))
        return result.stdout

    # Validate namespace ownership before creating any resources or an attempt.
    try:
        namespace = json.loads(kubectl('get', 'namespace', run.namespace, '-o', 'json'))
        existing = kubectl('get', 'deployment/database', 'service/database',
                           'secret/database-credentials', '--ignore-not-found', '-o', 'name')
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
        raise DatabasePreparationError('validate', result_path, str(error)) from None
    if namespace['metadata'].get('labels', {}).get(RUN_LABEL) != run.run_id:
        raise ValueError('Namespace is not owned by this run')
    if existing.strip():
        raise ValueError('Database resources already exist; refusing to overwrite or reseed')
    directory.parent.mkdir(parents=True, exist_ok=True)
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        raise ValueError(f'Database preparation already attempted: {result_path}') from None
    record = {'run_id': run.run_id, 'namespace': run.namespace, 'started_at': _now(),
              'state': 'running', 'phase': phase, 'cleanup_errors': [],
              'engine': {'type': spec.type, 'version': spec.version, 'image': POSTGRES_IMAGES[spec.version]},
              'resources': ['deployment/database', 'service/database', 'secret/database-credentials']}
    forward = seed = None
    seed_log = directory / 'seed.log'
    forward_log = directory / 'port-forward.log'

    def checkpoint(next_phase):
        nonlocal phase
        phase = next_phase
        record['phase'] = phase
        _save(result_path, record)

    def create(kind, name, **fields):
        api_version = 'apps/v1' if kind == 'Deployment' else 'v1'
        kubectl('create', '-f', '-', data={'apiVersion': api_version, 'kind': kind,
                'metadata': {'name': name, 'labels': {RUN_LABEL: run.run_id}}, **fields})

    try:
        checkpoint('provision')
        url = f'postgresql://{user}:{password}@{host}:5432/{spec.name}'
        for kind, name, fields in database_resources(run.run_id, image=POSTGRES_IMAGES[spec.version],
                                                     database=spec.name, user=user, password=password, url=url):
            create(kind, name, **fields)
        checkpoint('readiness')
        deadline = time.monotonic() + spec.readiness_timeout_seconds
        kubectl('rollout', 'status', 'deployment/database',
                f'--timeout={math.ceil(spec.readiness_timeout_seconds)}s',
                timeout=spec.readiness_timeout_seconds + 5)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Database has no ready Service endpoints')
            endpoints = json.loads(kubectl('get', 'endpointslices', '-l',
                'kubernetes.io/service-name=database', '-o', 'json', timeout=min(30, remaining)))
            if any(e.get('conditions', {}).get('ready', False)
                   for item in endpoints.get('items', []) for e in item.get('endpoints', [])):
                break
            time.sleep(min(.2, remaining))
        checkpoint('port-forward')
        _write_private(forward_log, '')
        with forward_log.open('a') as output:
            forward = subprocess.Popen(base + ['port-forward', 'service/database', ':5432',
                                       '--address', '127.0.0.1'], stdout=output, stderr=output,
                                       start_new_session=True)
        record['forward_pid'] = forward.pid
        _save(result_path, record)
        deadline = time.monotonic() + 15
        while True:
            if forward.poll() is not None:
                raise RuntimeError('Database port-forward exited before startup')
            match = re.search(r'Forwarding from 127\.0\.0\.1:(\d+) -> 5432', forward_log.read_text())
            if match:
                local_port = int(match[1])
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('Database port-forward startup timed out')
            time.sleep(.1)
        checkpoint('seed')
        _write_private(seed_log, '')
        environment = {**os.environ, 'DATABASE_URL':
                       f'postgresql://{user}:{password}@127.0.0.1:{local_port}/{spec.name}'}
        started_at = _now()
        with seed_log.open('a') as output:
            seed = subprocess.Popen([spec.seed.python_executable, '-m', spec.seed.module, *spec.seed.args],
                        cwd=spec.seed.working_directory, env=environment,
                        stdout=output, stderr=output, start_new_session=True)
        record['seed_pid'] = seed.pid
        _save(result_path, record)
        deadline = time.monotonic() + spec.seed.timeout_seconds
        while seed.poll() is None:
            if forward.poll() is not None:
                raise RuntimeError('Database port-forward exited during seeding')
            if time.monotonic() >= deadline:
                raise TimeoutError('Seed process timed out')
            time.sleep(.1)
        record['seed'] = asdict(SeedResult(seed.pid, started_at, _now(), seed.returncode, str(seed_log)))
        if seed.returncode != 0:
            raise RuntimeError(f'Seed process exited with code {seed.returncode}')
        if forward.poll() is not None:
            raise RuntimeError('Database port-forward exited during seeding')
        result = PreparedDatabase(host, 5432, spec.name, user,
            SecretKeyRef(run.namespace, 'database-credentials', 'password'),
            SecretKeyRef(run.namespace, 'database-credentials', 'url'),
            SeedResult(**record['seed']))
        record.update(state='succeeded', outputs=asdict(result))
    except BaseException as error:
        record.update(state='cancelled' if isinstance(error, (KeyboardInterrupt, SystemExit)) else 'failed',
                      error={'type': type(error).__name__, 'message': redact(str(error))})
        try:
            diagnostics = kubectl('get', 'pods,deployments,services,endpointslices', '-o', 'json', timeout=10)
            _write_private(directory / 'diagnostics.json', redact(diagnostics))
        except Exception as diagnostic_error:
            record['diagnostic_error'] = type(diagnostic_error).__name__
        if not isinstance(error, Exception):
            raise
        raise DatabasePreparationError(phase, result_path, redact(str(error))) from None
    finally:
        for process in (seed, forward):
            try:
                _stop(process)
            except Exception as error:
                record['cleanup_errors'].append(type(error).__name__)
        if seed is not None and 'seed' not in record:
            record['seed'] = asdict(SeedResult(seed.pid, started_at, _now(), seed.returncode, str(seed_log)))
        for path in (seed_log, forward_log):
            try:
                if path.exists():
                    _write_private(path, redact(path.read_text(errors='replace')))
            except Exception as error:
                record['cleanup_errors'].append(f'log: {type(error).__name__}')
        record['finished_at'] = _now()
        if record['cleanup_errors'] and record['state'] == 'succeeded':
            record['state'] = 'failed'
        try:
            _save(result_path, record)
        except Exception:
            # Preserve an active primary failure/cancellation if retention also fails.
            if record['state'] == 'succeeded':
                raise DatabasePreparationError('collection', result_path, 'Could not retain result') from None
    if record['state'] != 'succeeded':
        raise DatabasePreparationError('cleanup', result_path, 'Local process cleanup failed')
    return result


def load_database_spec(path: Path) -> DatabaseSpec:
    """Load the local database fragment; full LoadTestDefinition validation is later."""
    import yaml
    try:
        data = yaml.safe_load(path.read_text())['spec']['database']
        seed = data['seed']
        if set(data) - {'type', 'version', 'name', 'seed', 'readinessTimeoutSeconds'}:
            raise ValueError('Unknown database setting')
        if set(seed) - {'module', 'args', 'workingDirectory', 'timeoutSeconds'}:
            raise ValueError('Unknown seed setting')
        if not isinstance(seed['workingDirectory'], str):
            raise ValueError('Seed workingDirectory must be a path string')
        args = seed.get('args', [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError('Seed args must be a list of strings')
        return DatabaseSpec(type=data['type'], version=data['version'], name=data['name'],
            seed=SeedSpec(module=seed['module'], args=tuple(args),
                          working_directory=(path.resolve().parent / seed['workingDirectory']).resolve(),
                          timeout_seconds=seed.get('timeoutSeconds', 60)),
            readiness_timeout_seconds=data.get('readinessTimeoutSeconds', 150))
    except (KeyError, TypeError, yaml.YAMLError) as error:
        raise ValueError('Invalid database specification') from error



@contextmanager
def cancellation_signals():
    """CLI adapter; library callers own their signal handling."""
    def cancel(signum, frame):
        raise KeyboardInterrupt(f'Cancelled by signal {signum}')
    previous = {sig: signal.signal(sig, cancel) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--definition', type=Path, required=True)
    parser.add_argument('--kubeconfig', type=Path, required=True)
    parser.add_argument('--context', required=True)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--artifacts', type=Path, required=True)
    options = parser.parse_args()
    try:
        with cancellation_signals():
            result = prepare_database(load_database_spec(options.definition),
                target=KubernetesTarget(options.kubeconfig, options.context),
                run=RunContext(options.run_id, options.namespace, options.artifacts))
        print(json.dumps(asdict(result), indent=2))
    except (DatabasePreparationError, ValueError, OSError, KeyError) as error:
        parser.exit(1, f'{error}\n')
    except KeyboardInterrupt:
        parser.exit(130, 'Database preparation cancelled\n')


if __name__ == '__main__':
    main()
