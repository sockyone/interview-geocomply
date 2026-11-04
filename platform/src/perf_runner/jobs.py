"""Native Kubernetes Job manifests, bounded waiting, and evidence collection.

Jobs never retry (backoffLimit 0), have an active deadline, run non-root with no
service-account token, and reference images only by the resolved snapshot values.
"""
import json
import math
import re
import time

from perf_runner.progress import info
from perf_runner.database import RUN_LABEL, _write_private

# Container waiting reasons that will not recover without intervention.
STUCK_WAITING = {'ErrImagePull', 'ImagePullBackOff', 'InvalidImageName', 'CreateContainerConfigError',
                 'CreateContainerError', 'ErrImageNeverPull'}

SECURITY_CONTEXT = {'allowPrivilegeEscalation': False, 'runAsNonRoot': True,
                    'capabilities': {'drop': ['ALL']}, 'seccompProfile': {'type': 'RuntimeDefault'}}
# Every container declares requests and limits; init containers only wait on a socket/URL.
INIT_RESOURCES = {'requests': {'cpu': '50m', 'memory': '64Mi'}, 'limits': {'cpu': '200m', 'memory': '128Mi'}}


def _job(name, run_id, role, deadline, pod):
    labels = {RUN_LABEL: run_id, 'perf-runner/role': role}
    return {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {'name': name, 'labels': labels},
        'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': int(deadline), 'template': {
            'metadata': {'labels': labels},
            'spec': {'restartPolicy': 'Never', 'automountServiceAccountToken': False, **pod}}}}


def seed_job_manifest(*, run_id, image, module, args, bindings, outputs, timeout_seconds):
    """Run the published setup image's Python module against the run-owned database.

    ``outputs`` maps database output names to literals or ``(secret, key)`` tuples.
    """
    environment = []
    for name, source in sorted(bindings.items()):
        value = outputs[source]
        if isinstance(value, tuple):
            environment.append({'name': name, 'valueFrom': {'secretKeyRef': {'name': value[0], 'key': value[1]}}})
        else:
            environment.append({'name': name, 'value': str(value)})
    tcp_wait = ("import os,socket,time\n"
                "deadline = time.monotonic() + 30\n"
                "while True:\n"
                "    try:\n"
                "        socket.create_connection((os.environ['DB_HOST'], int(os.environ['DB_PORT'])), 1).close()\n"
                "        break\n"
                "    except OSError:\n"
                "        if time.monotonic() >= deadline:\n"
                "            raise\n"
                "        time.sleep(.2)\n")
    return _job('database-seed', run_id, 'seed', timeout_seconds, {
        'terminationGracePeriodSeconds': 5,
        # Endpoints can be Ready before the node's Service rules converge; wait for the dataplane.
        'initContainers': [{'name': 'wait-for-database', 'image': image, 'imagePullPolicy': 'IfNotPresent',
            'command': ['python', '-c', tcp_wait],
            'env': [{'name': 'DB_HOST', 'value': str(outputs['database.host'])},
                    {'name': 'DB_PORT', 'value': str(outputs['database.port'])}],
            'securityContext': SECURITY_CONTEXT, 'resources': INIT_RESOURCES}],
        'containers': [{
        'name': 'seed', 'image': image, 'imagePullPolicy': 'IfNotPresent',
        'command': ['python', '-m', module, *args], 'env': environment,
        'securityContext': SECURITY_CONTEXT,
        'resources': {'requests': {'cpu': '50m', 'memory': '64Mi'},
                      'limits': {'cpu': '500m', 'memory': '256Mi'}}}]})


def profile_config_map(run_id, name, profile_text):
    return {'apiVersion': 'v1', 'kind': 'ConfigMap', 'immutable': True,
            'metadata': {'name': name, 'labels': {RUN_LABEL: run_id}},
            'data': {'profile.yaml': profile_text}}


