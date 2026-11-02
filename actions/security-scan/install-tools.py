#!/usr/bin/env python3
"""Install reviewed Linux x86_64 release assets after SHA256 verification."""
import hashlib
import io
import json
import os
import pathlib
import tarfile
import urllib.request

destination = pathlib.Path(os.environ['RUNNER_TEMP']) / 'verified-tools'
destination.mkdir(exist_ok=True)
for tool in json.loads(pathlib.Path(__file__).with_name('tool-lock.json').read_text()):
    with urllib.request.urlopen(tool['url'], timeout=90) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != tool['sha256']:
        raise ValueError('Release asset checksum mismatch: '+tool['name'])
    if tool['url'].endswith('.tar.gz'):
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            entries = [m for m in archive.getmembers() if pathlib.PurePosixPath(m.name).name == tool['name'] and m.isfile()]
            if len(entries) != 1:
                raise ValueError('Expected exactly one regular executable')
            data = archive.extractfile(entries[0]).read()
    binary = destination / tool['name']
    binary.write_bytes(data)
    binary.chmod(0o755)
with open(os.environ['GITHUB_PATH'], 'a', encoding='utf-8') as pathfile:
    pathfile.write(str(destination)+'\n')
