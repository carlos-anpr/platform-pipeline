#!/usr/bin/env python3
"""Job `quality` del pipeline central: 7 reglas de calidad sobre lo que cambia el PR.

  1 lint          linter obligatorio: el del proyecto o, si no tiene, Biome (JS/TS) o Ruff (Python)
  2 format        formato: si el proyecto tiene formateador, el código cambiado debe estar formateado
  3 duplication   duplicación de las líneas cambiadas (jscpd) <= duplication_max %
  4 complexity    complejidad ciclomática por función tocada (lizard) <= complexity_max (aviso por defecto)
  5 pr-size       tamaño del PR: aviso > pr_size_warn líneas, bloqueo > pr_size_max
  6 new-dependency dependencias nuevas: deben existir en el registro y tener >= N días
  7 stub          funciones sin implementar en código de producción (bloquea); TODO/FIXME (aviso)

Todo se aplica SOLO a lo que cambia el PR. Umbrales: repos/<repo>.json -> "quality".
Excepciones: repos/<repo>.json -> "exceptions" con estas reglas (path = ruta o, en
new-dependency, nombre del paquete). Uso: check-quality.py --base <sha> --allow-file f --config '<json>'
"""
import argparse
import csv
import datetime as dt
import fnmatch
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request

BIOME = '2.5.15'
JSCPD = '5.4.1'
DEFAULTS = {'duplication_max': 3, 'complexity_max': 15, 'complexity_mode': 'warn', 'format_required': False,
            'pr_size_warn': 400, 'pr_size_max': 1500, 'new_dependency_min_age_days': 30}
CODE = re.compile(r'\.(?:[cm]?[jt]sx?|py|java|kt|cs|go|rb|php|swift|scala|rs|c|cc|cpp|h|hpp)$', re.I)
JS = re.compile(r'\.[cm]?[jt]sx?$', re.I)
FMT_JS = re.compile(r'\.(?:[cm]?[jt]sx?|json|jsonc|css|scss)$', re.I)
PY = re.compile(r'\.py$', re.I)
TEST = re.compile(r'(^|/)(tests?|__tests__|specs?)(/|$)|\.(test|spec)\.[cm]?[jt]sx?$|(^|/)test_[^/]+\.py$|_test\.(py|go)$|Tests?\.(cs|java|kt)$', re.I)
GENERATED = re.compile(r'(^|/)(node_modules|dist|build|coverage|vendor|\.next|out|__snapshots__|\.github)/'
                       r'|\.min\.(js|css)$|\.snap$|(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|uv\.lock|'
                       r'Pipfile\.lock|Cargo\.lock|go\.sum|composer\.lock)$', re.I)
DOCS = re.compile(r'\.(md|markdown|txt|rst|adoc)$', re.I)
STUB_BLOCK = re.compile(
    r'raise\s+NotImplementedError|throw\s+new\s+Error\(\s*[\'"`]\s*(not\s+implemented|unimplemented|todo)\b'
    r'|throw\s+new\s+NotImplementedException|UnsupportedOperationException\(\s*"(not\s+implemented|todo)'
    r'|\btodo!\(|\bunimplemented!\(', re.I)
STUB_WARN = re.compile(r'\b(TODO|FIXME|XXX|HACK)\b')
JSCPD_IGNORE = ('**/node_modules/**,**/dist/**,**/build/**,**/coverage/**,**/vendor/**,**/.git/**,'
                '**/test/**,**/tests/**,**/__tests__/**,**/*.test.*,**/*.spec.*,**/test_*.py,**/*_test.py,**/*.min.js')


# ───────────────────────── utilidades ─────────────────────────
def git(*args):
    # core.quotePath=false: git no escapa los acentos de las rutas (src/café.py)
    return subprocess.run(['git', '-c', 'core.quotePath=false', *args], check=True, capture_output=True, text=True,
                          encoding='utf-8', errors='replace').stdout


