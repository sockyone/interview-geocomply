"""Finite batches and timed profiles; Locust owns HTTP metrics and user concurrency."""
from dataclasses import dataclass, replace
from copy import deepcopy
import json
import inspect
import math
from pathlib import Path
import re
import signal
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import gevent
from gevent.local import local
from gevent.event import Event
from gevent.queue import Queue, Empty
from locust import HttpUser, task
from locust import log as locust_log
from locust.env import Environment
from locust.event import Events
from locust.exception import StopUser

from .profile import LoadProfile, parse_profile
from .profile_metrics import ProfileMetrics
from .client import JourneyClient
from .sdk import require
from .results import create_result, now, save_result, timing


@dataclass(frozen=True)
class Options:
    base_url: str
    run_id: str
    journeys: tuple[str, ...]
    result_path: Path
    iterations: int = 1
    users: int = 1
    request_timeout: float = 3
    execution_timeout: float = 60
    drain_timeout: float = 2
    profile: LoadProfile | None = None


def validate(options, suite):
    url = urlsplit(options.base_url)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('Base URL must be HTTP(S) without embedded credentials/query/fragment')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', options.run_id):
        raise ValueError('Invalid run ID')
    if not options.journeys or len(set(options.journeys)) != len(options.journeys) or set(options.journeys) - suite.journeys.keys():
        raise ValueError('Select unique journeys declared in this TestSuite')
    if type(options.users) is not int or not 1 <= options.users <= 256:
        raise ValueError('users must be 1–256 concurrent users')
    if type(options.iterations) is not int or not 1 <= options.iterations <= 10000:
        raise ValueError('iterations must be 1–10000 per selected journey')
    if len(options.journeys) * options.iterations > 20000:
        raise ValueError('At most 20000 journey attempts are supported per smoke batch')
    for name, limit in [('request_timeout', 60), ('execution_timeout', 3700 if options.profile else 3600), ('drain_timeout', 30)]:
        value = getattr(options, name)
        if isinstance(value, bool) or not math.isfinite(value) or not 0 < value <= limit:
            raise ValueError(f'{name} must be positive and at most {limit} seconds')


