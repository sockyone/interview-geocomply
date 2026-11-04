"""Executable engine assessment; local synthetic HTTP target, no Checkout dependency."""
from gevent import monkey
monkey.patch_all()

import argparse
import json
from pathlib import Path
import time

import gevent
from gevent.pywsgi import WSGIServer
from flask import Flask
from locust import HttpUser, LoadTestShape, constant, task
from locust.env import Environment
from locust.event import Events

parser = argparse.ArgumentParser()
parser.add_argument('--fail', action='store_true')
parser.add_argument('--result', type=Path, required=True)
args = parser.parse_args()
app = Flask(__name__)
active = peak = 0


@app.get('/work')
def work():
    global active, peak
    active += 1
    peak = max(peak, active)
    try:
        gevent.sleep(.03)
        return ({'ok': not args.fail}, 503 if args.fail else 200)
    finally:
        active -= 1


server = WSGIServer(('127.0.0.1', 0), app, log=None)
server.start()


def exercise(client):
    client.get('/work', timeout=(1, 1), name='/work')


class SpikeUser(HttpUser):
    wait_time = constant(.01)

    @task
    def call(self):
        exercise(self.client)


stages = []


class Shape(LoadTestShape):
    def tick(self):
        elapsed = self.get_run_time()
        target = 2 if elapsed < 1 else 4 if elapsed < 3 else None
        if target is not None and (not stages or stages[-1] != target):
            stages.append(target)
        return None if target is None else (target, 100)


env = Environment(user_classes=[SpikeUser], host=f'http://127.0.0.1:{server.server_port}',
                  events=Events(), shape_class=Shape(), stop_timeout=1)
runner = env.create_local_runner()
env.shape_class.runner = runner
try:
    runner.start_shape()
    deadline = time.monotonic() + 8
    while runner.state != 'stopped':
        if time.monotonic() >= deadline:
            raise TimeoutError('Engine spike did not stop')
        gevent.sleep(.05)
    entry = env.stats.get('/work', 'GET')
    result = {'engine': 'locust', 'version': '2.46.6', 'loadModel': 'closed-users',
              'stageTargets': stages, 'peakConcurrentRequests': peak,
              'requests': entry.num_requests, 'failures': entry.num_failures,
              'p95Ms': entry.get_response_time_percentile(.95)}
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(result, indent=2) + '\n')
    if stages != [2, 4] or peak < 2 or entry.num_requests == 0:
        raise RuntimeError('Spike did not demonstrate stages/concurrency/metrics')
finally:
    runner.quit()
    server.stop()
raise SystemExit(1 if result['failures'] else 0)