def git_path(raw):
    """Ruta de una cabecera de git: quita la tabulación final y decodifica las comillas de C ("caf\\303\\251.py")."""
    raw = raw.rstrip('\n').rstrip('\t')
    if len(raw) < 2 or raw[0] != '"' or raw[-1] != '"':
        return raw
    out, i, body = bytearray(), 0, raw[1:-1]
    simple = {'n': 10, 't': 9, 'r': 13, 'a': 7, 'b': 8, 'f': 12, 'v': 11, '"': 34, '\\': 92}
    while i < len(body):
        ch = body[i]
        if ch == '\\' and i + 1 < len(body):
            nxt = body[i + 1]
            octal = body[i + 1:i + 4]
            if len(octal) == 3 and all(c in '01234567' for c in octal):
                out.append(int(body[i + 1:i + 4], 8))
                i += 4
                continue
            out.append(simple.get(nxt, ord(nxt)))
            i += 2
            continue
        out += ch.encode('utf-8')
        i += 1
    return out.decode('utf-8', 'replace')


def b_path(target):
    """Ruta destino de una línea '+++ ': None si es /dev/null."""
    t = git_path(target)
    if t == '/dev/null':
        return None
    return t[2:] if t.startswith('b/') else t


def load_allow(path):
    rules = []
    p = pathlib.Path(path) if path else None
    if p and p.is_file():
        for raw in p.read_text(encoding='utf-8').splitlines():
            parts = raw.split('#', 1)[0].split()
            if parts:
                rules.append((parts[0], parts[1] if len(parts) > 1 else '*'))
    return rules


def allowed(allow, name, rule):
    return any(fnmatch.fnmatch(name, g) and r in ('*', rule) for g, r in allow)


def changed_lines(base, head='HEAD'):
    """{ruta: set(líneas añadidas o modificadas)} de los archivos que siguen existiendo."""
    out, cur = {}, None
    for line in git('diff', '--no-color', '--unified=0', '-M', '--diff-filter=AMR', f'{base}...{head}').splitlines():
        if line.startswith('+++ '):
            cur = b_path(line[4:])
            if cur:
                out.setdefault(cur, set())
        elif line.startswith('@@') and cur:
            m = re.match(r'@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@', line)
            start, count = int(m.group(1)), int(m.group(2) or '1')
            out[cur].update(range(start, start + count))
    return out


def added_text(base, head='HEAD'):
    """{ruta: [(línea, texto)]} de las líneas añadidas."""
    out, cur, n = {}, None, 0
    header = False
    for line in git('diff', '--no-color', '--unified=0', '-M', '--diff-filter=AMR', f'{base}...{head}').splitlines():
        if line.startswith('diff --git '):
            cur, header = None, True
        elif header and line.startswith('+++ '):
            cur = b_path(line[4:])
        elif line.startswith('@@'):
            header = False
            m = re.match(r'@@ -\d+(?:,\d+)? \+(\d+)', line)
            n = int(m.group(1))
        elif cur and line.startswith('+') and not line.startswith('+++'):
            out.setdefault(cur, []).append((n, line[1:]))
            n += 1
    return out


def norm_path(p):
    p = p.replace('\\\\?\\', '').replace('\\', '/')
    cwd = str(pathlib.Path.cwd()).replace('\\', '/')
    if p.lower().startswith(cwd.lower() + '/'):
        p = p[len(cwd) + 1:]
    return p[2:] if p.startswith('./') else p


def is_prod_code(path):
    return bool(CODE.search(path)) and not TEST.search(path) and not GENERATED.search(path)


class Report:
    def __init__(self):
        self.rows, self.blocking = [], False

    def note(self, check, status, msg):
        self.rows.append((check, status, msg))
        if status == 'ERROR':
            self.blocking = True

    @staticmethod
    def annotate(level, title, msg, path=None, line=None):
        props = ([f'file={path}'] if path else []) + ([f'line={line}'] if path and line else []) + [f'title=Calidad · {title}']
        print(f'::{level} {",".join(props)}::{msg}')


def run(cmd, shell=False):
    printable = cmd if isinstance(cmd, str) else ' '.join(cmd)
    print(f'+ {printable[:300]}')
    # Registro público fijo: un .npmrc del PR no puede redirigir la descarga de las herramientas centrales
    env = dict(os.environ, npm_config_ignore_scripts='true', npm_config_registry='https://registry.npmjs.org/')
    return subprocess.run(cmd, shell=shell, env=env).returncode


