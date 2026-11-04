"""Provision the run database from pinned images and seed it with a native Job."""
from dataclasses import asdict, dataclass
import json
import math
import secrets
import time

from perf_runner.progress import info
from perf_runner.database import (RUN_LABEL, DatabasePreparationError, SecretKeyRef, _now, _positive, _save,
                                   _write_private, database_resources)
from perf_runner.jobs import collect_job, seed_job_manifest, wait_for_job
from perf_runner.kubernetes import Kubernetes


@dataclass(frozen=True)
class SeedJobResult:
    job: str
    started_at: str
    finished_at: str
    state: str
    log_path: str


@dataclass(frozen=True)
class SeededDatabase:
    host: str
    port: int
    database: str
    user: str
    password_secret: SecretKeyRef
    url_secret: SecretKeyRef
    seed: SeedJobResult


def prepare_published_database(definition, snapshot, *, target, run, redactions):
    """Create PostgreSQL from the resolved image, wait ready, then run the setup image.

    The namespace must already carry this run's ownership label. Existing database
    resources or a previous attempt directory are rejected, never reseeded. The
    generated password is appended to ``redactions`` before any resource exists.
    """
    database = definition.database
    if database.type != 'postgres':
        raise ValueError('Supported database engine: postgres')
    _positive(database.readiness_timeout_seconds, 'Readiness timeout')
    _positive(database.seed.timeout_seconds, 'Seed timeout')
    cluster = Kubernetes(target, run)
    cluster.assert_owned_namespace()
    if cluster.command('get', 'deployment/database', 'service/database', 'secret/database-credentials',
                       '--ignore-not-found', '-o', 'name').strip():
        raise ValueError('Database resources already exist; refusing to overwrite or reseed')
    directory = run.artifacts_dir.resolve() / 'database'
    directory.mkdir(mode=0o700)
    result_path = directory / 'result.json'
    password = secrets.token_urlsafe(32)
    redactions.append(password)
    user = 'user_' + secrets.token_hex(8)
    host = f'database.{run.namespace}.svc.cluster.local'
    pinned = snapshot['database']
    record = {'run_id': run.run_id, 'namespace': run.namespace, 'started_at': _now(), 'state': 'running',
              'phase': 'provision', 'engine': {'type': database.type, 'version': database.version,
                                               'image': pinned['image']},
              'seedImage': pinned['seedImage']}
    _save(result_path, record)
    phase = 'provision'

    def checkpoint(next_phase):
        nonlocal phase
        phase = record['phase'] = next_phase
        _save(result_path, record)

    try:
        info('Creating disposable PostgreSQL storage, Service and generated credential Secret')
        url = f'postgresql://{user}:{password}@{host}:5432/{database.name}'
        for kind, name, fields in database_resources(run.run_id, image=pinned['image'], database=database.name,
                                                     user=user, password=password, url=url):
            cluster.command('create', '-f', '-', data={'apiVersion': 'apps/v1' if kind == 'Deployment' else 'v1',
                'kind': kind, 'metadata': {'name': name, 'labels': {RUN_LABEL: run.run_id}}, **fields})
        checkpoint('readiness')
        info(f'Waiting for PostgreSQL readiness (up to {database.readiness_timeout_seconds}s)')
        timeout = database.readiness_timeout_seconds
        deadline = time.monotonic() + timeout
        cluster.command('rollout', 'status', 'deployment/database', f'--timeout={math.ceil(timeout)}s',
                        timeout=timeout + 5)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Database has no ready Service endpoints')
            slices = json.loads(cluster.command('get', 'endpointslices', '-l',
                'kubernetes.io/service-name=database', '-o', 'json', timeout=min(30, remaining)))
            if any(e.get('conditions', {}).get('ready', False)
                   for item in slices.get('items', []) for e in item.get('endpoints', [])):
                break
            time.sleep(min(.2, remaining))
        info('PostgreSQL is ready and has ready Service endpoints')
        checkpoint('seed')
        info(f'Starting seed Job: {database.seed.module} (deadline {database.seed.timeout_seconds}s; no retries)')
        outputs = {'database.host': 'database', 'database.port': 5432, 'database.database': database.name,
                   'database.user': user, 'database.password': ('database-credentials', 'password'),
                   'database.url': ('database-credentials', 'url')}
        manifest = seed_job_manifest(run_id=run.run_id, image=pinned['seedImage'], module=database.seed.module,
            args=database.seed.args, bindings=database.seed.bindings, outputs=outputs,
            timeout_seconds=database.seed.timeout_seconds)
        started = _now()
        cluster.command('create', '-f', '-', data=manifest)
        state, reason = wait_for_job(cluster, 'database-seed', deadline_seconds=database.seed.timeout_seconds)
        errors = collect_job(cluster, 'database-seed', directory / 'seed',
                             redact=lambda text: text.replace(password, '[REDACTED]'))
        seed = SeedJobResult('database-seed', started, _now(), state, str(directory / 'seed' / 'seed.log'))
        record['seed'] = {**asdict(seed), 'reason': reason, 'collectionErrors': errors}
        info(f'Seed Job {state}' + (f': {reason}' if reason else ''), level='INFO' if state == 'complete' else 'ERROR')
        if state != 'complete':
            raise RuntimeError(f'Seed Job failed ({reason or "container error"})')
        result = SeededDatabase(host, 5432, database.name, user,
            SecretKeyRef(run.namespace, 'database-credentials', 'password'),
            SecretKeyRef(run.namespace, 'database-credentials', 'url'), seed)
        record.update(state='succeeded', outputs=asdict(result))
    except BaseException as error:
        record.update(state='failed' if isinstance(error, Exception) else 'cancelled',
                      error={'type': type(error).__name__, 'message': str(error).replace(password, '[REDACTED]')})
        try:
            if 'seed' not in record and phase == 'seed':
                collect_job(cluster, 'database-seed', directory / 'seed',
                            redact=lambda text: text.replace(password, '[REDACTED]'))
            _write_private(directory / 'diagnostics.json',
                cluster.command('get', 'pods,deployments,services,endpointslices', '-o', 'json',
                                timeout=15).replace(password, '[REDACTED]'))
        except Exception as diagnostic_error:
            record['diagnostic_error'] = type(diagnostic_error).__name__
        if not isinstance(error, Exception):
            raise
        raise DatabasePreparationError(phase, result_path, str(error).replace(password, '[REDACTED]')) from None
    finally:
        record['finished_at'] = _now()
        try:
            _save(result_path, record)
        except Exception:
            if record['state'] == 'succeeded':
                raise DatabasePreparationError('collection', result_path, 'Could not retain result') from None
    return result
