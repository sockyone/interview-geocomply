"""Private atomic JSON results; a started or interrupted run is never a pass."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile


def now():
    return datetime.now(timezone.utc).isoformat()


def create_result(path, run_id, journeys, iterations, *, suite_name='unnamed'):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {'schemaVersion': 'v1alpha1', 'suite': suite_name, 'runId': run_id,
        'engine': {'name': 'locust', 'version': '2.46.6', 'loadModel': 'closed-users'},
        'state': 'running', 'outcome': 'incomplete', 'startedAt': now(), 'finishedAt': None,
        'journeys': {name: {'scheduled': iterations, 'attempted': 0, 'passed': 0,
                          'failed': 0, 'interrupted': 0, 'timingMs': None} for name in journeys},
        'http': [], 'failures': [], 'failuresTruncated': 0, 'executionErrors': [],
        'generatorWarnings': [], 'thresholdOutcomes': [], 'dataSamples': []}
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
        json.dump(record, stream, indent=2, allow_nan=False)
    return record


def save_result(path, record):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(record, stream, indent=2, allow_nan=False)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def timing(entry):
    if not entry or entry.num_requests == 0:
        return None
    return {'samples': entry.num_requests, 'min': entry.min_response_time,
            'max': entry.max_response_time, 'mean': entry.avg_response_time,
            'p50': entry.get_response_time_percentile(.50),
            'p95': entry.get_response_time_percentile(.95),
            'p99': entry.get_response_time_percentile(.99)}