def tool(name):
    bindir = os.environ.get('QUALITY_BIN', '')
    for cand in (os.path.join(bindir, name), os.path.join(bindir, name + '.exe')):
        if bindir and os.path.isfile(cand):
            return cand
    return shutil.which(name) or name


def pkg_json():
    try:
        return json.loads(pathlib.Path('package.json').read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}


# ───────────────────────── 1 · lint ─────────────────────────
def check_lint(rep, changed, allow, stack, cmd_lint):
    files = sorted(f for f in changed if CODE.search(f) and not GENERATED.search(f) and not allowed(allow, f, 'lint'))
    if cmd_lint:
        rc = run(cmd_lint, shell=True)
        return rep.note('lint', 'OK' if rc == 0 else 'ERROR', f'linter del proyecto (repos/<repo>.json): {"sin errores" if rc == 0 else "con errores"}')
    if stack == 'node':
        if (pkg_json().get('scripts') or {}).get('lint'):
            rc = run(['npm', 'run', 'lint'])
            return rep.note('lint', 'OK' if rc == 0 else 'ERROR', f'npm run lint: {"sin errores" if rc == 0 else "con errores"}')
        js = [f for f in files if JS.search(f)]
        if not js:
            return rep.note('lint', 'N/A', 'no hay archivos JS/TS cambiados')
        rc = run(['npx', '--yes', f'@biomejs/biome@{BIOME}', 'lint', '--no-errors-on-unmatched', *js])
        return rep.note('lint', 'OK' if rc == 0 else 'ERROR',
                        f'el proyecto no tiene script "lint": Biome (reglas recomendadas) sobre {len(js)} archivo(s) cambiado(s)')
    if stack == 'python':
        py = [f for f in files if PY.search(f)]
        if not py:
            return rep.note('lint', 'N/A', 'no hay archivos Python cambiados')
        rc = run([tool('ruff'), 'check', *py])
        return rep.note('lint', 'OK' if rc == 0 else 'ERROR', f'Ruff sobre {len(py)} archivo(s) cambiado(s)')
    if files:
        rep.annotate('error', 'linter', 'Hay código que no es Node ni Python y no hay linter configurado: define commands.lint en repos/<repo>.json')
        return rep.note('lint', 'ERROR', 'falta commands.lint para este tipo de proyecto')
    return rep.note('lint', 'N/A', 'no hay código cambiado')


# ───────────────────────── 2 · format ─────────────────────────
def formatter(stack):
    root = pathlib.Path('.')
    if stack == 'node':
        pj = pkg_json()
        deps = {**(pj.get('dependencies') or {}), **(pj.get('devDependencies') or {})}
        if 'prettier' in pj or 'prettier' in deps or any(root.glob('.prettierrc*')) or any(root.glob('prettier.config.*')):
            return 'prettier'
        if (root / 'biome.json').is_file() or (root / 'biome.jsonc').is_file():
            return 'biome'
    if stack == 'python':
        pyproject = root / 'pyproject.toml'
        text = pyproject.read_text(encoding='utf-8') if pyproject.is_file() else ''
        if '[tool.ruff' in text or (root / 'ruff.toml').is_file() or (root / '.ruff.toml').is_file():
            return 'ruff'
        if '[tool.black' in text:
            return 'black'
    return None


