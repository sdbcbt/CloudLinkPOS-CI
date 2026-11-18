"""读取本次完整构建的候选版本，串行同步，生成正式收据。"""
import json
import os
from pathlib import Path
import re
import sys
import importlib.util


def local_module(filename):
    spec = importlib.util.spec_from_file_location(filename, Path(__file__).with_name(filename + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_candidates(folder, env):
    names = {item['service'] for item in json.loads(env['EXPECTED_MATRIX'])['include']}
    candidates = {}
    for path in folder.glob('*.json'):
        item = json.loads(path.read_text())
        name, tag, digest = item['service'], item['tag'], item['digest']
        origin = item['origin']
        evidence = origin['privateEvidence']
        if (
            name not in names or name in candidates
            or item['schema'] != 'cloudlinkpos.public-full-build/v1'
            or item['sourceRepository'] != 'sdbcbt/CloudLinkPOS'
            or item['buildRepository'] != 'sdbcbt/CloudLinkPOS-CI'
            or item['sourceSHA'] != env['SOURCE_SHA']
            or item['workflowSHA'] != env['GITHUB_SHA']
            or item['runID'] != env['GITHUB_RUN_ID']
            or item['attempt'] != env['GITHUB_RUN_ATTEMPT']
            or item['repository'] != 'registry.cloudlinkpos.com/sdbcbt/cloudlinkpos'
            or not re.fullmatch(re.escape(name) + r'-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', tag)
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest)
            or origin['repository'] != 'ghcr.io/sdbcbt/cloudlinkpos' or origin['digest'] != digest
            or evidence['authenticated_read'] is not True
            or evidence['anonymous_pull_denied'] is not True
            or evidence['image_digest'] != digest
        ):
            raise ValueError('OCI_CANDIDATE_IDENTITY_INVALID')
        candidates[name] = item
    if not names or names != set(candidates):
        raise ValueError('OCI_CANDIDATE_BATCH_INCOMPLETE')
    return candidates


def main():
    registry = local_module('official-registry')
    build = local_module('full-build')
    origin = registry.SOURCE.PILOT.Registry()
    candidates = read_candidates(Path('candidates'), os.environ)
    def fleet_digests():
        return {item['digest'] for item in build.fresh_fleet_records() if item['repository'] == build.IMAGE}

    protected = set()
    registry.collect(protected, fleet_digests)
    for service, item in sorted(candidates.items()):
        tag, digest = item['tag'], item['digest']
        raw = origin.verify_image(tag, digest)
        try:
            item['privateEvidence'] = registry.publish(tag, digest, raw)
        except ValueError as error:
            if str(error) != 'OCI_CAPACITY_EXCEEDED':
                raise
            registry.collect(protected, fleet_digests, force_gc=True)
            item['privateEvidence'] = registry.publish(tag, digest, raw)
        protected.add(digest)
        print('官方 OCI 同步完成 ' + service + ' ' + tag, flush=True)
    registry.collect(protected, fleet_digests)
    for service, item in sorted(candidates.items()):
        tag, digest = item['tag'], item['digest']
        raw = origin.verify_image(tag, digest)
        registry.activate_latest(service, digest, raw, origin)
    work = Path('receipts')
    work.mkdir(exist_ok=True)
    for service, item in sorted(candidates.items()):
        (work / (service + '.json')).write_text(json.dumps(item, indent=2) + '\n')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        category = str(error) if isinstance(error, ValueError) and re.fullmatch('[A-Z_0-9]{3,80}', str(error)) else 'OCI_PUBLICATION_FAILED'
        print('::error::官方仓库同步停止 ' + category, file=sys.stderr)
        raise SystemExit(1)
