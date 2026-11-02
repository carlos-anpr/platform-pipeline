#!/usr/bin/env python3
"""Punto 1 · Guardarraíles del diff: falla si el cambio debilita la red de seguridad.

Detecta, solo leyendo git (sin ejecutar nada del PR):
  - tests desactivados (.skip, xit, @pytest.mark.skip, @Disabled…) o enfocados (.only, fit…)
  - supresiones nuevas de lint/tipos (eslint-disable, @ts-ignore, # noqa, # type: ignore…)
  - código excluido de la cobertura (istanbul/c8 ignore, pragma: no cover…)
  - menos aserciones en los tests que antes (se borran más de las que se añaden)
  - archivos de test borrados, o renombrados a algo que ya no es un test
  - umbrales de cobertura rebajados o eliminados en ficheros de configuración
  - policy-config: cualquier cambio en la configuración que decide qué comprueban el lint, el
    formato, los tests, la cobertura o los escáneres (.eslintrc, biome.json, ruff.toml,
    jest/vitest config, .gitleaks.toml, .semgrepignore, osv-scanner.toml…), en los scripts
    lint/test/coverage de package.json o en las secciones [tool.ruff|black|pytest|coverage…]
    de pyproject.toml. Así un PR (o un agente) no puede relajar las reglas que lo juzgan.

Las excepciones legítimas las pone plataforma en repos/<repo>.json -> exceptions, que el
job convierte en un archivo:  <glob de ruta>  <regla|*>   # motivo
Uso: check-diff-guardrails.py --base <sha> [--head <sha>] --allow-file <archivo>
"""
import json
import tomllib
import argparse
import fnmatch
import pathlib
import re
import subprocess
import sys

TEST_PATH = re.compile(
    r'(^|/)(tests?|__tests__|specs?)(/|$)|\.(test|spec)\.[cm]?[jt]sx?$|(^|/)test_[^/]+\.py$|_test\.(py|go)$|Tests?\.(cs|java|kt)$',
    re.I)
DOCS = re.compile(r'\.(md|markdown|txt|rst|adoc|svg|png|jpe?g|gif|pdf|lock)$', re.I)
OWN = ()  # modelo centralizado: los scripts y las excepciones no están en el repo del código, nada queda exento

# Configuración que decide QUÉ se comprueba: cambiarla requiere excepción de plataforma (regla policy-config)
POLICY_FILES = re.compile(
    r'(^|/)(\.eslintrc(\.[a-z]+)?|eslint\.config\.[cm]?[jt]s|\.eslintignore|biome\.jsonc?|\.prettierrc(\.[a-z]+)?'
    r'|prettier\.config\.[cm]?[jt]s|\.prettierignore|\.?ruff\.toml|\.flake8|\.?pylintrc|mypy\.ini'
    r'|(jest|vitest|karma)\.config\.[cm]?[jt]s|vitest\.workspace\.[cm]?[jt]s|\.mocharc(\.[a-z]+)?|\.nycrc(\.[a-z]+)?'
    r'|\.c8rc(\.json)?|\.coveragerc|pytest\.ini|tox\.ini|setup\.cfg|codecov\.ya?ml|\.codecov\.ya?ml'
    r'|\.gitleaks\.toml|\.gitleaksignore|\.semgrepignore|\.semgrep\.ya?ml|osv-scanner\.toml|\.trivyignore'
    r'|\.jscpd\.json|\.config/jscpd\.json)$', re.I)
VITE_CONFIG = re.compile(r'(^|/)vite\.config\.[cm]?[jt]s$', re.I)          # solo cuenta su bloque test: { … }
SCRIPT_PATH = re.compile(r'(?<![\w@:/.-])(?:\./)?((?:[\w.-]+/)*[\w.-]+\.(?:[cm]?[jt]s|sh|py|ps1|json))(?![\w/.-])')
PKG_SCRIPTS = ('lint', 'test', 'coverage', 'pretest', 'posttest', 'format', 'check', 'typecheck')
PKG_KEYS = ('jest', 'c8', 'nyc', 'mocha', 'ava', 'eslintConfig', 'eslintIgnore', 'prettier', 'vitest', 'jscpd')
PYPROJECT_TOOLS = ('ruff', 'black', 'pytest', 'coverage', 'mypy', 'pylint', 'flake8', 'isort')