def check_format(rep, changed, allow, stack, required):
    kind = formatter(stack)
    pattern = PY if stack == 'python' else FMT_JS
    files = sorted(f for f in changed if pattern.search(f) and not GENERATED.search(f) and not allowed(allow, f, 'format'))
    if not files:
        return rep.note('format', 'N/A', 'no hay archivos formateables cambiados')
    if not kind:
        msg = ('el proyecto no tiene formateador configurado (Prettier o Biome en Node; Ruff o Black en Python). '
               'Añadidlo para que todo el equipo, y sus agentes, formateen igual: es configuración de calidad, así que '
               'el PR necesita una excepción policy-config de plataforma.')
        rep.annotate('error' if required else 'warning', 'formato', msg)
        return rep.note('format', 'ERROR' if required else 'AVISO', 'sin formateador configurado')
    cmd = {'prettier': ['npx', '--no-install', 'prettier', '--check', *files],
           'biome': ['npx', '--yes', f'@biomejs/biome@{BIOME}', 'format', *files],
           'ruff': [tool('ruff'), 'format', '--check', *files],
           'black': [tool('black'), '--check', *files]}[kind]
    rc = run(cmd)
    if rc != 0:
        rep.annotate('error', 'formato', f'Hay archivos cambiados sin formatear con {kind}. Ejecuta el formateador y vuelve a subir el cambio.')
    return rep.note('format', 'OK' if rc == 0 else 'ERROR', f'{kind} sobre {len(files)} archivo(s) cambiado(s)')


# ───────────────────────── 3 · duplication ─────────────────────────
def duplication(report, changed, allow):
    """Devuelve (porcentaje, líneas duplicadas, líneas de código cambiadas, clones tocados)."""
    code = {f: ls for f, ls in changed.items() if is_prod_code(f) and not allowed(allow, f, 'duplication')}
    total = sum(len(ls) for ls in code.values())
    dup, clones = {}, []
    for d in report.get('duplicates', []):
        sides = [(norm_path(d['firstFile']['name']), d['firstFile']['start'], d['firstFile']['end']),
                 (norm_path(d['secondFile']['name']), d['secondFile']['start'], d['secondFile']['end'])]
        touched = False
        for path, start, end in sides:
            hit = {n for n in code.get(path, ()) if start <= n <= end}
            if hit:
                dup.setdefault(path, set()).update(hit)
                touched = True
        if touched:
            clones.append((sides, d.get('lines', 0)))
    dup_lines = sum(len(v) for v in dup.values())
    pct = 100.0 * dup_lines / total if total else 0.0
    return pct, dup_lines, total, clones


def check_duplication(rep, changed, allow, limit):
    if not any(is_prod_code(f) for f in changed):
        return rep.note('duplication', 'N/A', 'no hay código de producción cambiado')
    out = pathlib.Path(os.environ.get('RUNNER_TEMP', '.')) / 'jscpd'
    central = pathlib.Path(__file__).resolve().parent / 'jscpd.json'   # ignora .jscpd.json y la clave jscpd del repo
    rc = run(['npx', '--yes', f'jscpd@{JSCPD}', '--config', str(central), '--no-gitignore',
              '--reporters', 'json', '--output', str(out), '--silent', '--absolute',
              '--min-lines', '5', '--min-tokens', '50', '--ignore', JSCPD_IGNORE, '.'])
    reportfile = out / 'jscpd-report.json'
    if rc != 0 or not reportfile.is_file():
        return rep.note('duplication', 'ERROR', 'no se pudo ejecutar jscpd (falla en cerrado)')
    pct, lines, total, clones = duplication(json.loads(reportfile.read_text(encoding='utf-8')), changed, allow)
    level = 'error' if pct > limit else 'warning'
    for (a, b), n in clones[:20]:
        rep.annotate(level, 'duplicación', f'Bloque de {n} líneas duplicado: {a[0]}:{a[1]}-{a[2]} y {b[0]}:{b[1]}-{b[2]}. '
                     'Extrae una función común en lugar de copiar.', a[0], a[1])
    status = 'ERROR' if pct > limit else 'OK'
    return rep.note('duplication', status, f'{pct:.1f} % de {total} líneas de código cambiadas están duplicadas (máximo {limit:g} %)')


# ───────────────────────── 4 · complexity ─────────────────────────
def complexity(csv_text, changed, limit):
    """Funciones tocadas por el PR con complejidad > limit: [(ruta, inicio, fin, nombre, ccn)]."""
    out = []
    for row in csv.reader(io.StringIO(csv_text)):
        if len(row) < 11 or not row[1].isdigit():
            continue
        ccn, path, name, start, end = int(row[1]), norm_path(row[6]), row[7], int(row[9]), int(row[10])
        if ccn > limit and any(start <= n <= end for n in changed.get(path, ())):
            out.append((path, start, end, name, ccn))
    return out


