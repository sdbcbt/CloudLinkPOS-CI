"""构建正式镜像的私有 GHCR 原始仓库，所有请求禁止自动跨域转发凭据。"""
import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

REPOSITORY = 'sdbcbt/cloudlinkpos'
IMAGE = 'ghcr.io/' + REPOSITORY
ACCEPT = 'application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def request(url, headers=None, data=None, method='GET'):
    headers = {'User-Agent': 'cloudlinkpos-registry/1.0', **(headers or {})}
    try:
        with urllib.request.build_opener(NoRedirect()).open(urllib.request.Request(url, headers=headers, data=data, method=method), timeout=60) as response:
            return response.status, dict(response.headers), response.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), b''


def token(anonymous=False):
    headers = {}
    if not anonymous:
        credentials = os.environ['GITHUB_ACTOR'] + ':' + os.environ['GITHUB_TOKEN']
        headers['Authorization'] = 'Basic ' + base64.b64encode(credentials.encode()).decode()
    query = urllib.parse.urlencode({'scope': 'repository:' + REPOSITORY + (':pull' if anonymous else ':pull,push'), 'service': 'ghcr.io'})
    status, _, raw = request('https://ghcr.io/token?' + query, headers)
    if anonymous and status in (401, 403):
        return None
    if status != 200:
        raise ValueError('GHCR_TOKEN_FAILED')
    result = json.loads(raw)
    return result.get('token') or result['access_token']


class Registry:
    repository = REPOSITORY

    def authenticated(self, path, data=None, method='GET', extra=None):
        headers = {'Authorization': 'Bearer ' + token(), 'Accept': ACCEPT}
        headers.update(extra or {})
        return request('https://ghcr.io' + path, headers, data, method)

    def check_private_existing(self):
        status, _, raw = request('https://api.github.com/orgs/sdbcbt/packages/container/cloudlinkpos', {'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'], 'Accept': 'application/vnd.github+json'})
        if status == 200 and json.loads(raw).get('visibility') == 'private':
            return
        # 新包默认私有，推送后还会按本次 digest 核验匿名拒绝。
        if status != 404:
            raise ValueError('GHCR_PACKAGE_NOT_PRIVATE')

    def reject_existing_tags(self, tags):
        for tag in tags:
            status, _, _ = self.authenticated('/v2/' + self.repository + '/manifests/' + tag)
            if status != 404:
                raise ValueError('GHCR_VERSION_EXISTS_OR_UNVERIFIED')

    def manifest(self, reference):
        status, _, raw = self.authenticated('/v2/' + self.repository + '/manifests/' + reference)
        if status != 200:
            raise ValueError('GHCR_MANIFEST_READ_FAILED')
        return raw

    def verify_image(self, reference, digest):
        raw = self.manifest(reference)
        if 'sha256:' + hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('GHCR_DIGEST_MISMATCH')
        manifest = json.loads(raw)
        if manifest.get('schemaVersion') != 2 or not isinstance(manifest.get('layers'), list):
            raise ValueError('GHCR_MANIFEST_INVALID')
        return raw

    def prove_private(self, digest):
        self.verify_image(digest, digest)
        anonymous = token(True)
        headers = {'Accept': ACCEPT}
        if anonymous:
            headers['Authorization'] = 'Bearer ' + anonymous
        status, _, _ = request('https://ghcr.io/v2/' + self.repository + '/manifests/' + digest, headers)
        if status not in (401, 403):
            raise ValueError('GHCR_ANONYMOUS_READ_NOT_DENIED')
        return {'authenticated_read': True, 'anonymous_pull_denied': True, 'image_digest': digest}

    def update_latest(self, service, digest):
        raw = self.verify_image(digest, digest)
        media = json.loads(raw)['mediaType']
        status, _, _ = self.authenticated('/v2/' + self.repository + '/manifests/' + service + '-latest', raw, 'PUT', {'Content-Type': media})
        if status not in (200, 201, 202):
            raise ValueError('GHCR_LATEST_WRITE_FAILED')
        self.verify_image(service + '-latest', digest)

