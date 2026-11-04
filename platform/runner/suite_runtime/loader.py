"""Load user flow functions from a flat, immutable ConfigMap/file bundle."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import importlib
import importlib.abc
import importlib.util
import json
import inspect
from pathlib import Path
import re
import sys
import tempfile
from types import ModuleType

import yaml

MAX_BUNDLE_BYTES = 900 * 1024


def read_bundle(definition_path):
    """Read projected ConfigMap symlinks as files; skip Kubernetes' hidden entries."""
    definition_path = Path(definition_path).absolute()
    # Preserve the mount root even when the definition is a projected symlink.
    root = Path(definition_path).parent
    files = {}
    for path in root.iterdir():
        if path.name.startswith('.') or not path.is_file():
            continue
        if path.suffix not in {'.py', '.json', '.yaml', '.yml'}:
            raise ValueError('Suite bundle supports flat Python, JSON, and YAML files only')
        if path.stat().st_size > MAX_BUNDLE_BYTES:
            raise ValueError('Suite bundle exceeds the 900 KiB ConfigMap budget')
        files[path.name] = path.read_bytes()
    if definition_path.name not in files:
        raise ValueError('Suite definition is not in the script bundle')
    if sum(len(value) for value in files.values()) > MAX_BUNDLE_BYTES:
        raise ValueError('Suite bundle exceeds the 900 KiB ConfigMap budget')
    return files


def bundle_digest(files):
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode() + b'\0' + len(content).to_bytes(8, 'big') + content)
    return digest.hexdigest()


def validate_definition(definition):
    if not isinstance(definition, dict) or definition.get('apiVersion') != 'perf-runner/v1alpha1' or definition.get('kind') != 'TestSuite':
        raise ValueError('Unsupported TestSuite kind/version')
    name = definition.get('metadata', {}).get('name')
    if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', name):
        raise ValueError('TestSuite metadata.name must be a DNS label')
    spec = definition.get('spec')
    if isinstance(spec, dict) and 'scripts' in spec:
        raise ValueError('Remove spec.scripts: the runner names and manages the script ConfigMap')
    if not isinstance(spec, dict) or set(spec) - {'image', 'fixtures', 'journeys'}:
        raise ValueError('Unknown TestSuite setting')
    if 'image' in spec and (not isinstance(spec['image'], str) or not spec['image']):
        raise ValueError('TestSuite image must be a nonempty string when set')
    if 'fixtures' in spec and (not isinstance(spec['fixtures'], str) or Path(spec['fixtures']).name != spec['fixtures'] or not spec['fixtures'].endswith('.json')):
        raise ValueError('fixtures must name a JSON file in the bundle')
    journeys = spec.get('journeys')
    if not isinstance(journeys, dict) or not journeys:
        raise ValueError('TestSuite journeys must be a nonempty mapping')
    for journey, entry in journeys.items():
        if not isinstance(journey, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', journey):
            raise ValueError('Invalid journey name')
        if not isinstance(entry, dict) or set(entry) - {'callable', 'description'} or not isinstance(entry.get('callable'), str):
            raise ValueError('Journey requires callable: module:function')
        if not re.fullmatch(r'[A-Za-z_]\w*:[A-Za-z_]\w*', entry['callable']):
            raise ValueError('Journey callable must be module:function (flat bundle)')
    return definition


class SourceLoader(importlib.abc.SourceLoader):
    def __init__(self, path):
        self.path = path
    def get_filename(self, fullname):
        return str(self.path)
    def get_data(self, path):
        return Path(path).read_bytes()
    def get_code(self, fullname):
        # User-written assert statements remain active even under python -O.
        return compile(self.path.read_bytes(), str(self.path), 'exec', optimize=0)


class BundleFinder(importlib.abc.MetaPathFinder):
    def __init__(self, package, root):
        self.package, self.root = package, root
    def find_spec(self, fullname, path=None, target=None):
        prefix = self.package + '.'
        if fullname.startswith(prefix):
            name = fullname[len(prefix):]
            if '.' not in name and re.fullmatch(r'[A-Za-z_]\w*', name):
                source = self.root / (name + '.py')
                if source.is_file():
                    return importlib.util.spec_from_loader(fullname, SourceLoader(source))
        return None


@dataclass(frozen=True)
class LoadedSuite:
    name: str
    journeys: dict
    descriptions: dict
    fixture: dict
    digest: str
    references: dict


@contextmanager
def load_suite(definition_path, *, fixture_override=None):
    original = Path(definition_path).absolute()
    files = read_bundle(original)
    definition = validate_definition(yaml.safe_load(files[original.name]))
    digest = bundle_digest(files)
    if fixture_override:
        fixture_bytes = Path(fixture_override).read_bytes()
        fixture = json.loads(fixture_bytes)
    else:
        fixture_name = definition['spec'].get('fixtures')
        fixture = json.loads(files[fixture_name]) if fixture_name else {}
    if not isinstance(fixture, dict):
        raise ValueError('Fixture must be a JSON object')
    package_name = '_user_suite_' + digest[:20]
    with tempfile.TemporaryDirectory(prefix='suite-snapshot-') as directory:
        snapshot = Path(directory)
        for name, content in files.items():
            (snapshot / name).write_bytes(content)
        package = ModuleType(package_name)
        package.__path__ = [str(snapshot)]
        finder = BundleFinder(package_name, snapshot)
        sys.modules[package_name] = package
        sys.meta_path.insert(0, finder)
        try:
            functions, descriptions, references = {}, {}, {}
            for name, entry in definition['spec']['journeys'].items():
                module, function = entry['callable'].split(':')
                if module + '.py' not in files:
                    raise ValueError(f'Missing journey module: {module}.py')
                loaded = importlib.import_module(package_name + '.' + module)
                value = getattr(loaded, function, None)
                if not inspect.isfunction(value) or inspect.iscoroutinefunction(value) or inspect.isgeneratorfunction(value):
                    raise ValueError(f'Journey function not found: {entry["callable"]}')
                inspect.signature(value).bind(object())
                functions[name], descriptions[name], references[name] = value, entry.get('description', ''), entry['callable']
            yield LoadedSuite(definition['metadata']['name'], functions, descriptions, fixture, digest, references)
        finally:
            sys.meta_path.remove(finder)
            for name in list(sys.modules):
                if name == package_name or name.startswith(package_name + '.'):
                    del sys.modules[name]
