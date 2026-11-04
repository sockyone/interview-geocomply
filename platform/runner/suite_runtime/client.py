"""Bounded requests through Locust's instrumented client; no write retries."""
from uuid import uuid4


class JourneyClient:
    def __init__(self, session, *, run_id, timeout, bearer_token=None):
        self.session, self.timeout = session, timeout
        self.headers = {'X-Perf-Run-Id': run_id, 'X-Perf-Journey-Id': str(uuid4())}
        if bearer_token:
            self.headers['Authorization'] = 'Bearer ' + bearer_token

    def request(self, method, path, **kwargs):
        headers = {**self.headers, **kwargs.pop('headers', {})}
        return self.session.request(method, path, timeout=(self.timeout, self.timeout),
            allow_redirects=False, headers=headers, **kwargs)

    def get(self, path, **kwargs):
        return self.request('GET', path, **kwargs)

    def post(self, path, **kwargs):
        return self.request('POST', path, **kwargs)

    def patch(self, path, **kwargs):
        return self.request('PATCH', path, **kwargs)

    def put(self, path, **kwargs):
        return self.request('PUT', path, **kwargs)

    def delete(self, path, **kwargs):
        return self.request('DELETE', path, **kwargs)