LINE_RULES = [
    ('test-skip', 'test desactivado',
     r'\b(?:it|test|describe|context|suite)\.(?:skip|todo)\s*\(|\bx(?:it|test|describe)\s*\(|@pytest\.mark\.(?:skip|skipif|xfail)\b'
     r'|@unittest\.skip|\bpytest\.skip\s*\(|@Disabled\b|@Ignore\b|\[Ignore\]|\[(?:Fact|Theory)\s*\([^)]*Skip\s*=|\bt\.Skip(?:Now|f)?\s*\('),
    ('test-only', 'test enfocado (.only): deja de ejecutarse el resto',
     r'\b(?:it|test|describe|context|suite)\.only\s*\(|\bf(?:it|describe)\s*\('),
    ('suppression', 'supresión nueva de lint o de tipos',
     r'eslint-disable|@ts-ignore|@ts-nocheck|@ts-expect-error|biome-ignore|#\s*noqa\b|#\s*type:\s*ignore|#\s*pylint:\s*disable'
     r'|//\s*nolint|@SuppressWarnings\s*\(|#pragma\s+warning\s+disable|\[SuppressMessage\s*\(|NOSONAR|nosemgrep'
     r'|jscpd:ignore-start|gitleaks:allow'),
    ('coverage-ignore', 'código excluido de la cobertura',
     r'istanbul\s+ignore|\bc8\s+ignore|\bv8\s+ignore|pragma:\s*no\s*cover|@codeCoverageIgnore|\[ExcludeFromCodeCoverage\]'),
]
LINE_RULES = [(rid, msg, re.compile(rx)) for rid, msg, rx in LINE_RULES]

ASSERT = re.compile(
    r'\bexpect\s*\(|\bassert(?:[A-Z]\w*|_\w+)?\s*\(|\bassert\.\w+\s*\(|\bassert\s+\S|\bAssert\.\w+\s*\(|\.should\b'
    r'|\bassertThat\s*\(|\bt\.(?:Error|Errorf|Fatal|Fatalf|Fail)\s*\(|\bself\.assert\w+\s*\(')

COVERAGE_FILES = re.compile(
    r'(^|/)(package\.json|(vitest|vite|jest|karma)\.config\.[cm]?[jt]s|\.nycrc(\.json|\.ya?ml)?|\.c8rc(\.json)?|pyproject\.toml|setup\.cfg'
    r'|\.coveragerc|tox\.ini|\.?codecov\.ya?ml|pom\.xml|build\.gradle(\.kts)?|[^/]*\.csproj|Directory\.Build\.props|coverlet\.runsettings)$', re.I)
THRESHOLD = re.compile(
    r'''["']?(fail[_-]?under|failUnder|minimum|min(?:imum)?Coverage|threshold|lines|branches|functions|statements)["']?\s*[:=]\s*["']?(\d+(?:\.\d+)?)'''
    r'''|--(lines|branches|functions|statements|fail-under|cov-fail-under)[= ](\d+(?:\.\d+)?)''', re.I)


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


def load_allow(path='.guardrails-allow'):
    rules = []
    p = pathlib.Path(path)
    if not p.is_file():
        return rules
    for raw in p.read_text(encoding='utf-8').splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        rules.append((parts[0], parts[1] if len(parts) > 1 else '*'))
    return rules


def allowed(allow, path, rule):
    return any(fnmatch.fnmatch(path, g) and r in ('*', rule) for g, r in allow)


