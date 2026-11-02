#!/usr/bin/env python3
"""Punto 7 · Procedencia: qué PRs vienen de un agente y de cuál.

check  (solo lectura, dentro de ci-gate): el PR debe marcar en su plantilla qué agente se usó
       ("Ninguno" incluido) y la declaración no puede contradecir a los commits: si un commit
       lleva "Co-authored-by: Claude/Copilot/Codex…", no vale marcar "Ninguno".
label  (workflow aparte con permiso de escritura): pone etiquetas ai:<agente> para medir
       tasa de aceptación, retrabajo e incidencias por herramienta.
El texto del PR y de los commits se trata como datos: solo se compara con expresiones fijas.
"""
import json
import os
import pathlib
import re
import subprocess
import sys

OPTIONS = {  # texto de la casilla en la plantilla → etiqueta
    'ninguno': 'ai:ninguno', 'github copilot': 'ai:copilot', 'claude code': 'ai:claude-code', 'codex': 'ai:codex',
    'opencode': 'ai:opencode', 'cursor': 'ai:cursor', 'otro': 'ai:otro',
}
TRAILERS = [  # firma en commits → etiqueta
    ('ai:claude-code', r'(?im)^co-authored-by:.*(claude|noreply@anthropic\.com)|generated with \[?claude code'),
    ('ai:copilot', r'(?im)^co-authored-by:.*copilot'),
    ('ai:codex', r'(?im)^co-authored-by:.*codex'),
    ('ai:opencode', r'(?im)^co-authored-by:.*opencode'),
    ('ai:cursor', r'(?im)^co-authored-by:.*(cursor agent|cursoragent@cursor\.com)'),
    ('ai:otro', r'(?im)^co-authored-by:.*\b(gemini|devin|aider|jules|windsurf|cline|amazon q)\b'),
]
AGENT_LOGINS = {'copilot': 'ai:copilot', 'copilot-swe-agent[bot]': 'ai:copilot'}
COLORS = {'ai:ninguno': 'c5def5'}
EXEMPT_BOTS = {'dependabot[bot]', 'renovate[bot]'}  # actualizan dependencias; el resto de bots declara como todos
VERSION_LINE = re.compile(r'''^\s*(?:"(?:version|\.)"\s*:\s*"[^"]*",?|_{0,2}version_{0,2}\s*[:=]\s*["']?[\w.+-]*["']?,?'''
                          r'''|v?\d+\.\d+\.\d+[\w.+-]*)\s*$''', re.I)
RELEASE_FILES = re.compile(r'(^|/)(CHANGELOG\.md|\.release-please-manifest\.json|release-please-config\.json|package\.json'
                           r'|package-lock\.json|pyproject\.toml|setup\.py|setup\.cfg|version\.txt|_?version\.py|__init__\.py)$')


def gh(*args, data=None):
    out = subprocess.run(['gh', *args], input=data, capture_output=True, text=True, encoding='utf-8')
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or 'gh falló')
    return out.stdout


def declared(body):
    m = re.search(r'(?ims)^##\s*agente de ia[^\n]*\n(.*?)(?=^##\s|\Z)', body or '')
    if not m:
        return None
    marks = set()
    for label_text in re.findall(r'(?im)^\s*-\s*\[[xX]\]\s*([^\n<]+)', m.group(1)):
        key = label_text.strip().lower()
        for opt, lab in OPTIONS.items():
            if key.startswith(opt):
                marks.add(lab)
    return marks


def detected(commits):
    found = set()
    for c in commits:
        msg = (c.get('commit') or {}).get('message') or ''
        for lab, rx in TRAILERS:
            if re.search(rx, msg):
                found.add(lab)
        for who in (c.get('author') or {}, c.get('committer') or {}):
            login = (who.get('login') or '').lower()
            if login in AGENT_LOGINS:
                found.add(AGENT_LOGINS[login])
    return found