def check_complexity(rep, changed, allow, limit, mode):
    if mode == 'off':
        return rep.note('complexity', 'N/A', 'desactivado para este repo')
    files = sorted(f for f in changed if is_prod_code(f) and not allowed(allow, f, 'complexity'))
    if not files:
        return rep.note('complexity', 'N/A', 'no hay código de producción cambiado')
    res = subprocess.run([tool('lizard'), '--csv', *files], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if res.returncode != 0:
        rep.annotate('error', 'complejidad', f'lizard terminó con código {res.returncode}: {res.stderr.strip()[:300]}')
        return rep.note('complexity', 'ERROR', 'no se pudo medir la complejidad (lizard falló)')
    found = complexity(res.stdout, changed, limit)
    level = 'error' if mode == 'block' else 'warning'
    for path, start, end, name, ccn in found:
        rep.annotate(level, 'complejidad', f'{name}() tiene complejidad {ccn} (máximo {limit}). Divide la lógica en funciones '
                     'con nombre propio; no trocees solo para bajar el número.', path, start)
    if not found:
        return rep.note('complexity', 'OK', f'ninguna función tocada supera {limit}')
    return rep.note('complexity', 'ERROR' if mode == 'block' else 'AVISO', f'{len(found)} función(es) tocadas superan {limit}')


# ───────────────────────── 5 · pr-size ─────────────────────────
def pr_size(numstat, allow):
    total = 0
    for row in numstat.splitlines():
        parts = row.split('\t')
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        path = git_path(parts[-1]).split(' => ')[-1].replace('{', '').replace('}', '')
        if GENERATED.search(path) or DOCS.search(path) or allowed(allow, path, 'pr-size'):
            continue
        total += int(parts[0]) + int(parts[1])
    return total


def check_pr_size(rep, base, allow, warn, maximum):
    n = pr_size(git('diff', '--numstat', '-M', f'{base}...HEAD'), allow)
    if n > maximum:
        rep.annotate('error', 'tamaño del PR', f'{n} líneas cambiadas (máximo {maximum}). Divide el cambio en PRs más pequeños.')
        return rep.note('pr-size', 'ERROR', f'{n} líneas (máximo {maximum})')
    if n > warn:
        rep.annotate('warning', 'tamaño del PR', f'{n} líneas cambiadas: por encima de {warn} la revisión pierde calidad. Valora dividirlo.')
        return rep.note('pr-size', 'AVISO', f'{n} líneas (recomendado ≤ {warn})')
    return rep.note('pr-size', 'OK', f'{n} líneas cambiadas, sin contar lockfiles, generados ni documentación')


# ───────────────────────── 6 · new-dependency ─────────────────────────
def _req_names(text):
    names = set()
    for raw in text.splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line or line.startswith('-'):
            continue
        m = re.match(r'([A-Za-z0-9][A-Za-z0-9._-]*)', line)
        if m:
            names.add(re.sub(r'[-_.]+', '-', m.group(1)).lower())
    return names


def _npm_names(text):
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return set()
    names = set()
    for key in ('dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies'):
        for name, spec in (data.get(key) or {}).items():
            spec = str(spec)
            if spec.startswith('npm:'):  # alias: "npm:paquete@1.0" o "npm:@scope/paquete@1.0"
                alias = spec[4:]
                names.add(alias[:alias.rfind('@')] if alias.rfind('@') > 0 else alias)
            elif not re.match(r'(workspace|file|link|git|github|http|https):|[./]', spec):
                names.add(name)
    return names


def _pyproject_names(text):
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return set()
    specs = list((data.get('project') or {}).get('dependencies') or [])
    for group in ((data.get('project') or {}).get('optional-dependencies') or {}).values():
        specs += group
    for group in (data.get('dependency-groups') or {}).values():
        specs += [s for s in group if isinstance(s, str)]
    names = _req_names('\n'.join(specs))
    names |= {re.sub(r'[-_.]+', '-', n).lower() for n in ((data.get('tool') or {}).get('poetry') or {}).get('dependencies', {}) if n != 'python'}
    return names


def new_dependencies(base):
    """[(ecosistema, nombre, archivo)] añadidas por el PR."""
    found = []
    for path in filter(None, git('diff', '-z', '--name-only', '--diff-filter=AMR', f'{base}...HEAD').split('\0')):
        name = path.rsplit('/', 1)[-1]
        if 'node_modules/' in path:
            continue
        if name == 'package.json':
            parse, eco = _npm_names, 'npm'
        elif re.fullmatch(r'requirements[^/]*\.txt', name):
            parse, eco = _req_names, 'pypi'
        elif name == 'pyproject.toml':
            parse, eco = _pyproject_names, 'pypi'
        else:
            continue
        try:
            before = git('show', f'{base}:{path}')
        except subprocess.CalledProcessError:
            before = ''
        after = pathlib.Path(path).read_text(encoding='utf-8') if pathlib.Path(path).is_file() else ''
        for dep in sorted(parse(after) - parse(before)):
            found.append((eco, dep, path))
    return found


def registry_created(eco, name):
    """Fecha de creación del paquete en su registro; None si no existe. RuntimeError si no hay respuesta."""
    url = ('https://registry.npmjs.org/' + name.replace('/', '%2F') if eco == 'npm'
           else f'https://pypi.org/pypi/{urllib.parse.quote(name)}/json')
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'platform-pipeline'}), timeout=20) as r:
            data = json.load(r)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise RuntimeError(f'{eco} respondió {error.code}') from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(str(error)) from error
    if eco == 'npm':
        created = (data.get('time') or {}).get('created')
    else:
        times = [f['upload_time_iso_8601'] for files in (data.get('releases') or {}).values() for f in files if f.get('upload_time_iso_8601')]
        created = min(times) if times else None
    return dt.datetime.fromisoformat(created.replace('Z', '+00:00')) if created else None


