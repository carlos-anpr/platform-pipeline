#!/usr/bin/env python3
"""Administración del pipeline central. Solo para administradores de la organización.

  python scripts/platform_admin.py configure --org MI-ORG
      Sustituye TU-ORG por tu organización en este repo (una vez, antes del primer push).
  python scripts/platform_admin.py bootstrap --org MI-ORG [--enterprise] [--apply]
      Propiedad "pipeline", acceso de Actions al repo central, reglas del repo central y
      reglas de organización (develop, main, bloqueo de .github/workflows y, en Enterprise,
      el workflow requerido).
  python scripts/platform_admin.py enroll --org MI-ORG --repo NOMBRE [--enterprise] [--no-develop] [--apply]
      Da de alta un repo de código. Si no existe, lo crea vacío. Si está vacío, el archivo llamador
      (y AGENTS.md) entran como primer commit, sin PR, y se crea develop como rama por defecto.
      Después: propiedad pipeline=agentica, bloqueo de secretos al hacer push y etiquetas ai:*.
      Solo un repo que YA tiene código recibe el llamador por PR (lo mergea el admin).

Modo prueba gratis (--personal): --org es TU USUARIO y los repos son públicos de tu cuenta.
No hay reglas de organización ni equipos: enroll pone reglas de rama en el propio repo (con
--approvals aprobaciones, 0 por defecto porque no puedes aprobar tus propios PRs) y añade la
plantilla de PR en el mismo PR del archivo llamador. Sirve para ver el flujo, no para un equipo.

Sin --apply solo muestra lo que haría. Requiere `gh auth login` (owner de la org, o tu cuenta).
"""
import argparse
import base64
import copy
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLATFORM_REPO = 'platform-pipeline'
PROPERTY = 'pipeline'
LABELS = ['ai:ninguno', 'ai:copilot', 'ai:claude-code', 'ai:codex', 'ai:opencode', 'ai:cursor', 'ai:otro', 'ai:sin-declarar']


class Admin:
    def __init__(self, org, apply):
        if not re.fullmatch(r'[A-Za-z0-9-]+', org):
            sys.exit('Nombre de organización no válido')
        self.org, self.apply = org, apply

    def gh(self, *args, data=None, read=False):
        """read=True: siempre se ejecuta (consulta). Si no, solo con --apply."""
        cmd = ['gh', 'api', *args]
        if data is not None:
            cmd += ['--input', '-']
        if not read and not self.apply:
            print('   [simulación] gh api ' + ' '.join(args) + (f'  <- {json.dumps(data, ensure_ascii=False)[:160]}…' if data else ''))
            return None
        out = subprocess.run(cmd, input=json.dumps(data) if data is not None else None,
                             capture_output=True, text=True, encoding='utf-8')
        if out.returncode != 0:
            raise RuntimeError((out.stderr or out.stdout).strip())
        return json.loads(out.stdout) if out.stdout.strip() else None

    def exists(self, path):
        """Consulta que tolera el 404: True si existe, False si no."""
        try:
            return self.gh(path, read=True) is not None
        except RuntimeError as error:
            if '404' in str(error) or 'Not Found' in str(error):
                return False
            raise

    def file(self, repo, path, ref):
        """(sha, texto) de un archivo en una rama, o (None, None) si no existe."""
        try:
            got = self.gh(f'repos/{self.org}/{repo}/contents/{path}?ref={ref}', read=True)
        except RuntimeError as error:
            if '404' in str(error) or 'Not Found' in str(error):
                return None, None
            raise
        return got['sha'], base64.b64decode(got.get('content', '')).decode('utf-8', 'replace')

    def team_id(self, slug):
        try:
            return self.gh(f'orgs/{self.org}/teams/{slug}', read=True)['id']
        except RuntimeError:
            sys.exit(f'Falta el equipo @{self.org}/{slug}. Créalo (Organización → Teams) con permiso Write en los repos.')

    def render(self, rules, repo_id=None):
        """Quita las claves de documentación ($comment, $team) y rellena los IDs reales."""
        rules = copy.deepcopy(rules)
        rules.pop('$comment', None)
        for rule in rules.get('rules', []):
            for rev in rule.get('parameters', {}).get('required_reviewers', []):
                rev['reviewer']['id'] = self.team_id(rev.pop('$team'))
            for wf in rule.get('parameters', {}).get('workflows', []):
                wf['repository_id'] = repo_id
        return rules

    def upsert_ruleset(self, scope, rules):
        try:
            existing = self.gh(f'{scope}/rulesets?per_page=100', read=True) or []
        except RuntimeError as error:   # simulación sobre un repo que aún no existe
            if self.apply or not ('404' in str(error) or 'Not Found' in str(error)):
                raise
            existing = []
        match = next((r for r in existing if r['name'] == rules['name']), None)
        if match:
            print(f' - {rules["name"]}: existe, se actualiza')
            self.gh(f'{scope}/rulesets/{match["id"]}', '--method', 'PUT', data=rules)
        else:
            print(f' - {rules["name"]}: se crea')
            self.gh(f'{scope}/rulesets', '--method', 'POST', data=rules)


