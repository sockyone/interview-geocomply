"""Explicit cluster access and bounded local port-forwarding for lifecycle steps."""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import subprocess
import sys
import time

from perf_runner.database import RUN_LABEL, _stop, _write_private


def run_command(args, *, data=None, timeout=30):
    """Capture a command; timeout/cancellation also stop its child process group."""
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(input=data, timeout=timeout)
    except BaseException as error:
        try:
            _stop(process)
        except Exception as cleanup_error:
            error.add_note(f'Command cleanup failed: {type(cleanup_error).__name__}')
        raise
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


class Kubernetes:
    def __init__(self, target, run):
        self.target, self.run = target, run
        if not target.kubeconfig.is_file() or not target.context:
            raise ValueError('Explicit kubeconfig file and context required')
        self.base = ['kubectl', '--kubeconfig', str(target.kubeconfig.resolve()),
                     '--context', target.context, '--namespace', run.namespace]

    def command(self, *args, data=None, timeout=30):
        result = run_command(self.base + list(args), data=json.dumps(data) if data else None, timeout=timeout)
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
        return result.stdout

    def assert_owned_namespace(self):
        value = json.loads(self.command('get', 'namespace', self.run.namespace, '-o', 'json'))
        if (value['metadata'].get('labels', {}).get(RUN_LABEL) != self.run.run_id
                or value['metadata'].get('deletionTimestamp')):
            raise ValueError('Namespace is not owned by this active run')

    @contextmanager
    def port_forward(self, service, port, log_path):
        """Yield a localhost HTTP URL; always stop/reap the child process group."""
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _write_private(log_path, '')
        with log_path.open('a') as output:
            process = subprocess.Popen(self.base + ['port-forward', f'service/{service}',
                f':{port}', '--address', '127.0.0.1'], stdout=output, stderr=output, start_new_session=True)
        try:
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None:
                    raise RuntimeError('HTTP port-forward exited before startup')
                match = re.search(rf'Forwarding from 127\.0\.0\.1:(\d+) -> {port}\b', log_path.read_text())
                if match:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('HTTP port-forward startup timed out')
                time.sleep(.1)
            yield f'http://127.0.0.1:{match[1]}'
            if process.poll() is not None:
                raise RuntimeError('HTTP port-forward exited unexpectedly')
        finally:
            primary_error = sys.exc_info()[0]
            try:
                _stop(process)
            except Exception:
                if primary_error is None:
                    raise

    def delete_owned_namespace(self, timeout_seconds=120):
        self.assert_owned_namespace()
        return self.command('delete', 'namespace', self.run.namespace, '--wait=true',
                            f'--timeout={int(timeout_seconds)}s', timeout=timeout_seconds + 10)
