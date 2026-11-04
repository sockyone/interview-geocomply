"""Start-stage attribution using independent Locust event histograms, never resets."""
from locust.stats import RequestStats, StatsEntry
from .results import timing


class ProfileMetrics:
    def __init__(self, profile):
        self.profile = profile
        self.entries = {scope: {kind: StatsEntry(RequestStats(), scope, kind)
                        for kind in ('JOURNEY', 'HTTP')}
                        for scope in ('measured', *(stage.name for stage in profile.stages))}

    def sample(self, stage_index, method, duration, failed):
        if stage_index is None:
            return
        stage = self.profile.stages[stage_index]
        for scope in (stage.name, 'measured') if stage.measure else (stage.name,):
            entry = self.entries[scope]['JOURNEY' if method == 'JOURNEY' else 'HTTP']
            entry.log(duration, 0)
            if failed:
                entry.log_error(None)

    def summary(self, scope):
        return {kind.lower(): {'samples': entry.num_requests, 'failures': entry.num_failures,
                              'timingMs': timing(entry)} for kind, entry in self.entries[scope].items()}

    def evaluate(self, complete):
        outcomes = []
        for threshold in self.profile.thresholds:
            entry = self.entries[threshold.scope]['HTTP' if threshold.metric == 'httpFailureRatio' else 'JOURNEY']
            value = (entry.get_response_time_percentile(.95) if threshold.metric == 'journeyP95Ms'
                     else entry.num_failures / entry.num_requests) if entry.num_requests else None
            outcomes.append({'scope': threshold.scope, 'metric': threshold.metric, 'max': threshold.maximum,
                'samples': entry.num_requests, 'actual': value,
                'outcome': 'incomplete' if not complete else 'no-samples' if value is None else
                           'passed' if value <= threshold.maximum else 'failed'})
        return outcomes