def personal_ruleset(org_rules, approvals):
    """Convierte una regla de organización en regla de repo para una cuenta personal:
    sin condición de propiedad, sin revisores por equipo y con N aprobaciones."""
    rules = copy.deepcopy(org_rules)
    rules.pop('$comment', None)
    rules['conditions'] = {'ref_name': rules['conditions']['ref_name']}
    for rule in rules['rules']:
        params = rule.get('parameters', {})
        params.pop('required_reviewers', None)
        if rule['type'] == 'pull_request':
            params['required_approving_review_count'] = approvals
            # "Aprobación del último push" exige a otra persona: con 0 aprobaciones bloquearía todo merge
            params['require_last_push_approval'] = approvals > 0 and params.get('require_last_push_approval', False)
    rules['bypass_actors'] = personal_bypass(rules.get('bypass_actors', []))
    return rules


def personal_bypass(actors):
    """OrganizationAdmin no existe en una cuenta personal: lo sustituye el rol admin del repo (RepositoryRole 5)."""
    return [{'actor_id': 5, 'actor_type': 'RepositoryRole', 'bypass_mode': a.get('bypass_mode', 'always')}
            if a.get('actor_type') == 'OrganizationAdmin' else a for a in actors]


def personal_platform_rules(own):
    """Reglas del repo central en una cuenta personal."""
    branch, tags = copy.deepcopy(own['branch']), copy.deepcopy(own['tags'])
    tags['bypass_actors'] = personal_bypass(tags['bypass_actors'])
    return branch, tags


def load(name):
    return json.loads((ROOT / 'rulesets' / name).read_text(encoding='utf-8'))


def cmd_configure(args):
    changed = 0
    for path in ROOT.rglob('*'):
        if path.is_file() and path.suffix in {'.yml', '.yaml', '.json', '.md', '.sh', '.py'} and '.git' not in path.parts:
            text = path.read_text(encoding='utf-8')
            if 'TU-ORG' in text and path.name != 'platform_admin.py':
                path.write_text(text.replace('TU-ORG', args.org), encoding='utf-8')
                changed += 1
                print(f' - {path.relative_to(ROOT)}')
    print(f'{changed} archivo(s) actualizados con la organización {args.org}.')


