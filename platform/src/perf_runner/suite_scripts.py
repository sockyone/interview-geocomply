"""Publish a user's local Python flow bundle as an immutable run-owned ConfigMap."""
from dataclasses import dataclass, asdict
import argparse
import hashlib
import json
from pathlib import Path
import re

import yaml

from perf_runner.database import RUN_LABEL, KubernetesTarget, RunContext, _save
from perf_runner.kubernetes import Kubernetes
from suite_runtime.loader import validate_definition

# Keep authoring independent of the Locust runtime; it only packages UTF-8 files.
MAX_BUNDLE_BYTES = 900 * 1024


@dataclass(frozen=True)
class ScriptBundle:
    config_map: str
    namespace: str
    sha256: str
    files: tuple[str, ...]


def bundle_files(definition_path, scripts_directory):
    definition = yaml.safe_load(Path(definition_path).read_text())
    if not isinstance(definition, dict) or definition.get('apiVersion') != 'perf-runner/v1alpha1' or definition.get('kind') != 'TestSuite':
        raise ValueError('Unsupported TestSuite kind/version')
    validate_definition(definition)
    # Managed name: <suite name>-scripts-<content hash>; users never choose or see it.
    base_name = definition['metadata']['name'][:40].rstrip('-') + '-scripts'
    data = {'suite.yaml': Path(definition_path).read_text()}
    for path in Path(scripts_directory).iterdir():
        if path.name.startswith('.') or path.name == '__pycache__' or path.resolve() == Path(definition_path).resolve():
            continue
        if not path.is_file() or path.suffix not in {'.py', '.json', '.yaml', '.yml'} or path.name == 'suite.yaml':
            raise ValueError('Script bundle must contain flat Python/JSON/YAML files; suite.yaml is reserved')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,253}', path.name):
            raise ValueError('Unsupported ConfigMap file name')
        if path.stat().st_size > MAX_BUNDLE_BYTES:
            raise ValueError('Script bundle exceeds the 900 KiB ConfigMap budget')
        data[path.name] = path.read_text(encoding='utf-8')
    if not any(name.endswith('.py') for name in data):
        raise ValueError('Script bundle must contain at least one Python file')
    if sum(len(value.encode('utf-8')) for value in data.values()) > MAX_BUNDLE_BYTES:
        raise ValueError('Script bundle exceeds the 900 KiB ConfigMap budget')
    for entry in definition['spec']['journeys'].values():
        reference = entry['callable']
        if not re.fullmatch(r'[A-Za-z_]\w*:[A-Za-z_]\w*', reference) or reference.split(':')[0] + '.py' not in data:
            raise ValueError('Journey callable must refer to a Python module in the bundle')
    fixture = definition['spec'].get('fixtures')
    if fixture is not None and fixture not in data:
        raise ValueError('Fixture file is missing from the bundle')
    return base_name, data


def publish_suite_scripts(definition_path, scripts_directory, *, target, run):
    base_name, data = bundle_files(definition_path, scripts_directory)
    digest = hashlib.sha256()
    for name, value in sorted(data.items()):
        content = value.encode('utf-8')
        digest.update(name.encode() + b'\0' + len(content).to_bytes(8, 'big') + content)
    checksum = digest.hexdigest()
    name = base_name + '-' + checksum[:12]
    cluster = Kubernetes(target, run)
    cluster.assert_owned_namespace()
    manifest = {'apiVersion': 'v1', 'kind': 'ConfigMap',
        'metadata': {'name': name, 'namespace': run.namespace, 'labels': {RUN_LABEL: run.run_id},
                     'annotations': {'perf-runner/scripts-sha256': checksum}},
        'immutable': True, 'data': data}
    directory = run.artifacts_dir.resolve() / 'suite-scripts' / checksum
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _save(directory / 'configmap.json', manifest)
    existing = cluster.command('get', f'configmap/{name}', '--ignore-not-found', '-o', 'json')
    if existing:
        value = json.loads(existing)
        if (value['metadata'].get('labels', {}).get(RUN_LABEL) != run.run_id
                or value.get('data') != data or value.get('immutable') is not True):
            raise ValueError('Existing script ConfigMap is not this run-owned immutable bundle')
    else:
        cluster.command('create', '-f', '-', data=manifest)
    return ScriptBundle(name, run.namespace, checksum, tuple(sorted(data)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--definition', type=Path, required=True)
    parser.add_argument('--scripts', type=Path, required=True)
    parser.add_argument('--kubeconfig', type=Path, required=True)
    parser.add_argument('--context', required=True)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--artifacts', type=Path)
    args = parser.parse_args()
    try:
        value = publish_suite_scripts(args.definition, args.scripts,
            target=KubernetesTarget(args.kubeconfig, args.context),
            run=RunContext(args.run_id, args.namespace, args.artifacts or Path('.runtime/runs') / args.run_id))
        print(json.dumps(asdict(value), indent=2))
    except (ValueError, OSError, RuntimeError, KeyError, TypeError, AttributeError, yaml.YAMLError) as error:
        parser.exit(1, f'Could not publish script bundle: {error}\n')


if __name__ == '__main__':
    main()
