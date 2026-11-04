"""Durable TestResult document and readable report built from one run's retained evidence.

Nothing here queries the cluster: the report must stay useful after the namespace and
an ephemeral cluster are gone. Percentiles are copied from engine-owned histograms;
separate runs or stages are never averaged into a new percentile.
"""
from pathlib import Path

EVIDENCE = {'progress': 'progress.log', 'progressEvents': 'events.jsonl', 'record': 'run.json', 'resolvedSnapshot': 'resolved/resolved.json', 'suiteResult': 'suite/result.json',
            'suiteLog': 'suite/suite.log', 'suiteJob': 'suite/job.json', 'databaseRecord': 'database/result.json',
            'seedLog': 'database/seed/seed.log', 'diagnostics': 'diagnostics', 'resourceUsage': 'metrics/resource-usage.json',
            'resourceSeries': 'metrics/series.json', 'resourceCsv': 'metrics/resource-usage.csv'}


def _timing(block):
    timing = (block or {}).get('timingMs') or {}
    return {key: timing.get(key) for key in ('samples', 'p50', 'p95', 'p99', 'max')} if timing else None


def build_test_result(record, suite, usage, directory):
    """Assemble the TestResult. ``suite`` is the engine result (or None), ``usage`` the metrics document."""
    directory = Path(directory)
    suite = suite or {}
    limitations = []
    if not suite:
        limitations.append('No suite result was produced: execution did not complete, so performance is unknown.')
    elif suite.get('executionOutcome') != 'completed':
        limitations.append('Execution was incomplete; stage and threshold figures must not be treated as acceptance evidence.')
    if usage and not usage.get('available'):
        limitations.append('Resource metrics unavailable: ' + usage.get('reason', 'no samples were returned') + '.')
    elif usage and usage.get('missingRoles'):
        limitations.append('No resource samples for: ' + ', '.join(usage['missingRoles'])
                           + ' (Pods shorter than one scrape interval may not be sampled).')
    if usage and usage.get('roles'):
        short = [role for role, item in usage['roles'].items() if item['cpuCores'] is None and item['memoryBytes']]
        if short:
            limitations.append('CPU unavailable for ' + ', '.join(short) + ': a rate needs two scrapes and these '
                               'Pods lived too briefly; memory had a single sample. Use a longer profile for CPU evidence.')
    if record.get('outcome', {}).get('collection') != 'ok':
        limitations.append('Diagnostic collection was incomplete: ' + '; '.join(record.get('collectionErrors', ['see run.json'])[:5]))
    if record.get('outcome', {}).get('cleanup') == 'failed':
        limitations.append('Namespace cleanup failed; see cleanupHint in the run record.')
    stages = [{'name': s['name'], 'transition': s['transition'], 'durationSeconds': s['durationSeconds'],
               'users': s['users'], 'measured': s['measure'], 'startedAt': s.get('startedAt'),
               'finishedAt': s.get('finishedAt'), 'reachedTarget': s.get('reachedTarget'),
               'journey': {'samples': s['journey']['samples'], 'failures': s['journey']['failures'],
                           'timingMs': _timing(s['journey'])},
               'http': {'samples': s['http']['samples'], 'failures': s['http']['failures'],
                        'timingMs': _timing(s['http'])}} for s in suite.get('stages', [])]
    measured = suite.get('measured')
    return {
        'apiVersion': 'perf-runner/v1alpha1', 'kind': 'TestResult', 'runId': record['runId'],
        'definition': record['definition'], 'status': record['status'], 'exitCode': record['exitCode'],
        'outcome': record['outcome'],
        'timestamps': {'runStartedAt': record.get('startedAt'), 'runFinishedAt': record.get('finishedAt'),
                       'suiteStartedAt': suite.get('startedAt'), 'suiteFinishedAt': suite.get('finishedAt')},
        'artifacts': record.get('artifacts', {}),
        'engine': suite.get('engine'),
        'resolvedProfile': suite.get('resolvedProfile'),
        'stages': stages,
        'measured': {'journey': {**measured['journey'], 'timingMs': _timing(measured['journey'])},
                     'http': {**measured['http'], 'timingMs': _timing(measured['http'])}} if measured else None,
        'thresholds': suite.get('thresholdOutcomes', []),
        'journeys': {name: {key: counts[key] for key in ('attempted', 'passed', 'failed', 'interrupted')}
                     | {'timingMs': _timing(counts)} for name, counts in suite.get('journeys', {}).items()},
        'http': [{'method': h['method'], 'name': h['name'], 'requests': h['requests'], 'failures': h['failures'],
                  'timingMs': _timing(h)} for h in suite.get('http', [])],
        'executionErrors': suite.get('executionErrors', []), 'generatorWarnings': suite.get('generatorWarnings', []),
        'resourceUsage': usage, 'errors': record.get('errors', []), 'limitations': limitations,
        'evidence': {name: path for name, path in EVIDENCE.items() if (directory / path).exists()},
    }


