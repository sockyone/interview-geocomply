"""LoadTestRun: one bounded, owned lifecycle for a published LoadTestDefinition.

validate -> resolve -> owned namespace -> database -> seed Job -> services ->
application -> suite Job -> collect -> cleanup -> final record.

Every run has a unique ID, namespace, credentials and result directory. The run
record and resolved snapshot are written outside the namespace before anything is
provisioned. Only resources carrying this run's ownership label are deleted; the
shared cluster and registry are never touched. A hard kill (SIGKILL, power loss)
cannot run cleanup: the record keeps the namespace and context so that the
explicit ``cleanup`` command can remove the abandoned namespace.
"""
import argparse
from datetime import datetime
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import time

from perf_runner.progress import Progress, info
from perf_runner.application import deploy_application
from perf_runner.database import RUN_LABEL, KubernetesTarget, RunContext, _now, _save, _write_private, cancellation_signals
from perf_runner.definition import load_definition
from perf_runner.environment import database_outputs
from perf_runner.guardrails import namespace_manifests
from perf_runner.jobs import collect_job, profile_config_map, suite_job_manifest, wait_for_job
from perf_runner.kubernetes import Kubernetes
from perf_runner.metrics import CADVISOR_REFRESH_SECONDS, export_resource_usage, series_csv
from perf_runner.report import build_test_result, render_report
from perf_runner.platform import load_platform
from perf_runner.resolution import registry_for, resolve_definition
from perf_runner.seed import prepare_published_database
from perf_runner.suite_scripts import publish_suite_scripts

# 0 passed; 1 performance/correctness failure; 2 infrastructure or incomplete execution;
# 3 test passed but retention or cleanup failed; 130 cancelled.
EXIT_CODES = {'passed': 0, 'failed': 1, 'error': 2, 'passed-with-errors': 3, 'cancelled': 130}
TEXT_SUFFIXES = {'.json', '.jsonl', '.log', '.yaml', '.yml', '.txt'}
PHASE_LABELS = dict(validate='[1/11] Checking Kubernetes access', resolve='[2/11] Resolving and validating artifacts',
    namespace='[3/11] Creating an isolated namespace', database='[4/11] Starting PostgreSQL and preparing seed data',
    services='[5/11] Deploying supporting services', application='[6/11] Deploying application and waiting for readiness',
    suite='[7/11] Running the workload', collect='[8/11] Collecting diagnostics', cleanup='[9/11] Removing run resources',
    metrics='[10/11] Exporting resource metrics', report='[11/11] Writing the result and report')


def classify(*, primary, result, job_state, collection, cleanup):
    """Return (test outcome, final status). The primary test outcome is never overwritten.

    ``primary`` is None, 'error' (a lifecycle phase failed) or 'cancelled'.
    A cleanup or collection problem downgrades only an otherwise passing run.
    """
    if primary == 'cancelled':
        test = 'cancelled'
    elif primary == 'error':
        test = 'error'
    elif (not isinstance(result, dict) or result.get('executionOutcome') != 'completed'
          or job_state not in ('complete', 'failed')):
        test = 'error'
    elif result.get('performanceOutcome') == 'passed' and result.get('outcome') == 'passed' and job_state == 'complete':
        test = 'passed'
    elif result.get('performanceOutcome') == 'failed':
        test = 'failed'
    else:
        test = 'error'
    if test == 'passed' and (collection != 'ok' or cleanup != 'ok'):
        return test, 'passed-with-errors'
    return test, test


def parse_suite_result(log, run_id):
    """The runtime prints exactly one final result document per Job."""
    found = []
    for line in log.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get('schemaVersion') == 'v1alpha1' and value.get('runId') == run_id:
            found.append(value)
    return found[0] if len(found) == 1 else None


def make_run_id(name):
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:24].strip('-') or 'run'
    return f'lt-{slug}-{secrets.token_hex(4)}'


