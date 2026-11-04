"""Versioned published LoadTestDefinition and cross-component preflight."""
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
import json
import math
from importlib.resources import files
from pathlib import Path
import re

from jsonschema import Draft202012Validator
import yaml

from perf_runner.load_profile import LoadProfile, flow_journeys, parse_profile
from perf_runner.platform import parse_named
from perf_runner.registry import ArtifactRef
from perf_runner.services import PaymentStubSpec, payment_definition


@dataclass(frozen=True)
class PublishedSeed:
    image: str | None  # None: run the seed module from the application image
    module: str
    args: tuple[str, ...]
    timeout_seconds: int
    bindings: dict[str, str]


@dataclass(frozen=True)
class PublishedDatabase:
    type: str
    version: str
    name: str
    readiness_timeout_seconds: int
    seed: PublishedSeed


@dataclass(frozen=True)
class ApplicationSpec:
    definition: str  # ApplicationDefinition name[:version]
    bindings: dict[str, str]


@dataclass(frozen=True)
class ExecutionSpec:
    request_timeout_seconds: float
    application_readiness_timeout_seconds: int
    job_startup_allowance_seconds: int
    resources: dict


@dataclass(frozen=True)
class LoadTestDefinition:
    name: str
    application: ApplicationSpec
    database: PublishedDatabase
    services: dict[str, PaymentStubSpec]
    suite: str  # TestSuite name[:version]
    profile: LoadProfile
    execution: ExecutionSpec
    cleanup_timeout_seconds: int
    document: dict

    @property
    def job_deadline(self):
        import math
        return math.ceil(self.profile.execution_timeout + self.execution.job_startup_allowance_seconds)


def image_reference(image):
    if image.get('digest'):
        return str(ArtifactRef.parse(image['repository'] + '@' + image['digest']))
    return str(ArtifactRef.parse(image['repository'] + ':' + image['tag']))


def output_names(services):
    return {'database.host', 'database.port', 'database.database', 'database.user',
            'database.password', 'database.url'} | {f'services.{name}.{field}'
            for name in services for field in ('host', 'port', 'url')}


def validate_resources(resources):
    quantities = {}
    for category in ('requests', 'limits'):
        for resource in ('cpu', 'memory'):
            value = resources[category][resource]
            if resource == 'cpu':
                if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?m?', value):
                    raise ValueError('CPU must use cores or millicores')
                quantity = Decimal(value[:-1]) / 1000 if value.endswith('m') else Decimal(value)
                maximum = 16
            else:
                match = re.fullmatch(r'([0-9]+)(Mi|Gi)', value)
                if not match:
                    raise ValueError('Memory must use integer Mi or Gi')
                quantity = Decimal(match[1]) * (1024 if match[2] == 'Gi' else 1)
                maximum = 16384
            if not 0 < quantity <= maximum:
                raise ValueError('Runner resources exceed supported positive bounds (16 CPU, 16 GiB)')
            quantities[category, resource] = quantity
    if any(quantities['requests', key] > quantities['limits', key] for key in ('cpu', 'memory')):
        raise ValueError('Resource requests must not exceed limits')


def parse_definition(document):
    schema = json.loads(files('perf_runner').joinpath('schemas/load-test-definition.schema.json').read_text())
    errors = sorted(Draft202012Validator(schema).iter_errors(document), key=lambda e: str(list(e.path)))
    if errors:
        # A validation error may contain a credential-bearing value; expose only its location.
        raise ValueError('Invalid LoadTestDefinition at ' + '.'.join(map(str, errors[0].absolute_path)))
    document = deepcopy(document)
    spec = document['spec']
    app, db, execution = spec['application'], spec['database'], spec['execution']
    # Applications and suites are referred to by name; the platform says where they live.
    parse_named(app['definition'], 'application.definition')
    parse_named(spec['suite'], 'suite')
    services = {}
    for name, raw in spec['services'].items():
        if name in {'application', 'database'}:
            raise ValueError('Reserved supporting service name')
        service = PaymentStubSpec(None, raw['delayMs'], raw['outcome'])
        payment_definition(name, service)
        services[name] = service
    outputs = output_names(services)
    if set(app['bindings'].values()) - outputs:
        raise ValueError('Unknown application output binding')
    if set(db['seed']['bindings'].values()) - {key for key in outputs if key.startswith('database.')}:
        raise ValueError('Seed can bind only database outputs; services/app are created after seeding')
    # The runner's internal LoadProfile is generated from spec.load; users never write one.
    profile = parse_profile(profile_document(document['metadata']['name'], spec['load']), flow_journeys(spec['load']))
    if not math.isfinite(execution['requestTimeoutSeconds']):
        raise ValueError('Request timeout must be finite')
    validate_resources(execution['resources'])
    seed = db['seed']
    return LoadTestDefinition(document['metadata']['name'],
        ApplicationSpec(app['definition'], app['bindings']),
        PublishedDatabase(db['type'], db['version'], db['name'], db['readinessTimeoutSeconds'],
            PublishedSeed(seed.get('image'), seed['module'], tuple(seed['args']), seed['timeoutSeconds'],
                          seed['bindings'])),
        services, spec['suite'], profile,
        ExecutionSpec(execution['requestTimeoutSeconds'], execution['applicationReadinessTimeoutSeconds'],
                      execution['jobStartupAllowanceSeconds'], execution['resources']),
        spec['cleanup']['timeoutSeconds'], document)


def profile_document(name, load):
    """Internal LoadProfile document for the runner image, built from a definition's ``spec.load``."""
    return {'apiVersion': 'perf-runner/v1alpha1', 'kind': 'LoadProfile', 'metadata': {'name': name},
            'spec': {'model': 'closed-users', **deepcopy(load)}}


def load_definition(path):
    path = Path(path)
    if path.stat().st_size > 256 * 1024:
        raise ValueError('Definition exceeds 256 KiB')
    try:
        return parse_definition(yaml.safe_load(path.read_text()))
    except yaml.YAMLError:
        raise ValueError('Invalid definition YAML') from None
