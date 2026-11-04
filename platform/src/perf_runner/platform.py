"""Platform configuration: where named artifacts live and which platform artifacts to use.

Test authors refer to things by name (``application: {definition: checkout}``,
``suite: checkout``, ``image: checkout:0.3.0``). This module turns those names into
registry references. Resolution then pins each reference to an immutable digest.

Naming rules:

- ``name`` or ``name:version``: an ApplicationDefinition or TestSuite published under
  ``<registry>/applications/`` or ``<registry>/suites/``. Without a version the latest
  published one is used; the exact digest is still recorded for every run.
- A short image name (no registry host, e.g. ``checkout:0.3.0``) lives in the platform
  registry. A full reference (``ghcr.io/acme/checkout:1.4``) is used unchanged.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import re

import yaml

from perf_runner.registry import ArtifactRef

LATEST = 'latest'
NAME = r'[a-z][a-z0-9-]{0,62}'
VERSION = r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}'
NAMED = re.compile(rf'({NAME})(?::({VERSION}))?')
DEFAULT_PATH = Path('platform/config/local.yaml')


def parse_named(value, kind):
    """Return (name, version) for ``name`` or ``name:version``."""
    match = NAMED.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError(f'{kind} must be a name or name:version (for example checkout or checkout:0.3.0)')
    return match[1], match[2] or LATEST


def _has_registry_host(reference):
    first = reference.split('/', 1)[0]
    return '/' in reference and ('.' in first or ':' in first or first == 'localhost')


@dataclass(frozen=True)
class Platform:
    name: str
    registry: str
    plain_http: bool
    runner_image: str
    databases: dict
    services: dict
    kubeconfig: str | None = None
    context: str | None = None

    @property
    def authority(self):
        return self.registry.split('/', 1)[0]

    @property
    def http_registries(self):
        return [self.authority] if self.plain_http else []

    def image(self, reference):
        """Expand a short image name into the platform registry; keep full references."""
        if not isinstance(reference, str) or not reference:
            raise ValueError('Image reference is required')
        expanded = reference if _has_registry_host(reference) else f'{self.registry}/{reference}'
        return str(ArtifactRef.parse(expanded))

    def image_values(self, image):
        """Expand the repository of a chart ``image`` mapping (repository/tag/digest)."""
        expanded = dict(image)
        repository = image.get('repository')
        if not isinstance(repository, str) or not repository:
            raise ValueError('image.repository is required')
        if not _has_registry_host(repository):
            expanded['repository'] = f'{self.registry}/{repository}'
        return expanded

    def application(self, value):
        name, version = parse_named(value, 'application.definition')
        return name, str(ArtifactRef.parse(f'{self.registry}/applications/{name}:{version}'))

    def suite(self, value):
        name, version = parse_named(value, 'suite')
        return name, str(ArtifactRef.parse(f'{self.registry}/suites/{name}:{version}'))

    def chart(self, name, version):
        return str(ArtifactRef.parse(f'{self.registry}/charts/{name}:{version}'))

    def database_image(self, engine, version):
        try:
            return self.image(self.databases[engine][version])
        except KeyError:
            raise ValueError(f'The platform does not provide {engine} {version}') from None

    def service_image(self, service_type):
        try:
            return self.image(self.services[service_type])
        except KeyError:
            raise ValueError(f'The platform does not provide a {service_type} service') from None


def load_platform(path=None):
    path = Path(path or os.environ.get('PERF_PLATFORM') or DEFAULT_PATH)
    if not path.is_file():
        raise ValueError(f'Platform configuration not found: {path} (set --platform or PERF_PLATFORM)')
    document = yaml.safe_load(path.read_text())
    if (not isinstance(document, dict) or document.get('apiVersion') != 'perf-runner/v1alpha1'
            or document.get('kind') != 'Platform' or not isinstance(document.get('spec'), dict)):
        raise ValueError('Expected a perf-runner/v1alpha1 Platform document')
    spec = document['spec']
    required = {'registry', 'runnerImage', 'databases', 'services'}
    if required - set(spec) or set(spec) - required - {'plainHttp', 'cluster'}:
        raise ValueError('Platform spec needs registry, runnerImage, databases and services')
    cluster = spec.get('cluster') or {}
    if not isinstance(cluster, dict) or set(cluster) - {'kubeconfig', 'context'}:
        raise ValueError('Platform cluster accepts only kubeconfig and context')
    registry = spec['registry'].rstrip('/')
    ArtifactRef.parse(f'{registry}/probe:1')  # registry host + path must be valid
    platform = Platform(document.get('metadata', {}).get('name', path.stem), registry,
                        bool(spec.get('plainHttp', False)), '', spec['databases'], spec['services'])
    return Platform(platform.name, registry, platform.plain_http, platform.image(spec['runnerImage']),
                    spec['databases'], spec['services'], cluster.get('kubeconfig'), cluster.get('context'))
