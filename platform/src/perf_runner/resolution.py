"""Resolve published definitions to private local evidence, without cluster access."""
import argparse
import ast
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re

import yaml

from perf_runner.progress import info
from perf_runner.application import bind_application_values, validate_application, validate_chart, _helm
from perf_runner.database import SecretKeyRef, _now, _save, _write_private
from perf_runner.definition import image_reference, load_definition, parse_definition
from perf_runner.guardrails import check_plan, plan_test
from perf_runner.platform import load_platform
from perf_runner.registry import (Registry, ArtifactRef, AD_CONFIG, AD_LAYER, HELM_CONFIG, HELM_LAYER,
                                  SUITE_CONFIG, SUITE_LAYER, checksum)
from perf_runner.services import payment_definition
from suite_runtime.loader import validate_definition as validate_suite, bundle_digest, MAX_BUNDLE_BYTES
from suite_runtime.profile import parse_profile


@dataclass(frozen=True)
class ResolvedDefinition:
    directory: Path
    snapshot: dict
    chart: Path
    suite_directory: Path


def validate_bundle(data):
    if not isinstance(data, dict) or not data or 'suite.yaml' not in data:
        raise ValueError('Suite artifact must contain a flat file mapping with suite.yaml')
    files = {}
    for name, value in data.items():
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,252}', name)
                or Path(name).suffix not in {'.py', '.json', '.yaml', '.yml'} or not isinstance(value, str)):
            raise ValueError('Invalid flat suite bundle file')
        files[name] = value.encode('utf-8')
    if sum(len(value) for value in files.values()) > MAX_BUNDLE_BYTES:
        raise ValueError('Suite bundle exceeds ConfigMap budget')
    definition = validate_suite(yaml.safe_load(files['suite.yaml']))
    for name, content in files.items():
        if name.endswith('.py'):
            compile(content, name, 'exec', optimize=0)
    for entry in definition['spec']['journeys'].values():
        module, function = entry['callable'].split(':')
        if module + '.py' not in files:
            raise ValueError('Journey module is missing')
        tree = ast.parse(files[module + '.py'])
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function]
        if not functions or any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(functions[-1])):
            raise ValueError('Journey must be a declared synchronous non-generator function')
        args = functions[-1].args
        positional = len(args.posonlyargs) + len(args.args)
        if (positional - len(args.defaults) > 1 or (positional < 1 and args.vararg is None)
                or any(v is None for v in args.kw_defaults)):
            raise ValueError('Journey signature must accept one context')
    fixture = definition['spec'].get('fixtures')
    if fixture and (fixture not in files or not isinstance(json.loads(files[fixture]), dict)):
        raise ValueError('Suite JSON fixture is missing or not an object')
    return definition, files


def reject_literal_credentials(definition):
    # Literal env is for nonsecret configuration. Known sensitive names must be bound to Secret outputs.
    for key in definition['spec']['values'].get('env', {}):
        if re.search(r'PASSWORD|PASSWD|SECRET|TOKEN|CREDENTIAL|API_KEY|DATABASE_URL', key, re.I):
            raise ValueError('Sensitive application settings must use output/Secret bindings')