class LoadTestRun:
    def __init__(self, definition, *, target, runs_dir, platform=None, registry=None, run_id=None, quiet=False,
                 progress_interval=5):
        self.definition, self.target, self.platform, self.registry = definition, target, platform, registry
        self.run_id = run_id or make_run_id(definition.name)
        if not re.fullmatch(r'[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?', self.run_id):
            raise ValueError('Run ID must be a DNS label')
        self.directory = Path(runs_dir).resolve() / self.run_id
        self.run = RunContext(self.run_id, self.run_id, self.directory)
        self.cluster = Kubernetes(target, self.run)
        self.redactions = []
        self.progress = Progress(self.directory, self.run_id, redact=self.redact, quiet=quiet, interval=progress_interval)
        self.namespace_created = False
        self.suite_result = None
        self.job_state = None
        self.usage = None
        self.suite_done = None
        self.window_start = None
        self.record = {'apiVersion': 'perf-runner/v1alpha1', 'kind': 'LoadTestRun', 'runId': self.run_id,
            'namespace': self.run_id, 'definition': definition.name, 'state': 'created', 'pid': os.getpid(),
            'host': socket.gethostname(), 'target': {'context': target.context,
                                                    'kubeconfig': str(target.kubeconfig.resolve())},
            'phases': [], 'errors': [], 'outcome': {}}

    # -- retention -------------------------------------------------------------------------------
    def redact(self, text):
        for secret in self.redactions:
            text = text.replace(secret, '[REDACTED]')
        return text

    def save(self):
        _save(self.directory / 'run.json', self.record)

    @contextmanager
    def phase(self, name):
        entry = {'name': name, 'startedAt': _now(), 'outcome': 'running'}
        self.record['phases'].append(entry)
        self.record['phase'] = name
        self.save()
        started = time.monotonic()
        try:
            with self.progress.operation(name, PHASE_LABELS.get(name, name)):
                yield
        except BaseException as error:
            entry['outcome'] = 'failed' if isinstance(error, Exception) else 'cancelled'
            entry['error'] = {'type': type(error).__name__, 'message': self.redact(str(error))[:2000]}
            raise
        else:
            entry['outcome'] = 'succeeded'
        finally:
            entry['finishedAt'] = _now()
            entry['elapsedSeconds'] = round(time.monotonic() - started, 3)
            try:
                self.save()
            except OSError:
                pass

    def redact_retained_files(self):
        if not self.redactions:
            return
        for path in self.directory.rglob('*'):
            if path.is_file() and path.suffix in TEXT_SUFFIXES and path.stat().st_size < 20 * 1024 * 1024:
                text = path.read_text(errors='replace')
                clean = self.redact(text)
                if clean != text:
                    _write_private(path, clean)

    # -- lifecycle -------------------------------------------------------------------------------
    def execute(self):
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=False)
        lock = os.open(self.directory / 'run.lock', os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # held until exit: a dead holder means interrupted
        try:
            with self.progress.activate():
                return self._execute()
        finally:
            os.close(lock)

    def _execute(self):
        primary = None
        self.progress.emit(f'Run {self.run_id} | definition {self.definition.name} | context {self.target.context}', event='run-start')
        self.progress.emit(f'Namespace: {self.run_id}; evidence: {self.directory}')
        self.record['logs'] = {'progress': 'progress.log', 'events': 'events.jsonl'}
        self.record.update(state='running', startedAt=_now())
        self.save()
        try:
            self.provision_and_run()
        except Exception as error:
            primary = 'error'
            self.record['errors'].append({'phase': self.record.get('phase'), 'type': type(error).__name__,
                                          'message': self.redact(str(error))[:2000]})
        except BaseException as error:  # SIGINT/SIGTERM through cancellation_signals
            primary = 'cancelled'
            self.record['errors'].append({'phase': self.record.get('phase'), 'type': type(error).__name__,
                                          'message': self.redact(str(error))[:2000]})
        with self.progress.operation('collect', PHASE_LABELS['collect']):
            collection = self.collect()
            info(f'Diagnostics collection: {collection}', level='WARN' if collection != 'ok' else 'INFO')
        with self.progress.operation('cleanup', PHASE_LABELS['cleanup']):
            cleanup = self.cleanup()
            info(f'Namespace cleanup: {cleanup}', level='WARN' if cleanup == 'failed' else 'INFO')
        with self.progress.operation('metrics', PHASE_LABELS['metrics']):
            self.export_metrics()
            info('Resource metrics available' if self.usage and self.usage.get('available') else
                 'Resource metrics unavailable or incomplete; see report for limitations')
        test, status = classify(primary=primary, result=self.suite_result, job_state=self.job_state,
                                collection=collection, cleanup=cleanup)
        result = self.suite_result or {}
        self.record['outcome'] = {'test': test, 'execution': result.get('executionOutcome', 'not-run'),
            'performance': result.get('performanceOutcome', 'not-run'),
            'collection': collection, 'cleanup': cleanup}
        self.record.update(status=status, exitCode=EXIT_CODES[status], state='finished', finishedAt=_now())
        try:
            with self.progress.operation('report', PHASE_LABELS['report']):
                self.write_report()
        except Exception as error:
            self.record['outcome']['collection'] = 'failed'
            self.record['errors'].append({'phase': 'report', 'type': type(error).__name__, 'message': 'Could not write the TestResult'})
            if status == 'passed':
                self.record.update(status='passed-with-errors', exitCode=EXIT_CODES['passed-with-errors'])
        try:
            self.redact_retained_files()
        except Exception as error:
            self.record['outcome']['collection'] = 'failed'
            self.record['errors'].append({'phase': 'retention', 'type': type(error).__name__, 'message': 'Redaction failed'})
            if status == 'passed':
                self.record.update(status='passed-with-errors', exitCode=EXIT_CODES['passed-with-errors'])
        if self.record_progress_errors():
            self.refresh_report_after_log_error()
        self.save()
        self.progress.emit(f"{self.record['status'].upper()} | execution={self.record['outcome']['execution']} | "
            f"performance={self.record['outcome']['performance']} | cleanup={cleanup} | exit={self.record['exitCode']}",
            level='INFO' if self.record['exitCode'] == 0 else 'ERROR', event='run-complete')
        if (self.directory / 'report.md').is_file():
            self.progress.emit(f'Report: {self.directory / "report.md"}; record: {self.directory / "run.json"}')
        else:
            self.progress.emit(f'No report was written; see record: {self.directory / "run.json"}', level='WARN')
        if self.record_progress_errors():
            self.refresh_report_after_log_error()
            self.save()
        return self.record

    def refresh_report_after_log_error(self):
        try:
            self.write_report()
        except Exception as error:
            self.record['errors'].append({'phase': 'report', 'type': type(error).__name__,
                                         'message': 'Could not update report after log retention failed'})

    def record_progress_errors(self):
        if not self.progress.errors:
            return False
        previous = next((e for e in self.record['errors'] if e['type'] == 'LogWriteError'), None)
        message = 'Could not write: ' + ', '.join(sorted(set(self.progress.errors)))
        if previous and previous['message'] == message:
            return False
        self.record['outcome']['collection'] = 'failed'
        if previous:
            previous['message'] = message
        else:
            self.record['errors'].append({'phase': 'progress', 'type': 'LogWriteError', 'message': message})
        if self.record['status'] == 'passed':
            self.record.update(status='passed-with-errors', exitCode=3)
        return True

    def provision_and_run(self):
        definition = self.definition
        with self.phase('validate'):
            self.cluster.command('get', 'namespaces', '--no-headers', timeout=20)  # explicit target reachable
        with self.phase('resolve'):
            platform = self.platform or load_platform()
            self.record['platform'] = {'name': platform.name, 'registry': platform.registry}
            resolved = resolve_definition(definition, self.directory / 'resolved', platform=platform,
                                          registry=self.registry or registry_for(platform))
            snapshot = resolved.snapshot
            self.record['snapshot'] = str(resolved.directory / 'resolved.json')
            self.record['artifacts'] = snapshot['artifacts']
            info(f"Resolved {len(snapshot['artifacts'])} artifact roles to immutable digests")
        with self.phase('namespace'):
            # Record intent first: even a kill between create and the next save leaves the label to find.
            self.namespace_created = True
            self.window_start = time.time()
            self.cluster.command('create', '-f', '-', data={'apiVersion': 'v1', 'kind': 'Namespace',
                'metadata': {'name': self.run_id, 'labels': {RUN_LABEL: self.run_id,
                                                              'app.kubernetes.io/managed-by': 'perf-runner'}}})
            self.cluster.assert_owned_namespace()
            # Limits and network isolation exist before any workload Pod is created.
            manifests = namespace_manifests(self.run_id)
            _save(self.directory / 'guardrails.json', {'resourcePlan': snapshot.get('resourcePlan'),
                                                       'objects': manifests})
            self.cluster.command('create', '-f', '-', data={'apiVersion': 'v1', 'kind': 'List', 'items': manifests})
            self.record['guardrails'] = {'objects': [f"{m['kind']}/{m['metadata']['name']}" for m in manifests],
                                         'file': str(self.directory / 'guardrails.json')}
            info('Guardrails applied: ' + ', '.join(self.record['guardrails']['objects']))
        timeout = definition.execution.application_readiness_timeout_seconds
        with self.phase('database'):
            database = prepare_published_database(definition, snapshot, target=self.target, run=self.run,
                                                  redactions=self.redactions)
        outputs = database_outputs(database)
        with self.phase('services'):
            for name, service_definition in snapshot['services'].items():
                service = deploy_application(service_definition, chart=resolved.chart, target=self.target,
                                             run=self.run, release=name, timeout_seconds=timeout)
                outputs.update({f'services.{name}.host': service.host, f'services.{name}.port': service.port,
                                f'services.{name}.url': service.url})
        with self.phase('application'):
            application = deploy_application(snapshot['application'], chart=resolved.chart, target=self.target,
                run=self.run, bindings=definition.application.bindings, outputs=outputs, timeout_seconds=timeout)
            info(f'Application ready at {application.url}')
        with self.phase('suite'):
            self.run_suite(snapshot, resolved, application)

    def run_suite(self, snapshot, resolved, application):
        directory = self.directory / 'suite'
        directory.mkdir(mode=0o700)
        bundle = publish_suite_scripts(resolved.suite_directory / 'suite.yaml', resolved.suite_directory,
                                       target=self.target, run=self.run)
        if bundle.sha256 != snapshot['suite']['scriptBundleSha256']:
            raise RuntimeError('Published scripts differ from the resolved snapshot')
        profile_name = 'load-profile'
        profile = profile_config_map(self.run_id, profile_name, (resolved.directory / 'profile.json').read_text())
        _save(directory / 'profile-configmap.json', profile)
        self.cluster.command('create', '-f', '-', data=profile)
        probes = snapshot['application']['spec']['values'].get('probes', {})
        manifest = suite_job_manifest(run_id=self.run_id, image=snapshot['suite']['runtimeImage'],
            script_config_map=bundle.config_map, profile_config_map_name=profile_name, base_url=application.url,
            readiness_path=probes.get('readiness', {}).get('path', '/'),
            request_timeout_seconds=snapshot['execution']['requestTimeoutSeconds'],
            deadline_seconds=snapshot['execution']['jobDeadlineSeconds'], resources=snapshot['execution']['resources'])
        _save(directory / 'job-manifest.json', manifest)
        self.record['suite'] = {'job': 'suite', 'configMap': bundle.config_map, 'scriptBundleSha256': bundle.sha256,
                                'deadlineSeconds': snapshot['execution']['jobDeadlineSeconds']}
        info(f'Starting suite Job: {self.definition.profile.duration}s schedule, '
             f"{snapshot['execution']['jobDeadlineSeconds']}s outer deadline; no retries")
        self.cluster.command('create', '-f', '-', data=manifest)
        self.save()  # a killed run now records that traffic may have started
        self.suite_done = None
        self.job_state, reason = wait_for_job(self.cluster, 'suite',
                                              deadline_seconds=snapshot['execution']['jobDeadlineSeconds'])
        self.suite_done = time.monotonic()
        self.record['suite'].update(jobState=self.job_state, jobReason=reason)
        errors = collect_job(self.cluster, 'suite', directory, redact=self.redact)
        self.record['suite']['collectionErrors'] = errors
        log_path = directory / 'suite.log'
        self.suite_result = parse_suite_result(log_path.read_text() if log_path.exists() else '', self.run_id)
        if self.suite_result is not None:
            _save(directory / 'result.json', self.suite_result)
            self.record['suite']['result'] = str(directory / 'result.json')
            info(f"Workload finished: execution={self.suite_result.get('executionOutcome')}, "
                 f"performance={self.suite_result.get('performanceOutcome')}")
            for threshold in self.suite_result.get('thresholdOutcomes', []):
                info(f"Threshold {threshold['scope']}/{threshold['metric']}: {threshold['outcome']} "
                     f"(actual={threshold.get('actual')}, max={threshold.get('max')}, samples={threshold.get('samples')})",
                     level='INFO' if threshold['outcome'] == 'passed' else 'WARN')
        if self.job_state == 'failed' and self.suite_result is None:
            raise RuntimeError(f'Suite Job failed without a result ({reason or "container error"})')
        if self.suite_result is None:
            raise RuntimeError('Suite Job completed without exactly one result document')

    def collect(self):
        """Capture namespace diagnostics before deletion. Failures are recorded, never raised."""
        if not self.namespace_created:
            return 'ok'
        directory = self.directory / 'diagnostics'
        errors = []
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
            for filename, args in [('resources.json', ('get', 'pods,deployments,services,jobs,endpointslices,'
                                                       'persistentvolumeclaims', '-o', 'json')),
                                   ('events.json', ('get', 'events', '-o', 'json'))]:
                try:
                    _write_private(directory / filename, self.redact(self.cluster.command(*args, timeout=30)))
                except BaseException as error:
                    errors.append(f'{filename}: {type(error).__name__}')
                    if not isinstance(error, Exception):
                        raise
            try:
                pods = json.loads((directory / 'resources.json').read_text())['items']
            except Exception:
                pods = []
            for pod in (item for item in pods if item['kind'] == 'Pod'):
                spec = pod['spec']
                for container in [c['name'] for c in spec.get('initContainers', [])] + [c['name'] for c in spec['containers']]:
                    try:
                        _write_private(directory / f"{pod['metadata']['name']}-{container}.log", self.redact(
                            self.cluster.command('logs', pod['metadata']['name'], '-c', container, '--tail=500', timeout=30)))
                    except Exception as error:
                        errors.append(f"{pod['metadata']['name']}/{container}: {type(error).__name__}")
        except BaseException as error:
            errors.append(type(error).__name__)
            self.record['errors'].append({'phase': 'collect', 'type': type(error).__name__, 'message': 'Collection interrupted'})
        if errors:
            self.record['collectionErrors'] = errors[:50]
        return 'failed' if errors else 'ok'

    def export_metrics(self):
        """Resource usage for the run window. Unavailable metrics are stated, not fatal."""
        if not self.namespace_created or self.window_start is None:
            return
        snapshot_services = self.definition.services
        try:
            if self.suite_done is not None:  # let one scrape land after the last traffic
                wait = CADVISOR_REFRESH_SECONDS + 1
                time.sleep(max(0, min(wait, self.suite_done + wait - time.monotonic())))
            self.usage, series = export_resource_usage(self.cluster, self.run_id, self.window_start,
                                                       time.time(), snapshot_services)
            directory = self.directory / 'metrics'
            directory.mkdir(mode=0o700, exist_ok=True)
            _save(directory / 'resource-usage.json', self.usage)
            if series:
                _save(directory / 'series.json', series)
                origin = None
                if self.suite_result and self.suite_result.get('stages'):
                    origin = datetime.fromisoformat(self.suite_result['stages'][0]['startedAt']).timestamp()
                _write_private(directory / 'resource-usage.csv', series_csv(series, snapshot_services, origin))
        except BaseException as error:
            self.usage = {'available': False, 'reason': f'Export failed ({type(error).__name__})'}
            if not isinstance(error, Exception):
                raise

    def write_report(self):
        """Durable TestResult + readable report. Needs only retained files, never the cluster."""
        result = build_test_result(self.record, self.suite_result, self.usage, self.directory)
        _save(self.directory / 'testresult.json', result)
        self.record['reports'] = {'testResult': 'testresult.json', 'report': 'report.md'}
        if result['stages']:
            try:
                from perf_runner.dashboard import render as render_dashboard
                render_dashboard(self.directory)
                result['evidence']['dashboard'] = 'dashboard.png'
                self.record['reports']['dashboard'] = 'dashboard.png'
            except ImportError:
                result['limitations'].append("Dashboard not drawn: matplotlib is not installed "
                                             "(pip install -e './platform[charts]').")
            except Exception as error:  # a chart problem must never change the test outcome
                result['limitations'].append(f'Dashboard could not be drawn ({type(error).__name__}).')
            _save(self.directory / 'testresult.json', result)
        _write_private(self.directory / 'report.md', render_report(result))

    def cleanup(self):
        """Delete only this run's namespace, then verify it is gone."""
        if not self.namespace_created:
            return 'skipped'
        try:
            try:
                self.cluster.delete_owned_namespace(self.definition.cleanup_timeout_seconds)
            except RuntimeError as error:
                if 'NotFound' not in str(error):
                    raise
            if self.cluster.command('get', 'namespace', self.run_id, '--ignore-not-found', '-o', 'name').strip():
                raise RuntimeError('Namespace still exists after deletion')
            return 'ok'
        except BaseException as error:
            self.record['errors'].append({'phase': 'cleanup', 'type': type(error).__name__,
                                          'message': self.redact(str(error))[:2000]})
            self.record['cleanupHint'] = f'./loadtest cleanup --run-id {self.run_id}'
            return 'failed'


# -- inspection and explicit abandoned-run cleanup ----------------------------------------------
def read_run(runs_dir, run_id):
    return json.loads((Path(runs_dir) / run_id / 'run.json').read_text())


def run_activity(runs_dir, run_id):
    """'finished', 'active', or 'interrupted' (record says running but nobody holds its lock)."""
    record = read_run(runs_dir, run_id)
    if record.get('state') == 'finished':
        return 'finished'
    lock_path = Path(runs_dir) / run_id / 'run.lock'
    if not lock_path.exists():
        return 'interrupted'
    descriptor = os.open(lock_path, os.O_RDWR)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 'active'
        return 'interrupted'
    finally:
        os.close(descriptor)


def list_runs(runs_dir):
    runs = []
    for path in sorted(Path(runs_dir).glob('lt-*/run.json')):
        try:
            record = json.loads(path.read_text())
            runs.append({'runId': record['runId'], 'activity': run_activity(runs_dir, record['runId']),
                         'status': record.get('status'), 'phase': record.get('phase'),
                         'namespace': record['namespace'], 'startedAt': record.get('startedAt')})
        except (OSError, ValueError, KeyError):
            continue
    return runs


def cleanup_abandoned(runs_dir, run_id, *, timeout_seconds=120):
    """Explicit cleanup. Refuses an active run; deletes only a namespace labelled for this run."""
    if not re.fullmatch(r'lt-[a-z0-9-]{1,60}', run_id):
        raise ValueError('Invalid run ID')
    record = read_run(runs_dir, run_id)
    if run_activity(runs_dir, run_id) == 'active':
        raise ValueError('Run is still active; stop it instead of cleaning it up')
    target = KubernetesTarget(Path(record['target']['kubeconfig']), record['target']['context'])
    cluster = Kubernetes(target, RunContext(run_id, record['namespace'], Path(runs_dir) / run_id))
    existing = cluster.command('get', 'namespace', record['namespace'], '--ignore-not-found', '-o', 'name').strip()
    if existing:
        cluster.delete_owned_namespace(timeout_seconds)  # ownership label is rechecked here
    record.setdefault('abandonedCleanup', []).append({'at': _now(), 'namespaceFound': bool(existing)})
    if record.get('state') != 'finished':
        record.update(state='finished', status=record.get('status') or 'error', finishedAt=_now(),
                      interrupted=True)
    _save(Path(runs_dir) / run_id / 'run.json', record)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run', help='Execute one complete LoadTestRun')
    run.add_argument('--definition', type=Path, required=True)
    run.add_argument('--kubeconfig', type=Path, help='Default: the platform config cluster.kubeconfig')
    run.add_argument('--context', help='Default: the platform config cluster.context')
    run.add_argument('--platform', type=Path,
                     help='Platform config owned by DevOps (default: PERF_PLATFORM or platform/config/local.yaml)')
    run.add_argument('--run-id')
    run.add_argument('--quiet', action='store_true', help='Suppress live progress; retain progress.log and events.jsonl')
    run.add_argument('--progress-interval', type=float, default=5, help='Seconds between still-working updates (0.1–60)')
    status = commands.add_parser('status', help='List runs and report interrupted ones')
    cleanup = commands.add_parser('cleanup', help='Delete the owned namespace of an abandoned run')
    cleanup.add_argument('--run-id', required=True)
    for sub in (run, status, cleanup):
        sub.add_argument('--runs-dir', type=Path, default=Path('.runtime/runs'))
    options = parser.parse_args(argv)
    try:
        if options.command == 'status':
            print(json.dumps(list_runs(options.runs_dir), indent=2))
            return 0
        if options.command == 'cleanup':
            record = cleanup_abandoned(options.runs_dir, options.run_id)
            print(json.dumps({'runId': options.run_id, 'abandonedCleanup': record['abandonedCleanup'][-1]}))
            return 0
        interrupted = [item['runId'] for item in list_runs(options.runs_dir) if item['activity'] == 'interrupted']
        if interrupted:
            print('Interrupted runs found (not resumed): ' + ', '.join(interrupted) +
                  '; clean with: ./loadtest cleanup --run-id <id>', file=sys.stderr)
        if not options.quiet:
            print(f'Validating definition: {options.definition}', file=sys.stderr, flush=True)
        platform = load_platform(options.platform)
        kubeconfig = options.kubeconfig or (Path(platform.kubeconfig) if platform.kubeconfig else None)
        context = options.context or platform.context
        if not kubeconfig or not context:
            raise ValueError('No cluster selected: set cluster.kubeconfig/context in the platform config '
                             'or pass --kubeconfig and --context')
        loaded = LoadTestRun(load_definition(options.definition),
            target=KubernetesTarget(kubeconfig, context), runs_dir=options.runs_dir,
            platform=platform, run_id=options.run_id,
            quiet=options.quiet, progress_interval=options.progress_interval)
        with cancellation_signals():
            record = loaded.execute()
        print(json.dumps({key: record.get(key) for key in ('runId', 'status', 'outcome', 'exitCode', 'errors')}, indent=2))
        print(f'Record: {loaded.directory / "run.json"}', file=sys.stderr)
        return record['exitCode']
    except (ValueError, OSError, RuntimeError, KeyError) as error:
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
