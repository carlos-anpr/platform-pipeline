#!/usr/bin/env python3
"""Repos a los que el workflow release-central.yml debe preparar el release.

Son los repos dados de alta (propiedad pipeline=agentica) que NO tienen archivo llamador:
en Enterprise Cloud el pipeline va como workflow requerido y no escucha los push a main.
Los repos con llamador (plan Team) ya publican sus releases desde el job "release".
Escribe en stdout:  matrix=[{"repo": "org/x", "release_type": "node"}, ...]
Variables: ORG, GH_TOKEN (RELEASE_PLEASE_TOKEN) y, opcional, ONLY (un solo repo).
"""
import json
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROPERTY, VALUE = 'pipeline', 'agentica'
CALLER = '.github/workflows/agentic-ci.yml'


def gh(path):
    out = subprocess.run(['gh', 'api', '--paginate', '--slurp', path], capture_output=True, text=True, encoding='utf-8')
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return json.loads(out.stdout)


def has_caller(org, repo):
    """True si el repo tiene archivo llamador; un error que no sea 404 aborta (no se adivina)."""
    out = subprocess.run(['gh', 'api', f'repos/{org}/{repo}/contents/{CALLER}'], capture_output=True, text=True)
    if out.returncode == 0:
        return True
    if 'HTTP 404' in out.stderr or 'Not Found' in out.stderr:
        return False
    raise RuntimeError(f'{repo}: {out.stderr.strip()}')


def account_type(org):
    out = subprocess.run(['gh', 'api', f'users/{org}', '--jq', '.type'], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return out.stdout.strip()


def release_type(repo):
    cfg = json.loads((ROOT / 'repos' / '_default.json').read_text(encoding='utf-8'))
    own = ROOT / 'repos' / f'{repo}.json'
    if own.is_file():
        cfg.update(json.loads(own.read_text(encoding='utf-8')))
    return cfg.get('release_type', 'node')


def enrolled(pages):
    """Nombres de repo con pipeline=agentica en la respuesta de /orgs/{org}/properties/values."""
    for page in pages:
        for item in page:
            props = {p['property_name']: p['value'] for p in item.get('properties', [])}
            if props.get(PROPERTY) == VALUE:
                yield item['repository_name']


def targets(org, names, caller=has_caller, rtype=release_type):
    out = []
    for name in sorted(set(names)):
        kind = rtype(name)
        if kind == 'none':
            print(f'{name}: release_type none, se omite', file=sys.stderr)
        elif caller(org, name):
            print(f'{name}: tiene archivo llamador (plan Team), publica él mismo', file=sys.stderr)
        else:
            out.append({'repo': f'{org}/{name}', 'release_type': kind})
    return out


def main():
    org, only = os.environ['ORG'], os.environ.get('ONLY', '').strip()
    if not re.fullmatch(r'[A-Za-z0-9-]+', org) or (only and not re.fullmatch(r'[A-Za-z0-9_.-]+', only)):
        sys.exit('::error::Organización o repo no válidos')
    try:
        if account_type(org) != 'Organization':
            print('::notice::Cuenta personal: cada repo publica sus releases con su archivo llamador.', file=sys.stderr)
            print('matrix=[]')
            return
        names = list(enrolled(gh(f'orgs/{org}/properties/values?per_page=100')))
        if only:
            names = [n for n in names if n == only]
        matrix = targets(org, names)
    except RuntimeError as error:   # token caducado, sin permiso, límite de la API… → rojo, no "nada que hacer"
        sys.exit(f'::error title=Releases centrales::{error}. Revisa RELEASE_PLEASE_TOKEN (Contents, Pull requests y '
                 '"Custom properties: read").')
    print('matrix=' + json.dumps(matrix, separators=(',', ':')))


if __name__ == '__main__':
    main()
