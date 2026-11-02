#!/usr/bin/env python3
"""Punto 2 · Cobertura de las líneas cambiadas (diff coverage).

Lee el informe de cobertura que generan los tests (LCOV o Cobertura XML) y calcula qué
porcentaje de las líneas de código AÑADIDAS o MODIFICADAS en el PR ejecutan los tests.
Falla si queda por debajo del mínimo (variable DIFF_COVERAGE_MIN, 80 por defecto).

- Un archivo de código cambiado que no aparece en el informe cuenta como NO cubierto
  (si ningún test lo carga, nadie lo prueba). Excepción: .guardrails-allow con la regla
  "diff-coverage" (p. ej. el punto de arranque, que cubre el smoke test).
- Los tests, la configuración, la documentación y scripts/ci no cuentan.
Uso: check-diff-coverage.py --base <sha> [--head <sha>] [--min 80] [--report ruta ...]
"""
import argparse
import fnmatch
import os
import pathlib
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

SOURCE = re.compile(r'\.(?:[cm]?[jt]sx?|py|java|kt|cs|go|rb|php)$', re.I)
TEST_PATH = re.compile(
    r'(^|/)(tests?|__tests__|specs?)(/|$)|\.(test|spec)\.[cm]?[jt]sx?$|(^|/)test_[^/]+\.py$|_test\.(py|go)$|Tests?\.(cs|java|kt)$', re.I)
NOT_CODE = re.compile(r'(^|/)(scripts/ci/|\.github/)|\.config\.[cm]?[jt]s$|(^|/)(eslint|prettier|babel|webpack|rollup)\.', re.I)
DEFAULT_REPORTS = ['coverage/lcov.info', 'lcov.info', 'coverage.xml', 'coverage/cobertura-coverage.xml', 'coverage/coverage.cobertura.xml']


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


def norm(path, roots=('',)):
    p = path.replace('\\', '/')
    cwd = str(pathlib.Path.cwd()).replace('\\', '/')
    for root in roots:
        cand = (root.rstrip('/') + '/' + p) if root and not p.startswith('/') and not re.match(r'^[A-Za-z]:/', p) else p
        if cand.lower().startswith(cwd.lower() + '/'):
            cand = cand[len(cwd) + 1:]
        cand = cand.lstrip('./') if cand.startswith('./') else cand
        if pathlib.Path(cand).is_file() or root == roots[-1]:
            return cand
    return p


def read_lcov(text):
    data, cur = {}, None
    for line in text.splitlines():
        if line.startswith('SF:'):
            cur = data.setdefault(norm(line[3:].strip()), {})
        elif line.startswith('DA:') and cur is not None:
            num, hits = line[3:].split(',')[:2]
            cur[int(num)] = max(cur.get(int(num), 0), int(float(hits)))
        elif line.startswith('end_of_record'):
            cur = None
    return data


def read_cobertura(text):
    root = ET.fromstring(text)
    sources = [s.text.strip() for s in root.iter('source') if s.text] or ['']
    data = {}
    for cls in root.iter('class'):
        f = data.setdefault(norm(cls.get('filename', ''), tuple(sources) + ('',)), {})
        for ln in cls.iter('line'):
            n, h = int(ln.get('number')), int(float(ln.get('hits', '0')))
            f[n] = max(f.get(n, 0), h)
    return data


def load_reports(paths):
    data, used = {}, []
    for path in paths:
        p = pathlib.Path(path)
        if not p.is_file():
            continue
        text = p.read_text(encoding='utf-8', errors='replace')
        part = read_cobertura(text) if text.lstrip().startswith('<') else read_lcov(text)
        for f, lines in part.items():
            dst = data.setdefault(f, {})
            for n, h in lines.items():
                dst[n] = max(dst.get(n, 0), h)
        used.append(path)
    return data, used


