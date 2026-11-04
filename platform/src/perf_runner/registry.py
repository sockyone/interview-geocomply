"""Bounded OCI Distribution client for manifests and small definition artifacts.

Image filesystems remain with Docker/Kubernetes. Credentials are caller-owned,
never serialized, and redirects cannot forward them to another origin.
"""
from dataclasses import dataclass
import hashlib
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
IMAGE_MANIFESTS = {MANIFEST, 'application/vnd.docker.distribution.manifest.v2+json'}
INDEXES = {'application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json'}
HELM_CONFIG = 'application/vnd.cncf.helm.config.v1+json'
HELM_LAYER = 'application/vnd.cncf.helm.chart.content.v1.tar+gzip'
AD_CONFIG = 'application/vnd.perf-runner.application-definition.v1+json'
AD_LAYER = 'application/vnd.perf-runner.application-definition.v1+yaml'
SUITE_CONFIG = 'application/vnd.perf-runner.suite-bundle.v1+json'
SUITE_LAYER = 'application/vnd.perf-runner.suite-bundle.v1+json'
DIGEST = r'sha256:[a-f0-9]{64}'


def checksum(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class ArtifactRef:
    registry: str
    repository: str
    selector: str

    @classmethod
    def parse(cls, value):
        if not isinstance(value, str) or len(value) > 512:
            raise ValueError('Expected fully qualified registry artifact reference')
        match = re.fullmatch(r'([a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?)/([a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*)(?:@(' + DIGEST + r')|:([A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))', value)
        if not match or not ('.' in match[1] or ':' in match[1] or match[1] == 'localhost'):
            raise ValueError('Use registry/repository:version or registry/repository@sha256:digest; no local paths/URLs')
        if urlsplit('https://' + match[1]).port == 0:
            raise ValueError('Registry port must be positive')
        return cls(match[1], match[2], match[3] or match[4])

    def pinned(self, digest):
        if not re.fullmatch(DIGEST, digest):
            raise ValueError('Expected SHA-256 digest')
        return f'{self.registry}/{self.repository}@{digest}'

    def __str__(self):
        return f'{self.registry}/{self.repository}' + ('@' if self.selector.startswith('sha256:') else ':') + self.selector


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Blob redirects need an explicit transport extension, not credential forwarding.
        return None


class Registry:
    def __init__(self, *, http_registries=(), authorization=None):
        # Plain HTTP is only for an explicitly selected loopback development registry.
        self.http = set(http_registries)
        for authority in self.http:
            if urlsplit('http://' + authority).hostname not in {'localhost', '127.0.0.1'}:
                raise ValueError('Plain HTTP is restricted to explicit loopback registries')
        self._authorization = dict(authorization or {})
        self.opener = build_opener(NoRedirect())

    def origin(self, ref):
        return ('http://' if ref.registry in self.http else 'https://') + ref.registry

    def request(self, ref, path, *, method='GET', data=None, content_type=None, maximum=4 * 1024 * 1024):
        origin = self.origin(ref)
        url = urljoin(origin, path)
        if urlsplit(url).netloc != ref.registry or urlsplit(url).scheme != urlsplit(origin).scheme:
            raise ValueError('Registry upload location changed origin')
        headers = {'Accept': ', '.join(sorted(IMAGE_MANIFESTS | INDEXES))}
        if ref.registry in self._authorization:
            headers['Authorization'] = self._authorization[ref.registry]
        if content_type:
            headers['Content-Type'] = content_type
        try:
            with self.opener.open(Request(url, data=data, headers=headers, method=method), timeout=30) as response:
                body = response.read(maximum + 1)
                if len(body) > maximum:
                    raise ValueError('Registry response exceeded size limit')
                return body, response.headers
        except HTTPError as error:
            # Never retain response bodies, headers, credentials, or signed URLs.
            raise RuntimeError(f'Registry request failed with HTTP {error.code} for {ref.registry}/{ref.repository}') from None
        except (URLError, TimeoutError, OSError):
            raise RuntimeError(f'Registry unavailable: {ref.registry}') from None

    def manifest(self, reference):
        ref = ArtifactRef.parse(reference)
        body, headers = self.request(ref, f'/v2/{ref.repository}/manifests/{ref.selector}')
        digest = checksum(body)
        if ref.selector.startswith('sha256:') and digest != ref.selector:
            raise ValueError('Manifest content digest mismatch')
        advertised = headers.get('Docker-Content-Digest')
        if advertised and advertised != digest:
            raise ValueError('Registry manifest header digest mismatch')
        value = json.loads(body)
        if isinstance(value, dict) and 'mediaType' not in value:
            # Helm may omit the optional body field; digest verification above uses the original bytes.
            value['mediaType'] = headers.get('Content-Type', '').split(';')[0]
        if not isinstance(value, dict) or value.get('schemaVersion') != 2 or value.get('mediaType') not in IMAGE_MANIFESTS | INDEXES:
            raise ValueError('Unsupported OCI manifest')
        return ref, digest, value

    def blob(self, ref, descriptor, *, maximum=4 * 1024 * 1024):
        if (not isinstance(descriptor, dict) or not re.fullmatch(DIGEST, str(descriptor.get('digest', '')))
                or type(descriptor.get('size')) is not int or not 0 <= descriptor['size'] <= maximum):
            raise ValueError('Invalid or oversized OCI blob descriptor')
        body, _ = self.request(ref, f'/v2/{ref.repository}/blobs/{descriptor["digest"]}', maximum=maximum)
        if checksum(body) != descriptor['digest'] or len(body) != descriptor['size']:
            raise ValueError('OCI blob size/digest mismatch')
        return body

    def artifact(self, reference, config_type, layer_type):
        ref, digest, manifest = self.manifest(reference)
        if (manifest.get('mediaType') not in IMAGE_MANIFESTS or manifest.get('config', {}).get('mediaType') != config_type
                or not isinstance(manifest.get('layers'), list) or len(manifest['layers']) != 1
                or manifest['layers'][0].get('mediaType') != layer_type):
            raise ValueError('OCI artifact has unexpected configuration/layer type')
        self.blob(ref, manifest['config'], maximum=65536)
        return ref.pinned(digest), self.blob(ref, manifest['layers'][0])

    def image(self, reference, *, _indexed=False):
        ref, digest, manifest = self.manifest(reference)
        if manifest['mediaType'] in INDEXES:
            if _indexed:
                raise ValueError('Nested image indexes are unsupported')
            children = manifest.get('manifests')
            if not isinstance(children, list) or not children or len(children) > 64:
                raise ValueError('Invalid image index')
            for child in children:
                if child.get('mediaType') not in IMAGE_MANIFESTS or not re.fullmatch(DIGEST, str(child.get('digest', ''))):
                    raise ValueError('Unsupported image index entry')
                self.image(ref.pinned(child['digest']), _indexed=True)
        elif manifest.get('config', {}).get('mediaType') not in {
            'application/vnd.oci.image.config.v1+json', 'application/vnd.docker.container.image.v1+json'}:
            raise ValueError('Expected container image, not a definition/chart artifact')
        else:
            self.blob(ref, manifest['config'], maximum=1024 * 1024)
            if not isinstance(manifest.get('layers'), list) or not manifest['layers']:
                raise ValueError('Container image has no filesystem layers')
            if len(manifest['layers']) > 512:
                raise ValueError('Image has too many filesystem layers')
            for layer in manifest['layers']:
                if (not isinstance(layer, dict) or not re.fullmatch(DIGEST, str(layer.get('digest', '')))
                        or type(layer.get('size')) is not int or not 0 <= layer['size'] <= 32 * 1024**3
                        or layer.get('urls')):
                    raise ValueError('Unsupported image layer descriptor')
                _, headers = self.request(ref, f'/v2/{ref.repository}/blobs/{layer["digest"]}', method='HEAD', maximum=0)
                if headers.get('Content-Length') and int(headers['Content-Length']) != layer['size']:
                    raise ValueError('Image layer size mismatch')
        return ref.pinned(digest)

    def publish(self, reference, body, config_type, layer_type, *, config=b'{}'):
        ref = ArtifactRef.parse(reference)
        if ref.selector.startswith('sha256:'):
            raise ValueError('Publish using an explicit version tag')
        layers = []
        for content, media in ((config, config_type), (body, layer_type)):
            digest = checksum(content)
            _, headers = self.request(ref, f'/v2/{ref.repository}/blobs/uploads/', method='POST', data=b'')
            location = headers.get('Location')
            if not location:
                raise ValueError('Registry upload did not return a location')
            separator = '&' if '?' in location else '?'
            self.request(ref, location + separator + 'digest=' + digest, method='PUT', data=content,
                         content_type='application/octet-stream')
            layers.append({'mediaType': media, 'digest': digest, 'size': len(content)})
        manifest = json.dumps({'schemaVersion': 2, 'mediaType': MANIFEST,
            'config': layers[0], 'layers': [layers[1]]}, sort_keys=True, separators=(',', ':')).encode()
        self.request(ref, f'/v2/{ref.repository}/manifests/{ref.selector}', method='PUT', data=manifest, content_type=MANIFEST)
        return ref.pinned(checksum(manifest))
