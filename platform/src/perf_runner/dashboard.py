"""Render one dashboard image for a run (needs matplotlib: pip install -e './platform[charts]').

Reads only the run directory, so it works after the namespace is gone:

- ``testresult.json``: stages, thresholds, per-journey and per-endpoint latency;
- ``suite/suite.log``: the load generator's progress events (users and journeys every ~5 s);
- ``metrics/series.json`` + ``resource-usage.json``: Prometheus CPU/memory samples per Pod.

Time is shown as seconds since the load started (setup appears as negative time). Every
Prometheus sample is drawn as a marker; nothing is interpolated beyond the connecting line.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path

SURFACE, INK, MUTED, GRID = '#fcfcfb', '#0b0b0b', '#52514e', '#e4e3df'
# Categorical slots in fixed order; a component keeps its colour in every panel.
COMPONENT_COLORS = {'application': '#2a78d6', 'database': '#eb6834', 'generator': '#1baf7a',
                    'seed': '#eda100', 'service': '#e87ba4'}
PERCENTILE_COLORS = {'p50': '#86b6ef', 'p95': '#2a78d6', 'p99': '#104281'}  # ordinal blue ramp
GOOD, CRITICAL = '#0ca30c', '#d03b3b'
STAGE_SHADES = ('#f1f0ec', '#e6eef8')


def _time(value):
    return datetime.fromisoformat(value).timestamp()


def component_color(role):
    return COMPONENT_COLORS.get(role.split(':', 1)[0], MUTED)


def progress_events(directory):
    """PERF_PROGRESS events printed by the load generator, in order."""
    path = Path(directory) / 'suite/suite.log'
    events = []
    if path.exists():
        for line in path.read_text().splitlines():
            if line.startswith('PERF_PROGRESS '):
                try:
                    events.append(json.loads(line.removeprefix('PERF_PROGRESS ')))
                except ValueError:
                    continue
    return sorted(events, key=lambda event: (event.get('elapsedSeconds', 0), event.get('sequence', 0)))


def throughput(events):
    """[(midpoint seconds, journeys/s, failed journeys/s)] between consecutive progress events."""
    points = [(e['elapsedSeconds'], e.get('completedJourneys', 0), e.get('failedJourneys', 0))
              for e in events if 'completedJourneys' in e]
    rates = []
    for (t0, c0, f0), (t1, c1, f1) in zip(points, points[1:]):
        if t1 - t0 >= 0.5 and c1 >= c0:
            rates.append(((t0 + t1) / 2, (c1 - c0) / (t1 - t0), max(0, f1 - f0) / (t1 - t0)))
    return rates


def resource_series(directory):
    """{metric: {role: [(unix, value)]}} summed over each role's containers per timestamp."""
    directory = Path(directory)
    try:
        usage = json.loads((directory / 'metrics/resource-usage.json').read_text())
        series = json.loads((directory / 'metrics/series.json').read_text())
    except (OSError, ValueError):
        return {}
    pod_role = {pod: role for role, item in usage.get('roles', {}).items() for pod in item.get('pods', [])}
    totals = {}
    for metric, items in series.items():
        for item in items:
            role = pod_role.get(item['metric'].get('pod'))
            if role is None:
                continue
            bucket = totals.setdefault(metric, {}).setdefault(role, {})
            for timestamp, value in item['values']:
                bucket[float(timestamp)] = bucket.get(float(timestamp), 0.0) + float(value)
    return {metric: {role: sorted(points.items()) for role, points in roles.items()}
            for metric, roles in totals.items()}


def _style(axis, title):
    axis.set_facecolor(SURFACE)
    axis.set_title(title, loc='left', fontsize=10, color=INK, fontweight='bold')
    axis.grid(color=GRID, linewidth=.6)
    axis.set_axisbelow(True)
    for side in ('top', 'right'):
        axis.spines[side].set_visible(False)
    for side in ('left', 'bottom'):
        axis.spines[side].set_color(GRID)
    axis.tick_params(colors=MUTED, labelsize=8)


