#!/usr/bin/env python3
"""Lee la configuración central del repo que se está comprobando.

repos/_default.json  -> valores por defecto para todos los repos
repos/<nombre>.json  -> lo específico de un repo (opcional); solo lo cambian los administradores
Escribe las salidas en GITHUB_OUTPUT y las excepciones en un archivo para los guardarraíles.
"""
import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent / 'repos'
RULES = {'test-skip', 'test-only', 'suppression', 'coverage-ignore', 'assert-removed', 'test-deleted',
         'coverage-threshold', 'diff-coverage', 'lint', 'format', 'duplication', 'complexity', 'pr-size',
         'new-dependency', 'stub', 'policy-config', '*'}
QUALITY = {'duplication_max': (int, float), 'complexity_max': int, 'complexity_mode': str, 'format_required': bool,
           'pr_size_warn': int, 'pr_size_max': int, 'new_dependency_min_age_days': int}
KEYS = {'runner', 'node_version', 'python_version', 'coverage_min', 'smoke', 'commands', 'exceptions', 'release_type', 'quality', '$comment'}
RELEASE_TYPES = {'node', 'python', 'rust', 'simple', 'none'}
# Runners permitidos para build-test y smoke (los demás jobs no ejecutan el proyecto y siguen en Ubuntu)
RUNNERS = {'ubuntu-24.04', 'windows-2025'}


def load(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as error:
        sys.exit(f'::error title=Configuración de plataforma::{path.name} no es JSON válido: {error}')


def merge(base, extra):
    out = dict(base)
    for key, value in extra.items():
        out[key] = merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def main():
    repo = os.environ.get('GITHUB_REPOSITORY', '/').split('/')[-1]
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', repo):
        sys.exit('::error::Nombre de repositorio no válido')
    cfg = load(ROOT / '_default.json')
    own = ROOT / f'{repo}.json'
    if own.is_file():
        cfg = merge(cfg, load(own))
        print(f'Configuración: _default.json + {own.name}')
    else:
        print(f'Configuración: _default.json (no existe repos/{repo}.json)')
    unknown = set(cfg) - KEYS
    if unknown:
        sys.exit(f'::error title=Configuración de plataforma::Claves desconocidas en repos/{repo}.json: {", ".join(sorted(unknown))}')
    allow = pathlib.Path(os.environ.get('RUNNER_TEMP', '.')) / 'guardrails-allow'
    lines = []
    for exc in cfg.get('exceptions', []):
        if exc.get('rule') not in RULES or not exc.get('path') or not exc.get('reason'):
            sys.exit('::error title=Configuración de plataforma::Cada excepción necesita path, rule válida y reason')
        lines.append(f"{exc['path']}  {exc['rule']}  # {exc['reason']}")
    with open(allow, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join(lines) + '\n')
    if cfg.get('release_type', 'node') not in RELEASE_TYPES:
        sys.exit('::error title=Configuración de plataforma::release_type debe ser node, python, rust, simple o none')
    if cfg.get('runner', 'ubuntu-24.04') not in RUNNERS:
        sys.exit('::error title=Configuración de plataforma::runner debe ser ' + ' o '.join(sorted(RUNNERS)))
    quality = cfg.get('quality', {})
    for key, value in quality.items():
        if key not in QUALITY or not isinstance(value, QUALITY[key]) or isinstance(value, bool) != (QUALITY[key] is bool):
            sys.exit(f'::error title=Configuración de plataforma::quality.{key} no es válido')
    if quality.get('complexity_mode', 'warn') not in {'warn', 'block', 'off'}:
        sys.exit('::error title=Configuración de plataforma::quality.complexity_mode debe ser warn, block u off')
    smoke, cmds = cfg.get('smoke', {}), cfg.get('commands', {})
    out = {
        'runner': cfg.get('runner', 'ubuntu-24.04'),
        'node-version': str(cfg.get('node_version', '24')),
        'python-version': str(cfg.get('python_version', '3.13')),
        'coverage-min': str(float(cfg.get('coverage_min', 80))),
        'smoke-url': smoke.get('url', 'http://127.0.0.1:3000/health'),
        'smoke-start': smoke.get('start', ''),
        'smoke-port': str(smoke.get('port', '3000')),
        'cmd-install': cmds.get('install', ''),
        'cmd-lint': cmds.get('lint', ''),
        'cmd-build': cmds.get('build', ''),
        'cmd-test': cmds.get('test', ''),
        'allow-file': str(allow),
        'release-type': cfg.get('release_type', 'node'),
        'quality': json.dumps(quality, separators=(',', ':')),
    }
    # newline='\n': en un runner Windows el modo texto escribiría \r\n y el valor acabaría en \r
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8', newline='\n') as fh:
        for key, value in out.items():
            if '\n' in value:
                sys.exit(f'::error::{key} no puede tener saltos de línea')
            fh.write(f'{key}={value}\n')
    for key, value in out.items():
        print(f'  {key}: {value or "(automático)"}')


if __name__ == '__main__':
    main()