def resolve_definition(definition, directory, *, platform, registry=None):
    """No Kubernetes calls and no user imports. On failure, no ready snapshot exists.

    Names in the definition become registry references through ``platform``; every
    reference is then pinned to an immutable digest in the snapshot.
    """
    definition = parse_definition(definition.document)
    registry = registry or registry_for(platform)
    directory = Path(directory).absolute()
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    state_path = directory / 'resolution.json'
    _save(state_path, {'state': 'resolving', 'startedAt': _now()})
    try:
        identities, image_cache = {}, {}
        def image(key, reference):
            if reference not in image_cache:
                info(f'Resolving {key} image: {reference}')
                image_cache[reference] = registry.image(reference)
            pinned = image_cache[reference]
            identities[key] = {'requested': reference, 'resolved': pinned}
            return pinned

        app_name, app_reference = platform.application(definition.application.definition)
        info(f'Fetching application definition {definition.application.definition!r}: {app_reference}')
        ad_ref, ad_bytes = registry.artifact(app_reference, AD_CONFIG, AD_LAYER)
        ad = yaml.safe_load(ad_bytes)
        validate_application(ad)
        reject_literal_credentials(ad)
        if ad['metadata']['name'] != app_name:
            raise ValueError(f'Published application {app_reference} is named {ad["metadata"]["name"]!r}')
        # The AD names its supported chart; the platform says where that chart lives.
        chart_reference = platform.chart(ad['spec']['chart']['name'], ad['spec']['chart']['version'])
        info(f'Downloading and validating the Helm chart: {chart_reference}')
        chart_ref, chart_bytes = registry.artifact(chart_reference, HELM_CONFIG, HELM_LAYER)
        chart = directory / 'http-service-0.1.0.tgz'
        _write_bytes(chart, chart_bytes)
        validate_chart(chart, ad['spec']['chart'])
        identities['applicationDefinition'] = {'requested': app_reference, 'resolved': ad_ref,
                                              'contentSha256': checksum(ad_bytes)}
        identities['chart'] = {'requested': chart_reference, 'resolved': chart_ref,
                               'contentSha256': checksum(chart_bytes)}
        # Resolve all workload image tags once; replay uses digest references.
        app_image = image('application', image_reference(platform.image_values(ad['spec']['values']['image'])))
        effective_ad = deepcopy(ad)
        _pin_image(effective_ad['spec']['values']['image'], app_image)
        database_image = image('database', platform.database_image(definition.database.type,
                                                                   definition.database.version))
        seed = definition.database.seed
        seed_image = image('setup', platform.image(seed.image)) if seed.image else app_image
        if not seed.image:
            identities['setup'] = {'requested': 'application image', 'resolved': app_image}
        services = {}
        for name, service in definition.services.items():
            pinned = image('service.' + name, platform.service_image('payment-stub'))
            values = payment_definition(name, service, {'repository': 'unresolved', 'tag': 'unresolved',
                                                        'digest': '', 'pullPolicy': 'IfNotPresent'})
            _pin_image(values['spec']['values']['image'], pinned)
            services[name] = values

        info('Fetching user journeys and fixtures; validating without executing user code')
        suite_name, suite_reference = platform.suite(definition.suite)
        info(f'Fetching test suite {definition.suite!r}: {suite_reference}')
        suite_ref, suite_bytes = registry.artifact(suite_reference, SUITE_CONFIG, SUITE_LAYER)
        suite, bundle = validate_bundle(json.loads(suite_bytes))
        if suite['metadata']['name'] != suite_name:
            raise ValueError(f'Published suite {suite_reference} is named {suite["metadata"]["name"]!r}')
        # The platform chooses the load generator image; suites contain only journeys and data.
        if 'image' in suite['spec']:
            raise ValueError('Remove spec.image from suite.yaml: the platform chooses the runner image')
        runtime_image = image('suiteRuntime', platform.runner_image)
        profile = parse_profile(definition.profile.document(), suite['spec']['journeys'])
        digest = bundle_digest(bundle)
        identities['scripts'] = {'requested': suite_reference, 'resolved': suite_ref,
                                 'contentSha256': checksum(suite_bytes), 'scriptBundleSha256': digest}

        # The namespace here is only a placeholder for validating Secret references and Helm values.
        namespace = 'preflight'
        outputs = {'database.host': 'database', 'database.port': 5432, 'database.database': definition.database.name,
            'database.user': 'generated-at-run-time',
            'database.password': SecretKeyRef(namespace, 'database-credentials', 'password'),
            'database.url': SecretKeyRef(namespace, 'database-credentials', 'url')}
        for name in services:
            outputs.update({f'services.{name}.host': name, f'services.{name}.port': 8080,
                            f'services.{name}.url': f'http://{name}:8080'})
        if set(ad['spec']['values'].get('secretEnv', {})) - set(definition.application.bindings):
            raise ValueError('Published AD Secret environment entries must have run-owned output bindings')
        values = bind_application_values(effective_ad, definition.application.bindings, outputs,
                                        namespace=namespace, run_id='preflight')
        info('Rendering application and service configuration; checking bindings and load settings')
        rendered = {'application': _helm(['template', 'application', str(chart), '-f', '-'], data=json.dumps(values))}
        for name, service in services.items():
            rendered[name] = _helm(['template', name, str(chart), '-f', '-'], data=json.dumps(service['spec']['values']))
        info('Checking every Pod against the platform resource limits')
        resource_plan = check_plan(plan_test(definition, rendered))
        info(f"Resource plan: {resource_plan['pods']} Pods, limits {resource_plan['totals']['limits']['cpu']} CPU / "
             f"{resource_plan['totals']['limits']['memory']} (namespace allows "
             f"{resource_plan['policy']['namespaceLimits']['cpu']} CPU / {resource_plan['policy']['namespaceLimits']['memory']})")

        suite_directory = directory / 'suite'
        suite_directory.mkdir(mode=0o700)
        for name, content in bundle.items():
            _write_bytes(suite_directory / name, content)
        # Preserve exact original suite bytes/hash; runtime image is pinned in the execution plan, not rewritten in source.
        _write_private(directory / 'application.json', json.dumps(effective_ad, indent=2))
        _write_private(directory / 'profile.json', json.dumps(profile.document(), indent=2))
        snapshot = {'apiVersion': 'perf-runner/v1alpha1', 'kind': 'ResolvedLoadTestDefinition',
            'resolvedAt': _now(), 'definition': definition.document, 'artifacts': identities,
            'application': effective_ad, 'services': services,
            'database': {'image': database_image, 'seedImage': seed_image},
            'suite': {'runtimeImage': runtime_image, 'scriptBundleSha256': digest, 'files': sorted(bundle),
                      'journeys': suite['spec']['journeys']},
            'execution': {'jobDeadlineSeconds': definition.job_deadline,
                          'requestTimeoutSeconds': definition.execution.request_timeout_seconds,
                          'resources': definition.execution.resources},
            'resourcePlan': resource_plan,
            'setupOrder': ['database-ready', 'seed-complete', 'services-ready', 'application-ready', 'suite'],
            'state': 'resolved'}
        _save(state_path, {'state': 'resolved', 'finishedAt': _now()})
        _save(directory / 'resolved.json', snapshot)
        return ResolvedDefinition(directory, snapshot, chart, suite_directory)
    except BaseException as error:
        # Keep only the error class in retained diagnostics; arbitrary registry/chart errors can contain secrets.
        _save(state_path, {'state': 'failed', 'errorType': type(error).__name__, 'finishedAt': _now()})
        raise


