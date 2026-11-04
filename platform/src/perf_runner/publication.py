"""Publish named artifacts to the platform registry.

Team commands (run from a service's CI or laptop):

  application <ad.yaml>  --version V   -> <registry>/applications/<metadata.name>:V and :latest
  suite <suite-folder>   --version V   -> <registry>/suites/<metadata.name>:V and :latest

Platform command (run by DevOps when the platform changes):

  platform                              -> runner image, supported charts, database images

Sample helper:

  checkout --version V                  -> builds the sample Checkout image, then publishes its
                                           application definition and suite

Definitions then refer to ``checkout`` (latest) or ``checkout:V``; nobody edits references.
"""
import argparse
import json
from pathlib import Path
import shutil
import tempfile

import yaml

from perf_runner.application import validate_application, _helm
from perf_runner.definition import image_reference
from perf_runner.kubernetes import run_command
from perf_runner.platform import LATEST, load_platform, parse_named
from perf_runner.registry import AD_CONFIG, AD_LAYER, SUITE_CONFIG, SUITE_LAYER
from perf_runner.resolution import registry_for, validate_bundle, reject_literal_credentials
from perf_runner.suite_scripts import bundle_files

ROOT = Path(__file__).resolve().parents[3]  # repository root (source checkout)


def command(arguments, *, timeout=600):
    result = run_command(arguments, timeout=timeout)
    if result.returncode:
        # Registry credentials and upstream error bodies must not become retained diagnostics.
        raise RuntimeError(f'{arguments[0]} {arguments[1]} failed')
    return result.stdout


def _versions(version):
    parse_named('x:' + version, 'version')
    return [version] if version == LATEST else [version, LATEST]


def _publish_tags(name, version, locate, content, config, layer, registry):
    """Publish under name:version and name:latest; report the name, tags and digest."""
    tags = [locate(f'{name}:{tag}')[1] for tag in _versions(version)]
    pinned = {registry.publish(tag, content, config, layer) for tag in tags}
    return [{'name': name, 'tags': tags, 'digest': pinned.pop().split('@', 1)[1]}]


def publish_application(path, version, *, platform, registry):
    content = Path(path).read_bytes()
    definition = yaml.safe_load(content)
    validate_application(definition)
    reject_literal_credentials(definition)
    name = definition['metadata']['name']
    return _publish_tags(name, version, platform.application, content, AD_CONFIG, AD_LAYER, registry)


def publish_suite(directory, version, *, platform, registry):
    directory = Path(directory)
    _, data = bundle_files(directory / 'suite.yaml', directory)
    suite, _ = validate_bundle(data)
    if 'image' in suite['spec']:
        raise ValueError('Remove spec.image from suite.yaml: the platform chooses the runner image')
    content = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
    name = suite['metadata']['name']
    return _publish_tags(name, version, platform.suite, content, SUITE_CONFIG, SUITE_LAYER, registry)


def push_image(context, reference):
    command(['docker', 'build', '-t', reference, str(context)])
    command(['docker', 'push', reference])
    return reference


def publish_platform(*, platform, root=ROOT):
    """Runner image, supported charts and mirrored database images."""
    published = [push_image(root / 'platform/runner', platform.runner_image)]
    for engine, versions in platform.databases.items():
        for version in versions:
            target, upstream = platform.database_image(engine, version), f'{engine}:{version}'
            command(['docker', 'pull', upstream])
            command(['docker', 'tag', upstream, target])
            command(['docker', 'push', target])
            published.append(target)
    with tempfile.TemporaryDirectory(prefix='publish-charts-') as directory:
        for chart in sorted((root / 'platform/charts').iterdir()):
            if (chart / 'Chart.yaml').is_file():
                _helm(['package', str(chart), '-d', directory])
        for package in sorted(Path(directory).glob('*.tgz')):
            push = ['push', str(package), f'oci://{platform.registry}/charts']
            if platform.plain_http:
                push.append('--plain-http')
            _helm(push, timeout=120)
            published.append(f'{platform.registry}/charts/{package.stem}')
    return published


def publish_checkout(version, *, platform, registry, root=ROOT):
    """Sample helper: build the Checkout image named by its AD, then publish AD and suite."""
    ad_path = root / 'ApplicationDefinitions/checkout.yaml'
    ad = yaml.safe_load(ad_path.read_text())
    image = image_reference(platform.image_values(ad['spec']['values']['image']))
    published = [push_image(root / 'sample-service', image)]
    published += publish_application(ad_path, version, platform=platform, registry=registry)
    with tempfile.TemporaryDirectory(prefix='publish-suite-') as directory:
        bundle = Path(directory) / 'checkout'
        shutil.copytree(root / 'TestSuites/checkout', bundle, ignore=shutil.ignore_patterns('__pycache__'))
        published += publish_suite(bundle, version, platform=platform, registry=registry)
    return published


def main(argv=None):
    parser = argparse.ArgumentParser(prog='loadtest publish', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--platform', type=Path, help='Platform config (default: PERF_PLATFORM or platform/config/local.yaml)')
    commands = parser.add_subparsers(dest='action', required=True)
    application = commands.add_parser('application', help='Publish an ApplicationDefinition by its name')
    application.add_argument('definition', type=Path)
    suite = commands.add_parser('suite', help='Publish a TestSuite folder by its name')
    suite.add_argument('directory', type=Path)
    checkout = commands.add_parser('checkout', help='Build and publish the sample Checkout service and suite')
    for sub in (application, suite, checkout):
        sub.add_argument('--version', required=True)
    commands.add_parser('platform', help='Publish the runner image, charts and database images (platform team)')
    args = parser.parse_args(argv)
    try:
        platform = load_platform(args.platform)
        registry = registry_for(platform)
        if args.action == 'application':
            result = publish_application(args.definition, args.version, platform=platform, registry=registry)
        elif args.action == 'suite':
            result = publish_suite(args.directory, args.version, platform=platform, registry=registry)
        elif args.action == 'platform':
            result = publish_platform(platform=platform)
        else:
            result = publish_checkout(args.version, platform=platform, registry=registry)
        print(json.dumps({'published': result}, indent=2))
    except (Exception, KeyboardInterrupt) as error:
        # Only our own validation messages are shown; registry errors may echo credentials.
        detail = f': {error}' if type(error) is ValueError else ''
        parser.exit(130 if isinstance(error, KeyboardInterrupt) else 1,
                    f'Publication failed ({type(error).__name__}{detail}); no workload was started\n')


if __name__ == '__main__':
    main()