def suite_job_manifest(*, run_id, image, script_config_map, profile_config_map_name, base_url,
                       readiness_path, request_timeout_seconds, deadline_seconds, resources):
    """The shared runtime image plus read-only script and profile ConfigMaps."""
    ready_check = ("import os,time,urllib.request\n"
                   "url = os.environ['READY_URL']\n"
                   "deadline = time.monotonic() + 30\n"
                   "while True:\n"
                   "    try:\n"
                   "        with urllib.request.urlopen(url, timeout=1) as response:\n"
                   "            assert response.status == 200\n"
                   "        break\n"
                   "    except OSError:\n"
                   "        if time.monotonic() >= deadline:\n"
                   "            raise\n"
                   "        time.sleep(.2)\n")
    return _job('suite', run_id, 'suite', deadline_seconds, {
        'volumes': [{'name': 'user-flows', 'configMap': {'name': script_config_map}},
                    {'name': 'profile', 'configMap': {'name': profile_config_map_name}}],
        'initContainers': [{'name': 'wait-for-application', 'image': image, 'imagePullPolicy': 'IfNotPresent',
            'command': ['python', '-c', ready_check],
            'env': [{'name': 'READY_URL', 'value': base_url + readiness_path}],
            'securityContext': SECURITY_CONTEXT, 'resources': INIT_RESOURCES}],
        'containers': [{'name': 'suite', 'image': image, 'imagePullPolicy': 'IfNotPresent',
            'args': ['--suite', '/suite-input/suite.yaml', '--profile', '/profile/profile.yaml',
                     '--result', '/results/result.json', '--print-result',
                     '--request-timeout', str(request_timeout_seconds)],
            'env': [{'name': 'BASE_URL', 'value': base_url}, {'name': 'RUN_ID', 'value': run_id}, {'name': 'PERF_RUN_PROGRESS', 'value': '1'}],
            'volumeMounts': [{'name': 'user-flows', 'mountPath': '/suite-input', 'readOnly': True},
                             {'name': 'profile', 'mountPath': '/profile', 'readOnly': True}],
            'securityContext': SECURITY_CONTEXT, 'resources': resources}]})


def _terminal(job):
    for condition in job.get('status', {}).get('conditions', []):
        if condition['status'] == 'True' and condition['type'] in ('Complete', 'Failed'):
            return condition['type'].lower(), condition.get('reason', '')
    return None


def wait_for_job(cluster, name, *, deadline_seconds, pull_grace_seconds=45, poll_seconds=.5):
    """Return ('complete'|'failed', reason). Raises TimeoutError or RuntimeError.

    The Job's own active deadline normally ends execution; this outer bound adds
    a margin for scheduling/API delay. Unrecoverable image/config errors fail fast
    after a short grace period instead of waiting for the whole deadline.
    """
    started = time.monotonic()
    outer = started + deadline_seconds + 30
    stuck_since = None
    last_status = None
    last_log_poll = -float('inf')
    seen_events = set()
    while True:
        job = json.loads(cluster.command('get', f'job/{name}', '-o', 'json'))
        done = _terminal(job)
        if done:
            info(f'Job {name}: {done[0]}' + (f' ({done[1]})' if done[1] else ''))
            if name == 'suite':
                read_suite_progress(cluster, name, seen_events)
            return done
        pods = json.loads(cluster.command('get', 'pods', '-l', f'job-name={name}', '-o', 'json'))['items']
        reasons = set()
        summaries = []
        for pod in pods:
            summaries.append(pod.get('status', {}).get('phase', 'Pending'))
            for status in (pod.get('status', {}).get('initContainerStatuses', [])
                           + pod.get('status', {}).get('containerStatuses', [])):
                waiting = status.get('state', {}).get('waiting') or {}
                if waiting.get('reason'):
                    summaries.append(status.get('name', 'container') + ': ' + waiting['reason'])
                if status.get('state', {}).get('running'):
                    summaries.append(status.get('name', 'container') + ': running')
                terminated = status.get('state', {}).get('terminated')
                if terminated:
                    summaries.append(status.get('name', 'container') + ': exit ' + str(terminated.get('exitCode')))
                if waiting.get('reason') in STUCK_WAITING:
                    reasons.add(waiting['reason'])
        summary = ', '.join(summaries) if summaries else 'waiting for Pod scheduling'
        if summary != last_status:
            info(f'Job {name}: {summary}', event='job-status', job=name)
            last_status = summary
        if name == 'suite' and pods and time.monotonic() - last_log_poll >= 3:
            read_suite_progress(cluster, name, seen_events)
            last_log_poll = time.monotonic()
        if reasons:
            stuck_since = stuck_since or time.monotonic()
            if time.monotonic() - stuck_since > pull_grace_seconds:
                raise RuntimeError(f'Job {name} cannot start: {", ".join(sorted(reasons))}')
        else:
            stuck_since = None
        if time.monotonic() >= outer:
            raise TimeoutError(f'Job {name} did not finalize within its bounded wait')
        time.sleep(poll_seconds)


