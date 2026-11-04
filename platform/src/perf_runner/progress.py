"""Live, line-oriented lifecycle progress and private durable event logs."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import math
import os
import re
import sys
import threading
import time

_current = ContextVar('perf_progress', default=None)


def info(message, *, level='INFO', **details):
    reporter = _current.get()
    if reporter:
        reporter.emit(message, level=level, **details)


def enabled():
    return _current.get() is not None


def clean(text):
    text = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', str(text))
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    text = re.sub(r'([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@', r'\1[REDACTED]@', text)
    text = re.sub(r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9+/=._-]+', r'\1 [REDACTED]', text)
    return ''.join(' ' if ord(c) < 32 or 127 <= ord(c) <= 159 else c for c in text)[:2000]


class Progress:
    def __init__(self, directory, run_id, *, redact=lambda text: text, stream=None, quiet=False, interval=5):
        if not math.isfinite(interval) or not .1 <= interval <= 60:
            raise ValueError('Progress interval must be 0.1–60 seconds')
        self.directory, self.run_id = directory, run_id
        self.redact, self.stream, self.quiet = redact, stream or sys.stderr, quiet
        self.interval, self.started = interval, time.monotonic()
        self.lock, self.stop = threading.RLock(), threading.Event()
        self.phase, self.label, self.phase_started = 'run', 'Running lifecycle', self.started
        self.last_message = self.started
        self.errors = []
        self.text = self.events = None
        self.thread = None

    def emit(self, message, *, level='INFO', **details):
        with self.lock:
            elapsed = time.monotonic() - self.started
            event = dict(time=datetime.now(timezone.utc).isoformat(), elapsedSeconds=round(elapsed, 3),
                runId=self.run_id, phase=self.phase, level=level, message=clean(self.redact(str(message))))
            # Round-trip through JSON and sanitize every string, including structured values.
            def scrub(value):
                if isinstance(value, str):
                    return clean(self.redact(value))
                if isinstance(value, dict):
                    return {clean(str(k)): scrub(v) for k, v in value.items()}
                if isinstance(value, (list, tuple)):
                    return [scrub(v) for v in value]
                return value
            event.update(scrub({k: v for k, v in details.items() if k not in event}))
            line = f"{event['time'][11:19]}Z +{elapsed:7.1f}s {level:<5} [{self.phase}] {event['message']}"
            self.last_message = time.monotonic()
            for name, stream, content in (('progress.log', self.text, line),
                    ('events.jsonl', self.events, json.dumps(event, ensure_ascii=True, allow_nan=False)),
                    ('console', None if self.quiet else self.stream, line)):
                if stream is not None:
                    try:
                        stream.write(content + '\n')
                        stream.flush()
                    except (OSError, ValueError) as error:
                        if name not in self.errors:
                            self.errors.append(name)

    def heartbeat(self):
        while not self.stop.wait(min(self.interval, 1)):
            with self.lock:
                if time.monotonic() - self.last_message >= self.interval:
                    elapsed = time.monotonic() - self.phase_started
                    self.emit(f'{self.label} — still working ({elapsed:.0f}s in this step)', event='heartbeat')

    @contextmanager
    def activate(self):
        try:
            for filename, attribute in (('progress.log', 'text'), ('events.jsonl', 'events')):
                descriptor = os.open(self.directory / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
                setattr(self, attribute, os.fdopen(descriptor, 'a'))
            token = _current.set(self)
            self.thread = threading.Thread(target=self.heartbeat, name='run-progress', daemon=True)
            self.thread.start()
            try:
                yield self
            finally:
                _current.reset(token)
        finally:
            self.stop.set()
            if self.thread:
                self.thread.join(timeout=2)
            for stream in (self.text, self.events):
                if stream:
                    stream.close()

    @contextmanager
    def operation(self, phase, label):
        with self.lock:
            previous = self.phase, self.label, self.phase_started
            self.phase, self.label, self.phase_started = phase, label, time.monotonic()
            self.emit(label, event='step-start')
        try:
            yield
        except BaseException as error:
            self.emit(f'{label} — {"cancelled" if not isinstance(error, Exception) else "failed"}: '
                      f'{type(error).__name__}: {error}', level='ERROR', event='step-failed')
            raise
        else:
            self.emit(f'{label} — done ({time.monotonic() - self.phase_started:.1f}s)', event='step-complete')
        finally:
            with self.lock:
                self.phase, self.label, self.phase_started = previous
