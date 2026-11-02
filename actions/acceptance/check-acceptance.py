#!/usr/bin/env python3
"""Punto 3 · Trazabilidad criterio → test.

Lee los criterios "- [ ] AC-n" del cuerpo del PR y de las issues enlazadas (Closes #N) como
DATOS (nunca se ejecutan) y comprueba que cada AC-n aparece en el NOMBRE de al menos un test:
título de it/test/describe (JS/TS), @DisplayName (Java/C#) o la función test_…ac1… (Python).
Un comentario "// AC-1" no cuenta: no demuestra que exista un test. Límite: no se comprueba que
ese test lo descubra el runner; que los tests que se ejecutan pasen lo garantiza build-test.

- PR de tipo feat o fix (título Conventional Commits): debe declarar al menos un criterio.
- Otros tipos (docs, chore, ci, refactor, test…): solo se comprueban los criterios que declaren.
Basado en templates/polyglot/scripts/acceptance_trace.py (misma lógica léxica).
Uso: check-acceptance.py [--body-file f] [--title t]   (sin args: lee GITHUB_EVENT_PATH)
"""
import argparse
import ast
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tokenize

AC = r'(?<![A-Za-z0-9_-])AC-[1-9][0-9]*(?![A-Za-z0-9_-])'


def criteria(text):
    return set(re.findall(r'^\s*-\s*\[[ xX]\]\s+(' + AC + r')', text, re.M))


def linked_issue_numbers(text, repository):
    numbers = set(map(int, re.findall(r'(?i)\b(?:spec:|closes|fixes|resolves)\s+#([1-9][0-9]*)\b', text)))
    numbers.update(map(int, re.findall(r'https://github\.com/' + re.escape(repository) + r'/issues/([1-9][0-9]*)\b', text)))
    if len(numbers) > 8:
        raise ValueError('Como máximo 8 issues enlazadas pueden definir criterios')
    return sorted(numbers)


def is_test(path):
    return bool(re.search(r'\.(py|[cm]?jsx?|tsx?|cs|java)$', path, re.I) and re.search(
        r'(^|/)(tests?|__tests__)(/|$)|(^|/)test_[^/]+\.py$|_test\.py$|\.(test|spec)\.[cm]?[jt]sx?$|Tests?\.(cs|java)$', path, re.I))


def slash_comments(text):
    """Comentarios // y /* */ y nombres de test; ignora el contenido de cadenas normales."""
    comments, names, i = [], [], 0
    while i < len(text):
        if text.startswith('//', i):
            end = text.find('\n', i)
            end = len(text) if end < 0 else end
            comments.append(text[i:end]); i = end
        elif text.startswith('/*', i):
            end = text.find('*/', i + 2)
            if end < 0:
                raise ValueError('Comentario sin cerrar en un test')
            comments.append(text[i:end + 2]); i = end + 2
        elif text[i] in ('"', "'", '`'):
            start, quote = i, text[i]
            delimiter = quote * 3 if text.startswith(quote * 3, i) else quote
            verbatim = i > 0 and text[i - 1] == '@'
            i += len(delimiter)
            while i < len(text):
                if not verbatim and text[i] == '\\':
                    i += 2; continue
                if verbatim and text.startswith('""', i):
                    i += 2; continue
                if text.startswith(delimiter, i):
                    if re.search(r'(?:\b(?:it|test|describe)\s*\(|@DisplayName\s*\(|DisplayName\s*=)\s*$', text[:start]):
                        names.append(text[start + len(delimiter):i])
                    i += len(delimiter); break
                i += 1
        else:
            i += 1
    return '\n'.join(comments), '\n'.join(names)


def test_references(files):
    result = {}
    for path, text in files.items():
        if not is_test(path):
            continue
        if path.endswith('.py'):
            try:
                tree = ast.parse(text)
            except SyntaxError as error:
                raise ValueError('No se puede leer el test: ' + path) from error
            defs = ' '.join(node.name for node in ast.walk(tree)
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test'))
            names = '\n'.join('AC-' + n for n in re.findall(r'(?i)(?<![a-z0-9])ac_?([1-9][0-9]*)(?![0-9])', defs))
        else:
            _, names = slash_comments(text)
        for marker in re.findall(AC, names):
            result.setdefault(marker, []).append(path)
    return {k: sorted(set(v)) for k, v in result.items()}


def trace(bodies, files):
    required = set().union(*(criteria(b) for b in bodies))
    refs = test_references(files)
    missing = required - refs.keys()
    if missing:
        raise ValueError('Criterios sin ningún test que los referencie: ' + ', '.join(sorted(missing, key=lambda x: int(x[3:]))))
    return {k: refs[k] for k in sorted(required)}


def requires_criteria(title):
    return bool(re.match(r'^(feat|fix)(\([^)]*\))?!?:', title or ''))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--body-file')
    ap.add_argument('--title')
    args = ap.parse_args()
    if args.body_file:
        body, title = pathlib.Path(args.body_file).read_text(encoding='utf-8'), args.title or ''
    else:
        event = json.loads(pathlib.Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
        if 'pull_request' not in event:
            print('No es un pull request: no aplica.')
            return 0
        body, title = event['pull_request'].get('body') or '', event['pull_request'].get('title') or ''
    repo = os.environ.get('GITHUB_REPOSITORY', 'local/local')
    if len(body.encode()) > 100_000:
        raise ValueError('Cuerpo del PR demasiado grande')
    bodies = [body]
    if not args.body_file:
        for number in linked_issue_numbers(body, repo):
            issue = json.loads(subprocess.check_output(['gh', 'api', f'repos/{repo}/issues/{number}'], text=True))
            if 'pull_request' in issue:
                raise ValueError(f'#{number} es un PR, no una issue: enlaza la issue con los criterios')
            bodies.append(issue.get('body') or '')
    if requires_criteria(title) and not any(criteria(b) for b in bodies):
        print('::error title=Criterios de aceptación::Un PR feat/fix debe declarar criterios "- [ ] AC-n: …" en su descripción '
              'o enlazar con "Closes #N" una issue que los tenga.')
        return 1
    files = {}
    root = pathlib.Path.cwd().resolve()
    for name in subprocess.check_output(['git', 'ls-files', '-z']).decode().split('\x00'):
        if not name or not is_test(name):
            continue
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
            continue
        files[name] = path.read_text(encoding='utf-8', errors='replace')
    try:
        result = trace(bodies, files)
    except ValueError as error:
        print(f'::error title=Criterios sin test::{error}. Nombra el test con el criterio, p. ej. test(\'AC-1: …\').')
        return 1
    if not result:
        print('El PR no declara criterios (no es feat/fix): no aplica.')
    for key, paths in result.items():
        print(f'{key} → {", ".join(paths)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