def check_dependencies(rep, base, allow, min_days, lookup=registry_created, now=None):
    deps = [d for d in new_dependencies(base) if not allowed(allow, d[1], 'new-dependency')]
    if not deps:
        return rep.note('new-dependency', 'N/A', 'el PR no añade dependencias')
    now = now or dt.datetime.now(dt.timezone.utc)
    bad = 0
    for eco, name, path in deps:
        try:
            created = lookup(eco, name)
        except RuntimeError as error:
            rep.annotate('error', 'dependencia nueva', f'No se pudo verificar {name} en {eco} ({error}). Relanza el job.', path)
            bad += 1
            continue
        if created is None:
            rep.annotate('error', 'dependencia nueva', f'"{name}" no existe en {eco}. ¿Nombre inventado por un agente? '
                         'Los atacantes registran esos nombres (slopsquatting).', path)
            bad += 1
            continue
        days = (now - created).days
        if days < min_days:
            rep.annotate('error', 'dependencia nueva', f'"{name}" se publicó hace {days} día(s) (mínimo {min_days}). '
                         'Los paquetes recién creados son el vector típico de ataques a la cadena de suministro.', path)
            bad += 1
        else:
            rep.annotate('notice', 'dependencia nueva', f'{eco}: {name} (existe desde hace {days} días). Que la revise quien apruebe el PR.', path)
    return rep.note('new-dependency', 'ERROR' if bad else 'OK', f'{len(deps)} nueva(s), {bad} rechazada(s)')


# ───────────────────────── 7 · stub ─────────────────────────
def _python_abstract(path, line_no):
    """True si la línea está dentro de un método decorado con @abstractmethod (stub legítimo)."""
    try:
        lines = pathlib.Path(path).read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return False
    indent = len(lines[line_no - 1]) - len(lines[line_no - 1].lstrip())
    for i in range(line_no - 2, -1, -1):
        text = lines[i]
        stripped = text.lstrip()
        if stripped.startswith(('def ', 'async def ')) and len(text) - len(stripped) < indent:
            j = i - 1
            while j >= 0 and lines[j].lstrip().startswith('@'):
                if 'abstractmethod' in lines[j]:
                    return True
                j -= 1
            return False
    return False


