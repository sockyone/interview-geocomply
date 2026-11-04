"""Pure preflight contract; no Locust import needed on the host."""
from copy import deepcopy
from pathlib import Path
import unittest
import yaml

from perf_runner.load_profile import parse_profile
from load_fixtures import profile as internal_profile

ROOT = Path(__file__).resolve().parents[2]


class LoadProfileTests(unittest.TestCase):
    def setUp(self):
        self.document = internal_profile('baseline')

    def test_examples_and_schedule_boundaries(self):
        profile = parse_profile(self.document, ('browse', 'purchase'))
        self.assertEqual(profile.duration, 40)
        self.assertEqual(profile.target(0), (0, 1))     # ramp from zero toward 10 users
        self.assertEqual(profile.target(9), (0, 10))
        self.assertEqual(profile.target(10), (1, 10))   # hold
        self.assertEqual(profile.target(39), (1, 10))
        self.assertIsNone(profile.target(40))
        self.assertGreater(profile.job_deadline, profile.execution_timeout)
        parse_profile(internal_profile('smoke'), ('browse', 'purchase'))

    def test_flow_is_an_ordered_cycle_with_pauses(self):
        profile = parse_profile(self.document, ('browse', 'purchase'))
        self.assertEqual(profile.sequence(), (('browse', 0), ('browse', 0), ('browse', 2), ('purchase', 0)))
        self.assertEqual(profile.journeys, ('browse', 'purchase'))
        self.assertEqual(parse_profile(profile.document(), ('browse', 'purchase')), profile)  # round trip
        document = deepcopy(self.document)
        document['spec']['flow'] = [{'waitSeconds': 1}, {'journey': 'purchase'}, {'waitSeconds': .5}, {'waitSeconds': .5}]
        self.assertEqual(parse_profile(document, ('purchase',)).sequence(), (('purchase', 2),))

    def test_invalid_profiles_fail_preflight(self):
        mutations = [
            lambda s: s.update(model='arrival-rate'),
            lambda s: s.update(journeys={'browse': 3, 'purchase': 1}),     # old weights format
            lambda s: s.update(flow=[]),
            lambda s: s.update(flow=[{'journey': 'missing'}]),
            lambda s: s.update(flow=[{'waitSeconds': 2}]),                  # no journey at all
            lambda s: s.update(flow=[{'journey': 'browse', 'repeat': 0}]),
            lambda s: s.update(flow=[{'journey': 'browse', 'repeat': True}]),
            lambda s: s.update(flow=[{'journey': 'browse'}, {'waitSeconds': 0}]),
            lambda s: s.update(flow=[{'journey': 'browse', 'waitSeconds': 1}]),
            lambda s: s.update(flow=[{'journey': 'browse', 'repeat': 100}] * 11),
            lambda s: s.update(spawnRate=float('nan')),
            lambda s: s.update(drainTimeoutSeconds=0),
            lambda s: s.update(extra=1),
            lambda s: s['stages'][0].update(durationSeconds=1),
            lambda s: s['stages'][0].update(users=257),
            lambda s: s['stages'][0].update(transition='hold'),               # 10 users at once > spawnRate
            lambda s: s['stages'][1].update(users=4),                         # a hold must keep the user count
            lambda s: s['stages'][1].update(name='ramp-up'),
            lambda s: s['stages'][1].update(measure='false'),
            lambda s: [stage.update(measure=False) for stage in s['stages']],
            lambda s: s.update(thresholds=[{'scope': 'ramp-up', 'metric': 'journeyP95Ms', 'max': 1}]),
            lambda s: s['thresholds'][0].update(metric='rps'),
            lambda s: s['thresholds'][0].update(max=-1),
            lambda s: s['thresholds'].append(s['thresholds'][0]),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                document = deepcopy(self.document)
                mutate(document['spec'])
                with self.assertRaises(ValueError):
                    parse_profile(document, ('browse', 'purchase'))