def read_suite_progress(cluster, name, seen):
    """Relay only bounded structured runtime events, never arbitrary application logs."""
    from perf_runner.progress import enabled
    if not enabled():
        return
    try:
        log = cluster.command('logs', f'job/{name}', '-c', 'suite', '--tail=100', '--limit-bytes=65536', timeout=3)
        for line in log.splitlines():
            if not line.startswith('PERF_PROGRESS '):
                continue
            value = json.loads(line.removeprefix('PERF_PROGRESS '))
            if (not isinstance(value, dict) or value.get('runId') != cluster.run.run_id
                    or type(value.get('sequence')) is not int or not 0 <= value['sequence'] <= 5000
                    or value['sequence'] in seen):
                continue
            message = format_suite_progress(value)
            if message:
                seen.add(value['sequence'])
                fields = {'event', 'sequence', 'stage', 'measure', 'elapsedSeconds', 'durationSeconds',
                          'targetUsers', 'actualUsers', 'completedJourneys', 'failedJourneys', 'outcome'}
                info(message, event='workload-progress', runtimeEvent={k:v for k,v in value.items() if k in fields})
    except Exception:
        # Telemetry is best-effort. The Job/result gates remain authoritative.
        return


def format_suite_progress(value):
    if value.get('event') not in {'stage', 'tick', 'drain', 'finished'}:
        return None
    if value['event'] == 'drain':
        return 'Stopping new journeys; draining in-flight work'
    if value['event'] == 'finished':
        return f"Runner finished: {value.get('outcome')}" if value.get('outcome') in {'passed', 'failed', 'incomplete'} else None
    stage = str(value.get('stage', ''))[:63]
    elapsed, duration = value.get('elapsedSeconds'), value.get('durationSeconds')
    target, achieved = value.get('targetUsers'), value.get('actualUsers')
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', stage):
        return None
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0
           for v in (elapsed, duration, target, achieved, value.get('completedJourneys', 0), value.get('failedJourneys', 0))):
        return None
    return (f"Stage {stage}: {elapsed:.0f}/{duration:.0f}s workload | users {achieved}/{target} target | "
            f"journeys {value.get('completedJourneys', 0)} | failed {value.get('failedJourneys', 0)} | "
            f"{'measured' if value.get('measure') else 'warmup / excluded'}")


def collect_job(cluster, name, directory, *, redact=lambda text: text, tail=2000):
    """Retain Job/Pod JSON and every container log; return per-item collection errors."""
    errors = []
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def keep(filename, *args, timeout=20):
        try:
            _write_private(directory / filename, redact(cluster.command(*args, timeout=timeout)))
            return True
        except Exception as error:
            errors.append(f'{filename}: {type(error).__name__}')
            return False

    keep('job.json', 'get', f'job/{name}', '-o', 'json')
    keep('pods.json', 'get', 'pods', '-l', f'job-name={name}', '-o', 'json')
    try:
        pods = json.loads((directory / 'pods.json').read_text())['items']
    except Exception:
        pods = []
    for pod in pods:
        spec = pod['spec']
        for container in [c['name'] for c in spec.get('initContainers', [])] + [c['name'] for c in spec['containers']]:
            keep(f'{container}.log', 'logs', pod['metadata']['name'], '-c', container,
                 f'--tail={tail}', timeout=30)
    return errors