def cmd_bootstrap(args):
    a = Admin(args.org, args.apply)
    print(f'Organización: {a.org}   {"APLICANDO" if a.apply else "simulación (añade --apply)"}')
    repo = a.gh(f'repos/{a.org}/{PLATFORM_REPO}', read=True)
    tag = None
    try:
        tag = a.gh(f'repos/{a.org}/{PLATFORM_REPO}/git/ref/tags/v1', read=True)
    except RuntimeError:
        pass
    if repo['visibility'] == 'public' and not args.personal:   # en modo prueba es público a propósito
        print('AVISO: el repo de plataforma es público. Hazlo privado (o interno).')
    if args.personal:
        print('\nModo prueba (--personal): sin reglas de organización.')
        if repo['visibility'] != 'public':
            print('\n1) Compartir el repo central (privado) con tus otros repos')
            a.gh(f'repos/{a.org}/{PLATFORM_REPO}/actions/permissions/access', '--method', 'PUT', data={'access_level': 'user'})
        print('\n2) Reglas del propio repo central')
        for rules in personal_platform_rules(load('platform-repo.json')):
            a.upsert_ruleset(f'repos/{a.org}/{PLATFORM_REPO}', a.render(rules))
        if not tag:
            print('\nAVISO: crea la etiqueta v1:  git tag v1 && git push origin v1')
        return
    print('\n1) Propiedad de repo "pipeline" (marca qué repos usan el pipeline central)')
    a.gh(f'orgs/{a.org}/properties/schema/{PROPERTY}', '--method', 'PUT',
         data={'value_type': 'single_select', 'allowed_values': ['agentica'], 'required': False,
               'description': 'Repos que usan el pipeline central (platform-pipeline)'})
    print('\n2) El repo central comparte su workflow y sus acciones con los repos privados de la organización')
    a.gh(f'repos/{a.org}/{PLATFORM_REPO}/actions/permissions/access', '--method', 'PUT', data={'access_level': 'organization'})
    print('\n3) Reglas del propio repo central (main por PR con self-test; etiquetas v* solo admins)')
    own = load('platform-repo.json')
    a.upsert_ruleset(f'repos/{a.org}/{PLATFORM_REPO}', a.render(own['branch']))
    a.upsert_ruleset(f'repos/{a.org}/{PLATFORM_REPO}', a.render(own['tags']))
    print('\n4) Reglas de organización para los repos con pipeline=agentica')
    for name in ['org-develop.json', 'org-main.json', 'org-push-workflows.json']:
        a.upsert_ruleset(f'orgs/{a.org}', a.render(load(name)))
    if args.enterprise:
        a.upsert_ruleset(f'orgs/{a.org}', a.render(load('org-required-workflow.json'), repo_id=repo['id']))
    if not tag:
        print('\nAVISO: aún no existe la etiqueta v1 en el repo central. Créala tras el primer merge en main:'
              '\n   git tag v1 && git push origin v1   (ver README, "Publicar una versión")')
    print('\nPendiente a mano: equipos tech-leads y platform; revisión de Copilot en las reglas (opcional);'
          '\nrepo público TU-ORG/.github con org-dot-github/ (plantillas de PR e issue).')


def platform_files(org, personal):
    """Archivos de plataforma que lleva todo repo de código: (ruta, texto, mensaje del commit)."""
    content = (ROOT / 'caller' / 'agentic-ci.yml').read_text(encoding='utf-8')
    if 'TU-ORG' in content:
        sys.exit('Ejecuta antes: platform_admin.py configure --org ' + org)
    files = [('.github/workflows/agentic-ci.yml', content, 'ci: activa el pipeline central de plataforma')]
    if personal:   # en una organización la plantilla de PR viene del repo TU-ORG/.github
        template = (ROOT / 'org-dot-github' / '.github' / 'pull_request_template.md').read_text(encoding='utf-8')
        files.append(('.github/pull_request_template.md', template, 'docs: plantilla de PR del pipeline'))
    return files


def create_repo(a, name, personal):
    """Crea el repo vacío (sin README: el primer commit lo pone plataforma). Personal: público; organización: privado."""
    if personal:
        login = a.gh('user', read=True)['login']
        if login.lower() != a.org.lower():
            sys.exit(f'Con --personal, --org debe ser tu usuario ({login}).')
        path, data = 'user/repos', {'name': name, 'private': False, 'auto_init': False}
    else:
        path, data = f'orgs/{a.org}/repos', {'name': name, 'visibility': 'private', 'auto_init': False}
    created = a.gh(path, '--method', 'POST', data={**data, 'delete_branch_on_merge': True})
    return created or {'visibility': 'public' if personal else 'private', 'default_branch': 'main'}


