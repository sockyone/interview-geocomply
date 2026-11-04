"""Internal runner LoadProfile documents for engine-level tests.

Users write load settings in a LoadTestDefinition's ``spec.load``; the runner turns that into
this internal document. ``checkout`` is the real definition's load; ``smoke`` is a short shape
used only by tests.
"""
from pathlib import Path

import yaml

from perf_runner.definition import profile_document

ROOT = Path(__file__).resolve().parents[2]
DEFINITION = ROOT / 'LoadTestDefinitions/checkout.yaml'


def checkout_load():
    return yaml.safe_load(DEFINITION.read_text())['spec']['load']


SMOKE_LOAD = {
    'flow': [{'journey': 'browse', 'repeat': 3}, {'waitSeconds': 2}, {'journey': 'purchase'}],
    'spawnRate': 1, 'drainTimeoutSeconds': 5,
    'stages': [{'name': 'warmup', 'transition': 'hold', 'durationSeconds': 3, 'users': 1, 'measure': False},
               {'name': 'steady', 'transition': 'hold', 'durationSeconds': 6, 'users': 1, 'measure': True}],
    'thresholds': [{'scope': 'measured', 'metric': 'journeyP95Ms', 'max': 2000},
                   {'scope': 'steady', 'metric': 'journeyFailureRatio', 'max': 0},
                   {'scope': 'measured', 'metric': 'httpFailureRatio', 'max': 0}]}


def profile(name):
    """'smoke' (tests only) or 'baseline' (the checkout definition's own load)."""
    return profile_document(name, SMOKE_LOAD if name == 'smoke' else checkout_load())