def run_suite(options, suite, *, bearer_token=None, progress=None):
    if options.profile:
        # Validate API callers too; do not trust manually constructed dataclasses.
        profile = parse_profile(options.profile.document(), suite.journeys)
        options = replace(options, profile=profile, journeys=profile.journeys,
                          execution_timeout=profile.execution_timeout, drain_timeout=profile.drain_timeout)
    validate(options, suite)
    record = create_result(options.result_path, options.run_id, options.journeys, options.iterations, suite_name=suite.name)
    record['scriptBundleSha256'] = suite.digest
    import hashlib
    record['fixtureSha256'] = hashlib.sha256(json.dumps(suite.fixture, sort_keys=True).encode()).hexdigest()
    record['journeyFunctions'] = suite.references
    profile = options.profile
    metrics = ProfileMetrics(profile) if profile else None
    attempt_scope = local()
    accepting = True
    stage_index = None
    profile_started = None
    if profile:
        record['loadProfile'] = profile.name
        record['resolvedProfile'] = profile.document()
        record['jobDeadlineSeconds'] = profile.job_deadline
        record['executionOutcome'] = record['performanceOutcome'] = 'incomplete'
        record['stages'] = [dict(name=s.name, transition=s.transition, durationSeconds=s.duration,
            users=s.users, measure=s.measure, startedAt=None, finishedAt=None, targets=[], reachedTarget=False,
            **metrics.summary(s.name))
            for s in profile.stages]
        for counts in record['journeys'].values():
            counts['scheduled'] = None
    queue = Queue()
    if not profile:
        for _ in range(options.iterations):
            for name in options.journeys:
                queue.put_nowait(name)
    scheduled = queue.qsize()
    done, cancel = Event(), Event()
    cancellation = None
    completed = 0
    seen_ids = set()
    environment = runner = None
    previous_signals = {}

    def emit(event, elapsed=0, target=0):
        if not progress:
            return
        stage = profile.stages[stage_index] if profile and stage_index is not None else None
        try:
            progress(dict(event=event, runId=options.run_id, elapsedSeconds=round(elapsed, 2),
                durationSeconds=profile.duration if profile else options.execution_timeout,
                stage=stage.name if stage else '', measure=stage.measure if stage else False,
                targetUsers=target, actualUsers=runner.user_count if runner else 0,
                completedJourneys=completed, failedJourneys=sum(c['failed'] for c in record['journeys'].values()),
                outcome=record['outcome']))
        except Exception:
            pass  # Observability cannot change the traffic/result contract.

    def message(error):
        text = str(error)[:500] if isinstance(error, (AssertionError, TimeoutError)) else f'{type(error).__name__} during suite execution'
        return text.replace(bearer_token, '[REDACTED]') if bearer_token else text

    def record_id(value, *, namespace='default'):
        require(isinstance(value, str) and bool(value), 'Expected a nonempty ID')
        key = (namespace, value)
        require(key not in seen_ids, 'Different journey attempts received the same ID')
        require(len(seen_ids) < 100000, 'ID tracking resource limit exceeded')
        seen_ids.add(key)
        if len(record['dataSamples']) < 100:
            record['dataSamples'].append({'namespace': namespace, 'id': value})

    def request_failure(journey, error):
        if len(record['failures']) < 100:
            record['failures'].append({'journey': journey, 'type': type(error).__name__, 'message': message(error)})
        else:
            record['failuresTruncated'] += 1

    sequence = profile.sequence() if profile else ()

    class BatchUser(HttpUser):
        # Pauses from the profile flow happen between tasks, where Locust can stop a user at once.
        def wait_time(self):
            return self.pause

        def on_start(self):
            self.client.trust_env = False
            self.position, self.pause = 0, 0.0

        @task
        def execute(self):
            nonlocal completed
            self.pause = 0.0
            if profile:
                position = profile.target(time.monotonic() - profile_started)
                if not accepting or position is None or cancel.is_set():
                    raise StopUser()
                if sum(c['attempted'] for c in record['journeys'].values()) >= 100000:
                    raise RuntimeError('Profile exceeded 100000-attempt resource limit')
                # Every user walks the same ordered flow from its start, then repeats it.
                name, self.pause = sequence[self.position]
                self.position = (self.position + 1) % len(sequence)
                attempt_scope.stage = position[0]
            else:
                try:
                    name = queue.get_nowait()
                except Empty:
                    raise StopUser()
            counts = record['journeys'][name]
            counts['attempted'] += 1
            started = time.perf_counter()
            failure = None
            try:
                attempt_data = deepcopy(suite.fixture)
                context = SimpleNamespace(client=JourneyClient(self.client, run_id=options.run_id,
                    timeout=options.request_timeout, bearer_token=bearer_token),
                    run_id=options.run_id, fixture=attempt_data, data=attempt_data, record_id=record_id, check=require)
                returned = suite.journeys[name](context)
                require(not inspect.isawaitable(returned) and not inspect.isgenerator(returned), 'Journey must execute synchronously')
            except Exception as error:
                failure = error
                counts['failed'] += 1
                request_failure(name, error)
            except BaseException:
                failure = RuntimeError('Journey interrupted before completion')
                counts['interrupted'] += 1
                raise
            else:
                counts['passed'] += 1
            finally:
                environment.events.request.fire(request_type='JOURNEY', name=name,
                    response_time=(time.perf_counter() - started) * 1000,
                    response_length=0, exception=failure, context={})
                completed += 1
                if not profile and completed == scheduled:
                    done.set()

    def on_request(request_type, response_time, exception, **kwargs):
        if metrics:
            metrics.sample(getattr(attempt_scope, 'stage', None), request_type, response_time, exception is not None)

    def on_user_error(user_instance, exception, tb, **kwargs):
        record['executionErrors'].append({'type': type(exception).__name__, 'message': message(exception)})
        cancel.set()

    def on_cpu_warning(environment, cpu_usage, **kwargs):
        if len(record['generatorWarnings']) < 100:
            record['generatorWarnings'].append({'type': 'cpu-saturation', 'cpuPercent': cpu_usage})

    def interrupt(signum, frame):
        nonlocal cancellation
        cancellation = signum
        cancel.set()

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_signals[sig] = signal.signal(sig, interrupt)
        environment = Environment(user_classes=[BatchUser], host=options.base_url.rstrip('/'),
                                  events=Events(), stop_timeout=profile.drain_timeout if profile else options.drain_timeout)
        environment.events.user_error.add_listener(on_user_error)
        environment.events.cpu_warning.add_listener(on_cpu_warning)
        environment.events.request.add_listener(on_request)
        runner = environment.create_local_runner()
        profile_started = time.monotonic()
        if not profile:
            runner.start(user_count=min(options.users, scheduled), spawn_rate=min(options.users, scheduled))
        record['engineReady'] = True
        save_result(options.result_path, record)
        deadline = time.monotonic() + options.execution_timeout
        next_tick, last_target = 0, None
        while not cancel.is_set():
            elapsed = time.monotonic() - profile_started
            if time.monotonic() >= deadline:
                raise TimeoutError('Suite execution deadline exceeded')
            if profile:
                if elapsed - next_tick > 1:
                    raise RuntimeError('Generator missed a schedule tick')
                if elapsed >= profile.duration:
                    if stage_index is not None:
                        record['stages'][stage_index]['finishedAt'] = now()
                    break
                if elapsed >= next_tick:
                    index, target = profile.target(elapsed)
                    stage_changed = index != stage_index
                    if stage_changed:
                        if stage_index is not None:
                            record['stages'][stage_index]['finishedAt'] = now()
                            if not record['stages'][stage_index]['reachedTarget']:
                                raise RuntimeError('Stage did not achieve its user target')
                        stage_index = index
                        record['stages'][index]['startedAt'] = now()
                    stage_record = record['stages'][index]
                    stage_record['targets'].append({'elapsedSeconds': elapsed, 'target': target,
                                                   'achievedBeforeUpdate': runner.user_count})
                    if target == profile.stages[index].users and runner.user_count == target:
                        stage_record['reachedTarget'] = True
                    if last_target != target:
                        if runner.spawning_greenlet and not runner.spawning_greenlet.ready():
                            raise RuntimeError('Previous spawn/stop transition has not finished')
                        runner.start(user_count=target, spawn_rate=profile.spawn_rate)
                        last_target = target
                    if stage_changed or last_target != target or int(next_tick) % 5 == 0:
                        emit('stage' if stage_changed else 'tick', elapsed, target)
                    next_tick += 1
            elif done.is_set():
                break
            gevent.sleep(.05)
        accepting = False
        emit('drain', time.monotonic() - profile_started)
        if cancel.is_set():
            record['state'] = 'interrupted' if cancellation else 'error'
            if cancellation:
                record['executionErrors'].append({'type': 'Cancellation', 'signal': cancellation})
        else:
            record['state'] = 'completed'
    except BaseException as error:
        record['state'] = 'interrupted' if isinstance(error, KeyboardInterrupt) else 'error'
        record['executionErrors'].append({'type': type(error).__name__, 'message': message(error)})
    finally:
        accepting = False
        if runner is not None:
            try:
                runner.quit()
            except BaseException as error:
                record['state'] = 'error'
                record['executionErrors'].append({'type': type(error).__name__, 'message': message(error)})
        for sig, handler in previous_signals.items():
            signal.signal(sig, handler)
        if cancellation:
            record['state'] = 'interrupted'
            if not any(e['type'] == 'Cancellation' for e in record['executionErrors']):
                record['executionErrors'].append({'type': 'Cancellation', 'signal': cancellation})
        if environment is not None:
            for name, counts in record['journeys'].items():
                counts['timingMs'] = timing(environment.stats.entries.get((name, 'JOURNEY')))
            record['http'] = [{'method': method, 'name': name, 'requests': entry.num_requests,
                              'failures': entry.num_failures, 'timingMs': timing(entry)}
                             for (name, method), entry in sorted(environment.stats.entries.items())
                             if method != 'JOURNEY']
        if locust_log.unhandled_greenlet_exception:
            record['executionErrors'].append({'type': 'UnhandledLocustError', 'message': 'Engine reported an unhandled exception'})
            record['state'] = 'error'
        if profile:
            for stage in record['stages']:
                stage.update(metrics.summary(stage['name']))
            record['measured'] = metrics.summary('measured')
            complete = (record['state'] == 'completed' and not record['executionErrors']
                        and all(s['finishedAt'] and s['reachedTarget'] for s in record['stages'])
                        and all(c['attempted'] == c['passed'] + c['failed'] and c['interrupted'] == 0
                                for c in record['journeys'].values()))
            if record['state'] == 'completed' and not complete:
                record['executionErrors'].append({'type': 'IncompleteSchedule', 'message': 'Schedule or drain incomplete'})
                record['state'] = 'error'
            record['executionOutcome'] = 'completed' if complete else 'failed'
            record['thresholdOutcomes'] = metrics.evaluate(complete)
            measured_samples = all(s['journey']['samples'] > 0 for s in record['stages'] if s['measure'])
            accepted = measured_samples and all(t['outcome'] == 'passed' for t in record['thresholdOutcomes'])
        else:
            complete = all(c['passed'] + c['failed'] == c['scheduled'] and c['interrupted'] == 0
                           for c in record['journeys'].values())
            accepted = True
        passing = (record['state'] == 'completed' and complete and accepted and not record['executionErrors']
                   and all(c['failed'] == 0 for c in record['journeys'].values())
                   and all(entry['failures'] == 0 for entry in record['http']))
        if profile:
            record['performanceOutcome'] = 'incomplete' if not complete else 'passed' if passing else 'failed'
        record['outcome'] = 'passed' if passing else 'failed'
        record['finishedAt'] = now()
        save_result(options.result_path, record)
        emit('finished', time.monotonic() - profile_started if profile_started else 0)
    return (0 if passing else 130 if cancellation else 1), record