def _ms(timing, key='p95'):
    return '-' if not timing or timing.get(key) is None else f'{timing[key]:.0f}'


def render_report(result):
    outcome = result['outcome']
    lines = [f"# {result['definition']} — {result['status'].upper()}", '',
             f"Run `{result['runId']}` · exit code {result['exitCode']}", '',
             '| Execution | Performance | Collection | Cleanup |', '| --- | --- | --- | --- |',
             f"| {outcome['execution']} | {outcome['performance']} | {outcome['collection']} | {outcome['cleanup']} |", '']
    if result['evidence'].get('dashboard'):
        lines += [f"![Dashboard for {result['runId']}]({result['evidence']['dashboard']})", '']
    stamps = result['timestamps']
    lines += [f"Suite ran {stamps['suiteStartedAt'] or '-'} → {stamps['suiteFinishedAt'] or '-'} "
              f"(run {stamps['runStartedAt']} → {stamps['runFinishedAt']}).", '']
    if result['limitations']:
        lines += ['## Limitations', *[f'- {text}' for text in result['limitations']], '']
    if result['errors']:
        lines += ['## Errors', *[f"- `{e['phase']}` {e['type']}: {e['message']}" for e in result['errors']], '']
    if result['thresholds']:
        lines += ['## Thresholds', '| Scope | Metric | Max | Actual | Samples | Outcome |', '| --- | --- | --- | --- | --- | --- |']
        lines += [f"| {t['scope']} | {t['metric']} | {t['max']} | {t.get('actual')} | {t.get('samples')} | {t['outcome']} |"
                  for t in result['thresholds']]
        lines.append('')
    if result['stages']:
        lines += ['## Stages', '| Stage | Mode | Users | Measured | Journeys | Failures | Journey p95 ms | HTTP p95 ms |',
                  '| --- | --- | --- | --- | --- | --- | --- | --- |']
        lines += [f"| {s['name']} | {s['transition']} {s['durationSeconds']}s | {s['users']} | {'yes' if s['measured'] else 'no'} "
                  f"| {s['journey']['samples']} | {s['journey']['failures']} | {_ms(s['journey']['timingMs'])} "
                  f"| {_ms(s['http']['timingMs'])} |" for s in result['stages']]
        lines.append('')
    if result['journeys']:
        lines += ['## Journeys', '| Journey | Attempted | Passed | Failed | p95 ms |', '| --- | --- | --- | --- | --- |']
        lines += [f"| {n} | {j['attempted']} | {j['passed']} | {j['failed']} | {_ms(j['timingMs'])} |"
                  for n, j in sorted(result['journeys'].items())]
        lines.append('')
    usage = result['resourceUsage']
    if usage and usage.get('roles'):
        lines += ['## Resource usage', f"Prometheus window {usage['window']['start']} → {usage['window']['end']}.",
                  '| Role | CPU peak (cores) | CPU mean | Memory peak (MiB) |', '| --- | --- | --- | --- |']
        for role, item in usage['roles'].items():
            cpu, mem = item['cpuCores'], item['memoryBytes']
            lines.append(f"| {role} | {cpu['peak']:.3f} | {cpu['mean']:.3f} | " if cpu else f"| {role} | no samples | - | ")
            lines[-1] += (f"{mem['peak'] / 1048576:.0f} |" if mem else '- |')
        lines.append('')
    lines += ['## Artifacts', *[f"- {role}: `{a['resolved']}`" for role, a in sorted(result['artifacts'].items())], '',
              '## Evidence (relative to the run directory)', *[f'- {name}: `{path}`' for name, path in result['evidence'].items()], '']
    return '\n'.join(lines)