def parse_diff(text):
    """Devuelve {ruta: {'added': [(nº, texto)], 'removed': [texto]}} de un diff unificado -U0."""
    files, cur, new_line, header = {}, None, 0, False
    for line in text.splitlines():
        if line.startswith('diff --git '):
            cur, header = None, True
        elif header and line.startswith('+++ '):
            cur = b_path(line[4:])
            if cur is not None:
                files.setdefault(cur, {'added': [], 'removed': []})
        elif header and line.startswith('--- '):
            continue  # la ruta buena es la de +++ (soporta renombrados)
        elif line.startswith('@@'):
            header = False
            m = re.match(r'@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
            new_line = int(m.group(1)) if m else 0
        elif cur is not None and line.startswith('+') and not line.startswith('+++'):
            files[cur]['added'].append((new_line, line[1:]))
            new_line += 1
        elif cur is not None and line.startswith('-') and not line.startswith('---'):
            files[cur]['removed'].append(line[1:])
    return files


def show(rev, path):
    """Contenido de un archivo en un commit, o None si no existe."""
    try:
        return git('show', f'{rev}:{path}')
    except subprocess.CalledProcessError:
        return None


def package_policy(text):
    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {'(json no válido)': text}
    scripts = data.get('scripts') or {}
    return {'scripts.' + k: scripts.get(k) for k in PKG_SCRIPTS} | {k: data.get(k) for k in PKG_KEYS}


def pyproject_policy(text):
    try:
        tools = (tomllib.loads(text) if text else {}).get('tool') or {}
    except tomllib.TOMLDecodeError:
        return {'(toml no válido)': text}
    return {'tool.' + k: tools.get(k) for k in PYPROJECT_TOOLS}


def vite_test_block(text):
    """Texto del bloque test: { … } de un vite.config (sin espacios), o None si no hay."""
    m = re.search(r'\btest\s*:\s*\{', text or '')
    if not m:
        return None
    depth, i, quote = 0, m.end() - 1, None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == '\\':
                i += 1
            elif ch == quote:
                quote = None
        elif ch in '\'"`':
            quote = ch
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return re.sub(r'\s+', '', text[m.start():i + 1])
        i += 1
    return re.sub(r'\s+', '', text[m.start():])


def policy_reader(path):
    if path.endswith('package.json'):
        return package_policy
    if path.endswith('pyproject.toml'):
        return pyproject_policy
    if VITE_CONFIG.search(path):
        return lambda text: {'test (vite.config)': vite_test_block(text)}
    return None


def policy_changes(base, head, old, new, deleted=False):
    """Claves de política que cambian entre el archivo en base (old) y en head (new; None si se borró)."""
    reader = policy_reader(old) or policy_reader(new)
    before = reader(show(base, old))
    after = reader(None if deleted else show(head, new))
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


def script_files(base, head):
    """Archivos que ejecutan los scripts lint/test/coverage… de package.json (en base o en head)."""
    found = set()
    for rev in (base, head):
        for pkg in filter(None, git('ls-tree', '-r', '--name-only', rev).splitlines()):
            if not pkg.endswith('package.json') or '/node_modules/' in f'/{pkg}':
                continue
            folder = pkg[:-len('package.json')]
            scripts = package_policy(show(rev, pkg))
            for key, cmd in scripts.items():
                if key.startswith('scripts.') and isinstance(cmd, str):
                    found.update(folder + m.group(1) for m in SCRIPT_PATH.finditer(cmd))
    return found


def name_status(rng):
    """[(estado, ruta_antigua, ruta_nueva)] sin comillas ni escapes (-z)."""
    parts, out, i = git('diff', '--name-status', '-z', '-M', rng).split('\0'), [], 0
    while i < len(parts) and parts[i]:
        status = parts[i]
        if status[0] in 'RC':
            out.append((status[0], parts[i + 1], parts[i + 2]))
            i += 3
        else:
            out.append((status[0], parts[i + 1], parts[i + 1]))
            i += 2
    return out


def thresholds(lines):
    found = {}
    for text in lines:
        for m in THRESHOLD.finditer(text):
            key = (m.group(1) or m.group(3)).lower().replace('-', '_')
            found[key] = float(m.group(2) or m.group(4))
    return found


def check(base, head, allow):
    violations = []  # (regla, ruta, línea|None, mensaje)
    rng = f'{base}...{head}'
    changes = name_status(rng)
    for st, old, new in changes:
        if st == 'D' and TEST_PATH.search(old) and not allowed(allow, old, 'test-deleted'):
            violations.append(('test-deleted', old, None, 'archivo de test borrado'))
        elif st == 'R' and TEST_PATH.search(old) and not TEST_PATH.search(new) and not allowed(allow, old, 'test-deleted'):
            violations.append(('test-deleted', old, None, f'test renombrado a {new}, que ya no se ejecuta como test'))
        for path in {old, new}:
            if POLICY_FILES.search(path) and not allowed(allow, path, 'policy-config'):
                violations.append(('policy-config', path, None, 'cambia la configuración de lint, formato, tests, cobertura o '
                                   'escáneres. Pide a plataforma una excepción policy-config en repos/<repo>.json'))
                break
        # package.json, pyproject.toml y vite.config: solo sus claves de política, también al borrar o renombrar
        if (policy_reader(old) or policy_reader(new)) and not allowed(allow, new, 'policy-config'):
            keys = policy_changes(base, head, old, new, deleted=(st == 'D'))
            if keys:
                violations.append(('policy-config', new, None, 'cambia ' + ', '.join(keys) + ': decide qué comprueban los '
                                   'checks. Pide a plataforma una excepción policy-config en repos/<repo>.json'))
    runners = script_files(base, head)
    for st, old, new in changes:
        hit = next((p for p in (old, new) if p in runners), None)
        if hit and not allowed(allow, hit, 'policy-config'):
            violations.append(('policy-config', hit, None, 'es el script que ejecuta lint/test/coverage desde package.json: '
                               'cambiarlo cambia lo que se comprueba. Pide a plataforma una excepción policy-config'))
    diff = parse_diff(git('diff', '--no-color', '--unified=0', '-M', rng))
    asserts_added = asserts_removed = 0
    loss_by_file = {}
    for path, d in diff.items():
        if (OWN and path.startswith(OWN)) or DOCS.search(path):
            continue
        for number, text in d['added']:
            for rid, msg, rx in LINE_RULES:
                if rx.search(text) and not allowed(allow, path, rid):
                    violations.append((rid, path, number, msg))
        if TEST_PATH.search(path):
            a = sum(len(ASSERT.findall(t)) for _, t in d['added'])
            r = sum(len(ASSERT.findall(t)) for t in d['removed'])
            asserts_added += a
            asserts_removed += r
            if r > a and not allowed(allow, path, 'assert-removed'):
                loss_by_file[path] = r - a
        if COVERAGE_FILES.search(path) and not allowed(allow, path, 'coverage-threshold'):
            before, after = thresholds(d['removed']), thresholds(t for _, t in d['added'])
            for key, old in before.items():
                if key not in after:
                    violations.append(('coverage-threshold', path, None, f'umbral de cobertura "{key}" eliminado (antes {old:g})'))
                elif after[key] < old:
                    violations.append(('coverage-threshold', path, None, f'umbral de cobertura "{key}" rebajado de {old:g} a {after[key]:g}'))
    if asserts_removed > asserts_added and loss_by_file:
        detail = ', '.join(f'{p} (-{n})' for p, n in sorted(loss_by_file.items()))
        violations.append(('assert-removed', next(iter(sorted(loss_by_file))), None,
                           f'el PR quita {asserts_removed} aserciones y añade {asserts_added}: {detail}'))
    return violations


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', required=True)
    ap.add_argument('--head', default='HEAD')
    ap.add_argument('--allow-file', default='.guardrails-allow')
    args = ap.parse_args()
    if not args.base or re.fullmatch(r'0+', args.base):
        print('Sin commit base (rama nueva o evento sin diff): no aplica.')
        return 0
    violations = check(args.base, args.head, load_allow(args.allow_file))
    if not violations:
        print('OK: el cambio no debilita tests, supresiones ni umbrales de cobertura.')
        return 0
    for rid, path, line, msg in violations:
        loc = f'file={path},line={line}' if line else f'file={path}'
        print(f'::error {loc},title=Guardarraíl {rid}::{msg}')
    print(f'\n{len(violations)} problema(s). Si es una excepción legítima, pide a plataforma que la añada en '
          'repos/<repo>.json -> exceptions (con su motivo).')
    return 1


if __name__ == '__main__':
    sys.exit(main())
