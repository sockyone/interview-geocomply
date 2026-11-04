"""Make the local OCI registry reachable from kind nodes without changing image identities.

Run references keep their published authority (for example ``localhost:5001``). Inside a
kind node that name is the node itself, so containerd is given an explicit host mapping
to the registry container on the kind Docker network. This changes only the named node's
registry configuration; the cluster and registry data are untouched. It is idempotent.
"""
import argparse
import json

from perf_runner.kubernetes import run_command


def docker(*args, input=None, check=True):
    result = run_command(['docker', *args], data=input, timeout=60)
    if check and result.returncode:
        raise RuntimeError(f'docker {args[0]} failed: {result.stderr.strip()[:300]}')
    return result


def hosts_toml(upstream):
    return f'server = "{upstream}"\n\n[host."{upstream}"]\n  capabilities = ["pull", "resolve"]\n'


def connect(*, node, registry_container, authority, network='kind', registry_port=5000):
    """Attach the registry to the kind network and map ``authority`` to it inside ``node``."""
    attached = json.loads(docker('inspect', registry_container, '--format', '{{json .NetworkSettings.Networks}}').stdout)
    if network not in attached:
        docker('network', 'connect', '--alias', registry_container, network, registry_container)
    upstream = f'http://{registry_container}:{registry_port}'
    directory = f'/etc/containerd/certs.d/{authority}'
    docker('exec', node, 'mkdir', '-p', directory)
    docker('exec', '-i', node, 'sh', '-c', f'cat > "{directory}/hosts.toml"', input=hosts_toml(upstream))
    return {'node': node, 'authority': authority, 'upstream': upstream}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', default='performance-test-control-plane')
    parser.add_argument('--registry-container', default='perf-artifacts-registry')
    parser.add_argument('--authority', default='localhost:5001')
    options = parser.parse_args()
    print(json.dumps(connect(node=options.node, registry_container=options.registry_container,
                             authority=options.authority), indent=2))


if __name__ == '__main__':
    main()