def changed_lines(base, head):
    out, cur = {}, None
    for line in git('diff', '--no-color', '--unified=0', '-M', '--diff-filter=AMR', f'{base}...{head}').splitlines():
        if line.startswith('+++ '):
            cur = b_path(line[4:])
        elif line.startswith('@@') and cur:
            m = re.match(r'@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@', line)
            start, count = int(m.group(1)), int(m.group(2) or '1')
            out.setdefault(cur, set()).update(range(start, start + count))
    return out


def allow_rules(path='.guardrails-allow'):
    p = pathlib.Path(path)
    rules = []
    if p.is_file():
        for raw in p.read_text(encoding='utf-8').splitlines():
            parts = raw.split('#', 1)[0].split()
            if parts:
                rules.append((parts[0], parts[1] if len(parts) > 1 else '*'))
    return rules


def code_lines(path, numbers):
    """Para archivos sin datos de cobertura: cuenta solo líneas con código (ni vacías ni comentarios)."""
    try:
        lines = pathlib.Path(path).read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return set()
    keep = set()
    for n in numbers:
        if 1 <= n <= len(lines):
            s = lines[n - 1].strip()
            if s and not s.startswith(('//', '#', '/*', '*', '*/', '"""', "'''")) and s not in ('{', '}', '};', ')', '];', 'end'):
                keep.add(n)
    return keep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', required=True)
    ap.add_argument('--head', default='HEAD')
    ap.add_argument('--min', type=float, default=float(os.environ.get('DIFF_COVERAGE_MIN') or 80))
    ap.add_argument('--report', action='append')
    ap.add_argument('--allow-file', default='.guardrails-allow')
    args = ap.parse_args()
    if not args.base or re.fullmatch(r'0+', args.base):
        print('Sin commit base: no aplica.')
        return 0
    allow = allow_rules(args.allow_file)
    changed = {p: ls for p, ls in changed_lines(args.base, args.head).items()
               if SOURCE.search(p) and not TEST_PATH.search(p) and not NOT_CODE.search(p)
               and not any(fnmatch.fnmatch(p, g) and r in ('*', 'diff-coverage') for g, r in allow)}
    if not changed:
        print('El PR no cambia código de producción: la cobertura del diff no aplica.')
        return 0
    data, used = load_reports(args.report or DEFAULT_REPORTS)
    if not used:
        print('::error title=Cobertura::No hay informe de cobertura. El proyecto debe generar coverage/lcov.info o coverage.xml '
              '(en Node: script "coverage" del package.json; en Python: pytest --cov --cov-report=xml).')
        return 1
    total = covered = 0
    rows = []
    for path, lines in sorted(changed.items()):
        info = data.get(path)
        if info is None:
            relevant = code_lines(path, lines)
            if not relevant:
                continue
            total += len(relevant)
            rows.append((path, 0, len(relevant), sorted(relevant), 'ningún test carga este archivo'))
            continue
        relevant = [n for n in sorted(lines) if n in info]
        hit = [n for n in relevant if info[n] > 0]
        total += len(relevant)
        covered += len(hit)
        if relevant:
            rows.append((path, len(hit), len(relevant), [n for n in relevant if info[n] == 0], ''))
    if total == 0:
        print('Las líneas cambiadas no son ejecutables (declaraciones, comentarios): no aplica.')
        return 0
    pct = 100.0 * covered / total
    print(f'Informe(s): {", ".join(used)}')
    print(f'{"Archivo":60} {"cubiertas":>10}')
    for path, hit, n, missing, note in rows:
        print(f'{path:60} {hit:>4}/{n:<5} {note}')
        for start in missing[:20]:
            print(f'::warning file={path},line={start},title=Línea sin cubrir::Línea cambiada que ningún test ejecuta')
    print(f'\nCobertura de las líneas cambiadas: {pct:.1f} % (mínimo {args.min:g} %)')
    if pct + 1e-9 < args.min:
        print(f'::error title=Cobertura del diff::Solo el {pct:.1f} % de las líneas cambiadas tiene tests (mínimo {args.min:g} %). '
              'Añade tests para las líneas marcadas.')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
