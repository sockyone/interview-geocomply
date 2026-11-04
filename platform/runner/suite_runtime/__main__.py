# Patch before importing networking libraries used by Locust/requests.
from gevent import monkey
monkey.patch_all()

import argparse
import json
import os
from pathlib import Path
import sys

from .loader import load_suite
from .runner import Options, run_suite
from .profile import load_profile


def main():
    parser = argparse.ArgumentParser(description='Execute user-supplied TestSuite flows with the shared Locust runtime')
    parser.add_argument('--suite', type=Path, required=True, help='Mounted TestSuite YAML alongside its Python files')
    parser.add_argument('--list-journeys', action='store_true')
    parser.add_argument('--print-result', action='store_true', help='Also emit final JSON for Kubernetes Job log collection')
    parser.add_argument('--base-url', default=os.environ.get('BASE_URL'))
    parser.add_argument('--run-id', default=os.environ.get('RUN_ID'))
    parser.add_argument('--journey', action='append', dest='journeys')
    parser.add_argument('--profile', type=Path, help='Separate LoadProfile YAML')
    parser.add_argument('--iterations', type=int, help='Total attempts per selected journey')
    parser.add_argument('--users', type=int, help='Concurrent closed-model Locust users')
    parser.add_argument('--request-timeout', type=float, default=3)
    parser.add_argument('--execution-timeout', type=float)
    parser.add_argument('--drain-timeout', type=float)
    parser.add_argument('--fixture', type=Path)
    parser.add_argument('--result', type=Path, default=Path(os.environ.get('RESULT_PATH', '/results/result.json')))
    args = parser.parse_args()
    try:
        with load_suite(args.suite, fixture_override=args.fixture) as suite:
            if args.list_journeys:
                print(json.dumps(suite.descriptions, indent=2))
                return 0
            if not args.base_url or not args.run_id:
                parser.error('Supply --base-url/BASE_URL and --run-id/RUN_ID')
            profile = load_profile(args.profile, suite.journeys) if args.profile else None
            if profile and any(v is not None for v in (args.journeys, args.iterations, args.users,
                                                       args.execution_timeout, args.drain_timeout)):
                raise ValueError('Profile owns journeys, users, duration and drain; omit smoke flags')
            sequence = 0
            def report_progress(event):
                nonlocal sequence
                sequence += 1
                print('PERF_PROGRESS ' + json.dumps(dict(event, sequence=sequence), allow_nan=False), flush=True)
            code, record = run_suite(Options(args.base_url, args.run_id,
                profile.journeys if profile else tuple(args.journeys or suite.journeys),
                args.result, args.iterations if args.iterations is not None else 1,
                args.users if args.users is not None else 1, args.request_timeout,
                profile.execution_timeout if profile else args.execution_timeout if args.execution_timeout is not None else 60,
                profile.drain_timeout if profile else args.drain_timeout if args.drain_timeout is not None else 2,
                profile), suite, bearer_token=os.environ.get('AUTH_BEARER_TOKEN'),
                progress=report_progress if os.environ.get('PERF_RUN_PROGRESS') == '1' else None)
        print(json.dumps(record if args.print_result else {'runId': record['runId'], 'state': record['state'],
                          'outcome': record['outcome'], 'result': str(args.result)}, allow_nan=False))
        return code
    except Exception as error:
        print(f'Suite execution failed: {type(error).__name__}: {error}', file=sys.stderr)
        return 1


raise SystemExit(main())