def _shade_stages(axis, stages, origin, label=False):
    for index, stage in enumerate(stages):
        start, end = _time(stage['startedAt']) - origin, _time(stage['finishedAt']) - origin
        axis.axvspan(start, end, color=STAGE_SHADES[index % 2], zorder=0, linewidth=0)
        if label:
            name = stage['name'] + ('' if stage.get('measured') else ' (not measured)')
            axis.text((start + end) / 2, .97, name, ha='center', va='top', fontsize=7.5, color=MUTED,
                      transform=axis.get_xaxis_transform())


def _bar_labels(axis, bars, fmt='{:.0f}'):
    for bar in bars:
        axis.annotate(fmt.format(bar.get_height()),
                      (bar.get_x() + bar.get_width() / 2, bar.get_height()), xytext=(0, 2),
                      textcoords='offset points', ha='center', va='bottom', fontsize=7, color=MUTED)


def render(directory, output=None):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    directory = Path(directory)
    result = json.loads((directory / 'testresult.json').read_text())
    stages = result.get('stages') or []
    if not stages:
        raise ValueError('No stages in the result: the load did not run, so there is nothing to chart')
    origin = _time(stages[0]['startedAt'])
    load_end = _time(stages[-1]['finishedAt']) - origin
    events = progress_events(directory)
    resources = resource_series(directory)

    figure, axes = plt.subplots(3, 2, figsize=(14, 11.5), facecolor=SURFACE)
    figure.subplots_adjust(left=.05, right=.98, top=.84, bottom=.06, hspace=.5, wspace=.32)

    # 1. Users over time.
    axis = axes[0][0]
    _style(axis, 'Simulated users')
    _shade_stages(axis, stages, origin, label=True)
    if events:
        times = [e['elapsedSeconds'] for e in events]
        axis.step(times, [e.get('targetUsers', 0) for e in events], where='post', color=MUTED, linewidth=1.5,
                  linestyle='--', label='target')
        axis.plot(times, [e.get('actualUsers', 0) for e in events], color=COMPONENT_COLORS['generator'],
                  linewidth=2, marker='o', markersize=4, label='actual')
        axis.legend(loc='lower right', fontsize=8, frameon=False)
    axis.set_xlim(0, load_end)
    peak = max([e.get('targetUsers', 0) for e in events] + [s.get('users', 0) for s in stages] + [1])
    axis.set_ylim(0, peak * 1.3)
    axis.set_ylabel('users', fontsize=8, color=MUTED)

    # 2. Throughput.
    axis = axes[0][1]
    _style(axis, 'Completed journeys per second')
    _shade_stages(axis, stages, origin, label=True)
    rates = throughput(events)
    if rates:
        axis.plot([t for t, _, _ in rates], [r for _, r, _ in rates], color=COMPONENT_COLORS['application'],
                  linewidth=2, marker='o', markersize=4, label='journeys/s')
        if any(f for _, _, f in rates):
            axis.plot([t for t, _, _ in rates], [f for _, _, f in rates], color=CRITICAL, linewidth=2,
                      marker='o', markersize=4, label='failed/s')
        axis.legend(loc='lower right', fontsize=8, frameon=False)
    axis.set_xlim(0, load_end)
    axis.set_ylim(0, max([r for _, r, _ in rates] + [1]) * 1.3)
    axis.set_ylabel('journeys / s', fontsize=8, color=MUTED)

    # 3. Latency per journey.
    axis = axes[1][0]
    journeys = result.get('journeys') or {}
    _style(axis, 'Journey latency, whole run (ms)')
    names = sorted(journeys)
    width = .26
    for offset, percentile in enumerate(('p50', 'p95', 'p99')):
        values = [journeys[name]['timingMs'].get(percentile) or 0 for name in names]
        bars = axis.bar([i + (offset - 1) * width for i in range(len(names))], values, width * .92,
                        color=PERCENTILE_COLORS[percentile], label=percentile)
        _bar_labels(axis, bars)
    axis.set_xticks(range(len(names)))
    axis.set_xticklabels([f"{n}\n{journeys[n]['passed']}/{journeys[n]['attempted']} passed" for n in names],
                         fontsize=8, color=INK)
    axis.legend(loc='upper left', fontsize=8, frameon=False, ncol=3)
    axis.margins(y=.18)

    # 4. Latency per endpoint (p95 bar, p50 tick).
    axis = axes[1][1]
    endpoints = sorted(result.get('http') or [], key=lambda item: item['timingMs'].get('p95') or 0)
    _style(axis, 'HTTP latency per endpoint (ms)')
    labels = [f"{item['method']} {item['name']}  ({item['requests']} req)" for item in endpoints]
    p95 = [item['timingMs'].get('p95') or 0 for item in endpoints]
    bars = axis.barh(range(len(endpoints)), p95, .6, color=PERCENTILE_COLORS['p95'], label='p95')
    axis.scatter([item['timingMs'].get('p50') or 0 for item in endpoints], range(len(endpoints)), marker='|',
                 s=160, color=INK, label='p50', zorder=3)
    for bar, value in zip(bars, p95):
        axis.text(bar.get_width(), bar.get_y() + bar.get_height() / 2, f' {value:.0f}', va='center', fontsize=7,
                  color=MUTED)
    axis.set_yticks(range(len(endpoints)))
    axis.set_yticklabels(labels, fontsize=8, color=INK)
    axis.legend(loc='lower right', fontsize=8, frameon=False)
    axis.margins(x=.15)

    # 5-6. CPU and memory per component (Prometheus).
    for axis, metric, title, scale, unit in ((axes[2][0], 'cpuCores', 'CPU per component (cores)', 1.0, 'cores'),
                                              (axes[2][1], 'memoryBytes', 'Memory per component (MiB)', 2**20, 'MiB')):
        _style(axis, title)
        _shade_stages(axis, stages, origin)
        roles = resources.get(metric, {})
        for role in sorted(roles, key=lambda r: list(COMPONENT_COLORS).index(r.split(':')[0])
                           if r.split(':')[0] in COMPONENT_COLORS else 99):
            points = roles[role]
            xs, ys = [t - origin for t, _ in points], [v / scale for _, v in points]
            axis.plot(xs, ys, color=component_color(role), linewidth=2, marker='o', markersize=4,
                      label=role.replace('service:', 'stub: '))
        if roles:
            axis.legend(loc='upper left', fontsize=7.5, frameon=False, ncol=3)
            axis.margins(y=.25)
        else:
            axis.text(.5, .5, 'No Prometheus samples for this run', ha='center', va='center', color=MUTED,
                      transform=axis.transAxes)
        axis.set_ylim(bottom=0)
        axis.set_ylabel(unit, fontsize=8, color=MUTED)
        axis.set_xlabel('seconds since the load started (negative = setup)', fontsize=8, color=MUTED)

    # Header: outcome and thresholds, with icons so status is never colour alone.
    status = result.get('status', 'unknown').upper()
    figure.text(.05, .955, f"{result.get('definition')}  ·  {result.get('runId')}", fontsize=15, color=INK,
                fontweight='bold')
    figure.text(.98, .955, ('✔ ' if status == 'PASSED' else '✖ ') + status, ha='right', fontsize=15,
                fontweight='bold', color=GOOD if status == 'PASSED' else CRITICAL)
    lines = []
    for threshold in result.get('thresholds') or []:
        ok = threshold.get('outcome') == 'passed'
        lines.append(f"{'✔' if ok else '✖'} {threshold['metric']} ({threshold['scope']}) "
                     f"{threshold.get('actual')} ≤ {threshold['max']}")
    started = result.get('timestamps', {}).get('runStartedAt', '')[:19].replace('T', ' ')
    figure.text(.05, .918, f'Run started {started} UTC.  Thresholds:  ' + '     '.join(lines), fontsize=9,
                color=INK)
    figure.text(.05, .893, 'Shaded bands are load stages. Users and throughput come from the load generator '
                '(every ~5 s); CPU and memory are Prometheus scrapes, one marker per sample.', fontsize=8,
                color=MUTED)

    output = Path(output) if output else directory / 'dashboard.png'
    figure.savefig(output, dpi=110, facecolor=SURFACE)
    plt.close(figure)
    return str(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, help='Default: <run-dir>/dashboard.png')
    options = parser.parse_args()
    print(json.dumps({'dashboard': render(options.run_dir, options.output)}))


if __name__ == '__main__':
    main()
