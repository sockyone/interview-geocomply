"""Dependency-light LoadProfile contract shared by host preflight and the image."""
from dataclasses import dataclass
import math
from pathlib import Path
import re

import yaml


def fields(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ValueError(f'Expected fields {required}; optional {optional}')


def number(value, low, high, label, *, integer=False):
    if (type(value) not in (int, float) or not math.isfinite(value) or
            not low <= value <= high or (integer and type(value) is not int)):
        raise ValueError(f'{label} must be {low}–{high}' + (' (integer)' if integer else ''))
    return value


def name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', value):
        raise ValueError('Names must be lowercase DNS labels')
    return value


@dataclass(frozen=True)
class LoadStage:
    name: str
    transition: str
    duration: int
    users: int
    measure: bool


@dataclass(frozen=True)
class Threshold:
    scope: str
    metric: str
    maximum: float


@dataclass(frozen=True)
class FlowStep:
    """Either run ``journey`` ``repeat`` times in a row, or pause for ``wait`` seconds."""
    journey: str | None = None
    repeat: int = 1
    wait: float = 0

    def document(self):
        if self.journey is None:
            return {'waitSeconds': self.wait}
        return {'journey': self.journey} | ({'repeat': self.repeat} if self.repeat != 1 else {})


@dataclass(frozen=True)
class LoadProfile:
    name: str
    flow: tuple[FlowStep, ...]
    stages: tuple[LoadStage, ...]
    spawn_rate: float
    drain_timeout: float
    thresholds: tuple[Threshold, ...]

    def document(self):
        return {'apiVersion': 'perf-runner/v1alpha1', 'kind': 'LoadProfile',
            'metadata': {'name': self.name}, 'spec': {'model': 'closed-users',
            'flow': [step.document() for step in self.flow], 'spawnRate': self.spawn_rate,
            'drainTimeoutSeconds': self.drain_timeout,
            'stages': [dict(name=s.name, transition=s.transition, durationSeconds=s.duration,
                            users=s.users, measure=s.measure) for s in self.stages],
            'thresholds': [dict(scope=t.scope, metric=t.metric, max=t.maximum) for t in self.thresholds]}}

    @property
    def journeys(self):
        """Distinct journey names in first-use order."""
        return tuple(dict.fromkeys(step.journey for step in self.flow if step.journey))

    def sequence(self):
        """Each user's repeating cycle as (journey, pause after it). A leading pause wraps to the end."""
        cycle, leading = [], 0.0
        for step in self.flow:
            if step.journey is None:
                if cycle:
                    cycle[-1][1] += step.wait
                else:
                    leading += step.wait
            else:
                cycle.extend([step.journey, 0.0] for _ in range(step.repeat))
        cycle[-1][1] += leading
        return tuple((journey, wait) for journey, wait in cycle)

    @property
    def duration(self):
        return sum(s.duration for s in self.stages)

    @property
    def execution_timeout(self):
        return self.duration + self.drain_timeout + 10

    @property
    def job_deadline(self):
        return math.ceil(self.execution_timeout + 60)

    def target(self, elapsed):
        """One-second, integer closed-user targets; intervals are [start, end)."""
        start, previous = 0, 0
        for index, stage in enumerate(self.stages):
            if elapsed < start + stage.duration:
                fraction = min(1, (int(elapsed - start) + 1) / (stage.duration - 1))
                users = stage.users if stage.transition == 'hold' else round(previous + (stage.users - previous) * fraction)
                return index, users
            start += stage.duration
            previous = stage.users
        return None


def parse_flow(raw, journeys):
    """Ordered steps every user repeats: {journey, repeat?} or {waitSeconds}."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= 50:
        raise ValueError('flow must be a list of 1–50 steps')
    steps = []
    for item in raw:
        if isinstance(item, dict) and 'waitSeconds' in item:
            fields(item, ('waitSeconds',))
            steps.append(FlowStep(wait=number(item['waitSeconds'], .1, 600, 'waitSeconds')))
            continue
        fields(item, ('journey',), ('repeat',))
        if item['journey'] not in set(journeys):
            raise ValueError(f"Unknown journey in flow: {item['journey']!r}")
        steps.append(FlowStep(item['journey'], number(item.get('repeat', 1), 1, 100, 'repeat', integer=True)))
    if not any(step.journey for step in steps):
        raise ValueError('flow needs at least one journey step')
    if sum(step.repeat for step in steps if step.journey) > 1000:
        raise ValueError('flow expands to more than 1000 journey runs per cycle')
    return tuple(steps)


def flow_journeys(spec):
    """Journey names a raw profile spec refers to (for checks made before the TestSuite is known)."""
    flow = spec.get('flow') if isinstance(spec, dict) else None
    return {item['journey'] for item in flow if isinstance(item, dict) and isinstance(item.get('journey'), str)} \
        if isinstance(flow, list) else set()


def parse_profile(document, journeys):
    fields(document, ('apiVersion', 'kind', 'metadata', 'spec'))
    if document['apiVersion'] != 'perf-runner/v1alpha1' or document['kind'] != 'LoadProfile':
        raise ValueError('Expected perf-runner/v1alpha1 LoadProfile')
    fields(document['metadata'], ('name',))
    profile_name = name(document['metadata']['name'])
    spec = document['spec']
    if isinstance(spec, dict) and 'journeys' in spec:
        raise ValueError('Journey weights were replaced by spec.flow: list the steps each user repeats')
    fields(spec, ('model', 'flow', 'spawnRate', 'drainTimeoutSeconds', 'stages', 'thresholds'))
    if spec['model'] != 'closed-users':
        raise ValueError('Only closed-users is supported')
    flow = parse_flow(spec['flow'], journeys)
    rate = number(spec['spawnRate'], 1, 100, 'spawnRate')
    drain = number(spec['drainTimeoutSeconds'], .1, 30, 'drainTimeoutSeconds')
    if not isinstance(spec['stages'], list) or not 1 <= len(spec['stages']) <= 32:
        raise ValueError('Require 1–32 stages')
    stages, previous = [], 0
    for raw in spec['stages']:
        fields(raw, ('name', 'transition', 'durationSeconds', 'users', 'measure'))
        stage = LoadStage(name(raw['name']), raw['transition'],
            number(raw['durationSeconds'], 3, 3600, 'durationSeconds', integer=True),
            number(raw['users'], 1, 256, 'users', integer=True), raw['measure'])
        if stage.transition not in ('hold', 'ramp') or type(stage.measure) is not bool:
            raise ValueError('Use hold/ramp and boolean measure')
        if stage.name == 'measured' or stage.name in {s.name for s in stages}:
            raise ValueError('Stage names must be unique and cannot be measured')
        if stage.transition == 'hold':
            if previous and stage.users != previous:
                raise ValueError('A hold must retain the previous target; insert a ramp')
            if not previous and stage.users > rate:
                raise ValueError('Initial hold must spawn within one tick; use a ramp')
        elif abs(stage.users - previous) > rate * (stage.duration - 2):
            raise ValueError('Ramp duration/spawnRate cannot reach target with tick margin')
        stages.append(stage)
        previous = stage.users
    if sum(s.duration for s in stages) > 3600 or not any(s.measure for s in stages):
        raise ValueError('Require measured stages and at most 3600 total seconds')
    raw_thresholds = spec['thresholds']
    if not isinstance(raw_thresholds, list) or len(raw_thresholds) > 100:
        raise ValueError('thresholds must be a list of at most 100 entries')
    thresholds, seen = [], set()
    for raw in raw_thresholds:
        fields(raw, ('scope', 'metric', 'max'))
        scope, metric = raw['scope'], raw['metric']
        if not isinstance(scope, str) or scope not in {'measured', *(s.name for s in stages if s.measure)}:
            raise ValueError('Threshold scope must be measured or a measured stage name')
        if metric not in ('journeyP95Ms', 'journeyFailureRatio', 'httpFailureRatio'):
            raise ValueError('Unsupported threshold metric')
        maximum = number(raw['max'], 0, 3600000 if metric == 'journeyP95Ms' else 1, 'threshold max')
        if (scope, metric) in seen:
            raise ValueError('Duplicate threshold')
        seen.add((scope, metric))
        thresholds.append(Threshold(scope, metric, maximum))
    return LoadProfile(profile_name, flow, tuple(stages), rate, drain, tuple(thresholds))


def load_profile(path, journeys):
    path = Path(path)
    if path.stat().st_size > 65536:
        raise ValueError('Profile exceeds 64 KiB')
    return parse_profile(yaml.safe_load(path.read_text()), journeys)