def stubs(added, allow):
    """(bloqueos, avisos): [(ruta, línea, texto)]."""
    block, warn = [], []
    for path, lines in added.items():
        if not is_prod_code(path) or allowed(allow, path, 'stub'):
            continue
        for n, text in lines:
            if STUB_BLOCK.search(text):
                if PY.search(path) and _python_abstract(path, n):
                    continue
                block.append((path, n, text.strip()))
            elif STUB_WARN.search(text):
                warn.append((path, n, text.strip()))
    return block, warn


def check_stubs(rep, base, allow):
    block, warn = stubs(added_text(base), allow)
    for path, n, text in block:
        rep.annotate('error', 'sin implementar', f'Código de producción sin implementar: {text[:120]}', path, n)
    for path, n, text in warn[:30]:
        rep.annotate('warning', 'TODO', f'Pendiente añadido: {text[:120]}. Abre una issue en lugar de dejarlo en el código.', path, n)
    if block:
        return rep.note('stub', 'ERROR', f'{len(block)} sin implementar; {len(warn)} TODO/FIXME')
    return rep.note('stub', 'AVISO' if warn else 'OK', f'{len(warn)} TODO/FIXME nuevos' if warn else 'sin stubs ni pendientes nuevos')


# ───────────────────────── main ─────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', required=True)
    ap.add_argument('--allow-file', default='')
    ap.add_argument('--config', default='{}')
    ap.add_argument('--rules', choices=['static', 'project', 'all'], default='all',
                    help='static: sin instalar ni ejecutar nada del proyecto; project: lint y formato (tras instalar)')
    args = ap.parse_args()
    if not args.base or re.fullmatch(r'0+', args.base):
        print('Sin commit base: no aplica.')
        return 0
    cfg = {**DEFAULTS, **json.loads(args.config or '{}')}
    allow = load_allow(args.allow_file)
    stack = os.environ.get('STACK', 'none')
    changed = changed_lines(args.base)
    rep = Report()
    rules = [
        ('lint', lambda: check_lint(rep, changed, allow, stack, os.environ.get('CMD_LINT', ''))),
        ('format', lambda: check_format(rep, changed, allow, stack, bool(cfg['format_required']))),
        ('duplication', lambda: check_duplication(rep, changed, allow, float(cfg['duplication_max']))),
        ('complexity', lambda: check_complexity(rep, changed, allow, int(cfg['complexity_max']), cfg['complexity_mode'])),
        ('pr-size', lambda: check_pr_size(rep, args.base, allow, int(cfg['pr_size_warn']), int(cfg['pr_size_max']))),
        ('new-dependency', lambda: check_dependencies(rep, args.base, allow, int(cfg['new_dependency_min_age_days']))),
        ('stub', lambda: check_stubs(rep, args.base, allow)),
    ]
    project = {'lint', 'format'}
    for name, fn in rules:
        if (args.rules == 'static' and name in project) or (args.rules == 'project' and name not in project):
            continue
        print(f'\n━━━ {name} ━━━')
        try:
            fn()
        except Exception as error:  # una regla rota no debe dar verde en silencio
            rep.note(name, 'ERROR', f'fallo interno: {error}')
    icon = {'OK': '✅', 'AVISO': '⚠️', 'ERROR': '❌', 'N/A': '·'}
    print('\nResumen de calidad')
    for check, status, msg in rep.rows:
        print(f'  {icon[status]} {check:15} {status:5} {msg}')
    if args.rules == 'static' and os.environ.get('GITHUB_OUTPUT'):
        deps_ok = not any(c == 'new-dependency' and s == 'ERROR' for c, s, _ in rep.rows)
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as fh:
            fh.write(f'deps_ok={str(deps_ok).lower()}\n')
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as fh:
            part = {'static': ' (reglas estáticas)', 'project': ' (lint y formato)'}.get(args.rules, '')
            fh.write(f'### Calidad del código cambiado{part}\n\n| | Regla | Resultado |\n|---|---|---|\n')
            for check, status, msg in rep.rows:
                fh.write(f'| {icon[status]} | `{check}` | {msg} |\n')
    return 1 if rep.blocking else 0


if __name__ == '__main__':
    sys.exit(main())