def seed_empty_repo(a, name, default, files, develop):
    """Repo vacío: los archivos de plataforma entran como primer commit directo (sin PR, sin código del
    proyecto) ANTES de activar las reglas, que después exigirían PR. Opcionalmente crea develop."""
    for path, text, message in files:
        a.gh(f'repos/{a.org}/{name}/contents/{path}', '--method', 'PUT', data={
            # sin 'branch': en un repo vacío el primer archivo crea la rama por defecto
            'message': message, 'content': base64.b64encode(text.encode()).decode()})
    if not develop:
        return default
    if a.apply:
        sha = a.gh(f'repos/{a.org}/{name}/git/ref/heads/{default}', read=True)['object']['sha']
    else:
        sha = '<sha del primer commit>'
    a.gh(f'repos/{a.org}/{name}/git/refs', '--method', 'POST', data={'ref': 'refs/heads/develop', 'sha': sha})
    a.gh(f'repos/{a.org}/{name}', '--method', 'PATCH', data={'default_branch': 'develop'})
    return 'develop'


def cmd_enroll(args):
    a = Admin(args.org, args.apply)
    name = args.repo
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
        sys.exit('Nombre de repo no válido')
    files = platform_files(a.org, args.personal)
    mode = "APLICANDO" if a.apply else "simulación (añade --apply)"
    if a.exists(f'repos/{a.org}/{name}'):
        target = a.gh(f'repos/{a.org}/{name}', read=True)
        print(f'Repo: {a.org}/{name} ({target["visibility"]})   {mode}')
        empty = not a.gh(f'repos/{a.org}/{name}/branches?per_page=1', read=True)
    else:
        print(f'Repo: {a.org}/{name} (no existe)   {mode}')
        print('\n0) Crear el repo vacío')
        target, empty = create_repo(a, name, args.personal), True
    default = target['default_branch']
    if empty and not args.enterprise:
        print(f'\n0b) Repo vacío: primer commit con los archivos de plataforma en {default} (sin PR)'
              + (' y rama develop por defecto' if not args.no_develop else ''))
        seed = files + [('AGENTS.md', (ROOT / 'caller' / 'AGENTS.md').read_text(encoding='utf-8'),
                         'docs: AGENTS.md inicial para agentes de IA')]
        default = seed_empty_repo(a, name, default, seed, develop=not args.no_develop)
    if args.personal:
        print(f'\n1) Reglas de rama en el propio repo (modo prueba, {args.approvals} aprobación(es))')
        for f in ('org-develop.json', 'org-main.json'):
            a.upsert_ruleset(f'repos/{a.org}/{name}', personal_ruleset(load(f), args.approvals))
    else:
        print('\n1) Propiedad pipeline=agentica (activa las reglas de la organización en este repo)')
        a.gh(f'orgs/{a.org}/properties/values', '--method', 'PATCH',
             data={'repository_names': [name], 'properties': [{'property_name': PROPERTY, 'value': 'agentica'}]})
    print('\n1b) Permitir auto-merge (se mergea solo al estar aprobado y en verde)')
    a.gh(f'repos/{a.org}/{name}', '--method', 'PATCH', data={'allow_auto_merge': True, 'delete_branch_on_merge': True})
    print('\n2) Bloqueo de secretos al hacer push')
    try:
        a.gh(f'repos/{a.org}/{name}', '--method', 'PATCH', data={'security_and_analysis': {
            'secret_scanning': {'status': 'enabled'}, 'secret_scanning_push_protection': {'status': 'enabled'}}})
    except RuntimeError as error:
        print(f'   No se pudo activar ({error}). En repos privados requiere GitHub Secret Protection.')
    print('\n3) Etiquetas de procedencia')
    for label in LABELS:
        try:
            a.gh(f'repos/{a.org}/{name}/labels', '--method', 'POST',
                 data={'name': label, 'color': 'd4c5f9', 'description': 'Procedencia del cambio (agente de IA)'})
        except RuntimeError:
            pass  # ya existe
    cfg = ROOT / 'repos' / f'{name}.json'
    print(f'\n4) Configuración del repo: {"repos/" + cfg.name if cfg.is_file() else "se usará repos/_default.json"}')
    if args.enterprise:
        print('\n5) Enterprise: no hace falta archivo en el repo (lo exige el ruleset plataforma-pipeline-requerido).')
        return
    if empty:
        print(f'\n5) Archivo llamador: ya está en {default} desde el primer commit.')
        print(f'   Listo. Los desarrolladores clonan, crean su rama desde {default} y abren su PR: el pipeline salta solo.')
        return
    print('\n5) PR con el archivo llamador (.github/workflows/agentic-ci.yml): el repo ya tiene código')
    branch = 'platform/agentic-ci'
    # Se puede repetir sin miedo: reutiliza rama, archivos y PR si ya existen
    if all(a.file(name, path, default)[1] == text for path, text, _ in files):
        print(f'   Ya instalado en {default}: no hace falta PR.')
        return finish(default)
    if a.exists(f'repos/{a.org}/{name}/git/ref/heads/{branch}'):
        print(f'   La rama {branch} ya existe: se reutiliza.')
    else:
        base_sha = a.gh(f'repos/{a.org}/{name}/git/ref/heads/{default}', read=True)['object']['sha']
        a.gh(f'repos/{a.org}/{name}/git/refs', '--method', 'POST', data={'ref': f'refs/heads/{branch}', 'sha': base_sha})
    for path, text, message in files:
        sha, current = a.file(name, path, branch)
        if current == text:
            print(f'   {path}: ya está al día en la rama.')
            continue
        data = {'message': message, 'branch': branch, 'content': base64.b64encode(text.encode()).decode()}
        if sha:
            data['sha'] = sha
        a.gh(f'repos/{a.org}/{name}/contents/{path}', '--method', 'PUT', data=data)
    open_prs = a.gh(f'repos/{a.org}/{name}/pulls?state=open&head={a.org}:{branch}', read=True) or []
    if open_prs:
        print(f'   Ya hay un PR abierto: {open_prs[0]["html_url"]}')
        return finish(default)
    pr = a.gh(f'repos/{a.org}/{name}/pulls', '--method', 'POST', data={
        'title': 'ci: activa el pipeline central de plataforma', 'head': branch, 'base': default,
        'body': ('Añade el archivo llamador gestionado por plataforma. Toda la lógica y la configuración '
                 f'están en {a.org}/{PLATFORM_REPO}.\n\n## Agente de IA usado\n- [x] Ninguno\n')})
    if pr:
        print(f'   PR abierto: {pr["html_url"]}. Cuando ci-gate esté en verde, mergéalo (como admin).')
    finish(default)