def context():
    event = json.loads(pathlib.Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    pr = event.get('pull_request')
    if not pr:
        return None, None, None
    repo = os.environ['GITHUB_REPOSITORY']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('GITHUB_REPOSITORY no válido')
    return repo, pr, event


def exempt(pr, files=None, in_main=False):
    """Exenciones que no dependen solo de un nombre de rama elegible por el autor."""
    user, head = pr.get('user') or {}, pr.get('head') or {}
    if user.get('type') == 'Bot' and (user.get('login') or '').lower() in EXEMPT_BOTS:
        return True
    same_repo = ((head.get('repo') or {}).get('full_name') == ((pr.get('base') or {}).get('repo') or {}).get('full_name'))
    ref = head.get('ref') or ''
    if not same_repo:
        return False
    if ref in ('main', 'develop'):              # ramas protegidas: solo cambian por PR
        return True
    if ref.startswith('release-please--'):      # PR de release: solo CHANGELOG y líneas de versión
        return bool(files) and all(release_only(f) for f in files)
    if ref.startswith('chore/back-merge'):      # back-merge: su contenido ya está en main
        return in_main
    return False


def release_only(change):
    """Un archivo del PR de release: CHANGELOG libre; en el resto, cada línea cambiada debe ser una versión."""
    name, patch = change.get('filename') or '', change.get('patch')
    if re.search(r'(^|/)CHANGELOG\.md$', name):
        return True
    if not RELEASE_FILES.search(name) or patch is None:
        return False
    lines = [ln[1:] for ln in patch.splitlines() if ln[:1] in '+-' and not ln.startswith(('+++', '---'))]
    return all(VERSION_LINE.match(ln) for ln in lines)


def evaluate(pr, commits, files=None, in_main=False):
    """Devuelve (errores, etiquetas)."""
    if exempt(pr, files, in_main):
        return [], set()
    marks, found = declared(pr.get('body')), detected(commits)
    login = ((pr.get('user') or {}).get('login') or '').lower()
    if login in AGENT_LOGINS:                   # PR abierto por un agente (p. ej. Copilot): se declara solo
        found.add(AGENT_LOGINS[login])
        if not marks:
            marks = {AGENT_LOGINS[login]}
    errors = []
    if marks is None or not marks:
        errors.append('Marca en la sección "Agente de IA" de la plantilla del PR qué agente usaste (o "Ninguno").')
        marks = set()
    if 'ai:ninguno' in marks and len(marks) > 1:
        errors.append('Has marcado "Ninguno" y también un agente: deja solo lo que corresponda.')
    if 'ai:ninguno' in marks and found:
        errors.append('Has marcado "Ninguno", pero hay commits firmados por un agente ('
                      + ', '.join(sorted(x[3:] for x in found)) + '). Corrige la declaración.')
    labels = (marks | found) - ({'ai:ninguno'} if found else set())
    return errors, labels or {'ai:sin-declarar'}


def commits_of(repo, number):
    pages = json.loads(gh('api', '--paginate', '--slurp', f'repos/{repo}/pulls/{int(number)}/commits?per_page=100'))
    return [c for page in pages for c in page]


def facts(repo, pr):
    """Datos para las exenciones: archivos del PR de release y si el back-merge ya está en main."""
    ref = (pr.get('head') or {}).get('ref') or ''
    files, in_main = None, False
    if ref.startswith('release-please--'):
        pages = json.loads(gh('api', '--paginate', '--slurp', f'repos/{repo}/pulls/{int(pr["number"])}/files?per_page=100'))
        files = [{'filename': f['filename'], 'patch': f.get('patch')} for page in pages for f in page]
    if ref.startswith('chore/back-merge'):
        sha = (pr.get('head') or {}).get('sha') or ''
        if re.fullmatch(r'[0-9a-f]{40}', sha):
            status = json.loads(gh('api', f'repos/{repo}/compare/main...{sha}')).get('status')
            in_main = status in ('behind', 'identical')
    return files, in_main


def cmd_check():
    repo, pr, _ = context()
    if not pr:
        print('No es un pull request: no aplica.')
        return 0
    errors, labels = evaluate(pr, commits_of(repo, pr['number']), *facts(repo, pr))
    for e in errors:
        print(f'::error title=Procedencia del PR::{e}')
    if not errors:
        print('Procedencia declarada: ' + (', '.join(sorted(labels)) or 'no aplica (bot o rama de release)'))
    return 1 if errors else 0


def cmd_label():
    repo, pr, _ = context()
    if not pr:
        return 0
    _, labels = evaluate(pr, commits_of(repo, pr['number']), *facts(repo, pr))
    if not labels:
        return 0
    number = int(pr['number'])
    current = {x['name'] for x in json.loads(gh('api', f'repos/{repo}/issues/{number}/labels'))}
    for name in sorted(labels - current):
        try:
            gh('api', f'repos/{repo}/labels', '--method', 'POST', '--input', '-',
               data=json.dumps({'name': name, 'color': COLORS.get(name, 'd4c5f9'), 'description': 'Procedencia del cambio (agente de IA)'}))
        except RuntimeError:
            pass  # ya existe
    for stale in sorted(x for x in current if x.startswith('ai:') and x not in labels):
        gh('api', f'repos/{repo}/issues/{number}/labels/{stale}', '--method', 'DELETE')
    if labels - current:
        gh('api', f'repos/{repo}/issues/{number}/labels', '--method', 'POST', '--input', '-',
           data=json.dumps({'labels': sorted(labels - current)}))
    print('Etiquetas: ' + ', '.join(sorted(labels)))
    return 0


if __name__ == '__main__':
    sys.exit({'check': cmd_check, 'label': cmd_label}[sys.argv[1]]())