def _pin_image(image, reference):
    repository, digest = reference.split('@')
    image.update(repository=repository, digest=digest)
    # Chart schema requires a nonempty tag even though digest wins.
    image['tag'] = 'resolved'


def _write_bytes(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(content)


def registry_for(platform, http_registries=()):
    return registry_from_environment([*platform.http_registries, *http_registries])


def registry_from_environment(http_registries):
    # Explicit per-authority headers; never infer or retain Docker/login credentials.
    try:
        authorization = json.loads(os.environ.get('PERF_REGISTRY_AUTH', '{}'))
        if not isinstance(authorization, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                or not re.fullmatch(r'(Basic|Bearer) [A-Za-z0-9+/=._-]+', v) for k, v in authorization.items()):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Invalid PERF_REGISTRY_AUTH mapping') from None
    return Registry(http_registries=http_registries, authorization=authorization)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--definition', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New private resolution directory')
    parser.add_argument('--platform', type=Path, help='Platform config (default: PERF_PLATFORM or platform/config/local.yaml)')
    parser.add_argument('--http-registry', action='append', default=[])
    args = parser.parse_args()
    try:
        platform = load_platform(args.platform)
        result = resolve_definition(load_definition(args.definition), args.output, platform=platform,
                                    registry=registry_for(platform, args.http_registry))
        print(json.dumps({'state': 'resolved', 'snapshot': str(result.directory / 'resolved.json')}))
    except (Exception, KeyboardInterrupt) as error:
        # Do not echo raw untrusted definitions, authentication responses, or Helm error output.
        parser.exit(130 if isinstance(error, KeyboardInterrupt) else 1,
                    f'Artifact resolution failed ({type(error).__name__}); no workload was started\n')


if __name__ == '__main__':
    main()