def finish(default):
    if default != 'develop':
        print(f'   Nota: la rama por defecto es {default}. Si usáis develop, crea la rama y hazla la rama por defecto.')


def main():
    for stream in (sys.stdout, sys.stderr):     # tildes legibles en la consola de Windows (cp1252)
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('configure'); c.add_argument('--org', required=True)
    for n in ('bootstrap', 'enroll'):
        p = sub.add_parser(n)
        p.add_argument('--org', required=True)
        p.add_argument('--enterprise', action='store_true', help='GitHub Enterprise Cloud: workflow requerido, sin archivo llamador')
        p.add_argument('--personal', action='store_true', help='Prueba gratis con tu cuenta personal y repos públicos')
        p.add_argument('--approvals', type=int, default=0, help='Aprobaciones requeridas en modo --personal (0 por defecto)')
        p.add_argument('--apply', action='store_true')
        if n == 'enroll':
            p.add_argument('--repo', required=True)
            p.add_argument('--no-develop', action='store_true',
                           help='Repo nuevo: trabajar solo con main (por defecto se crea develop como rama por defecto)')
    args = ap.parse_args()
    try:
        {'configure': cmd_configure, 'bootstrap': cmd_bootstrap, 'enroll': cmd_enroll}[args.cmd](args)
    except RuntimeError as error:
        sys.exit(f'Error de la API de GitHub: {error}')


if __name__ == '__main__':
    main()
