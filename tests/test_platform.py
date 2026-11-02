"""Pruebas de los scripts de control del CI (puntos 1, 2, 3 y 7).
Ejecutar desde la raíz del repo de plataforma:  python -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

ACTIONS = pathlib.Path(__file__).resolve().parent.parent / 'actions'
WHERE = {'check-diff-guardrails': 'guardrails', 'check-diff-coverage': 'diff-coverage',
         'check-acceptance': 'acceptance', 'pr-provenance': 'provenance', 'repo_config': 'repo-config',
         'check-quality': 'quality'}


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ACTIONS / WHERE[name] / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard = load('check-diff-guardrails')
cov = load('check-diff-coverage')
acc = load('check-acceptance')
prov = load('pr-provenance')
repo_config = load('repo_config')
quality = load('check-quality')


class Repo:
    """Repo git temporal: commit base, cambios, commit head."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.old = os.getcwd()
        os.chdir(self.root)
        for args in (['init', '-q', '-b', 'develop'], ['config', 'user.email', 't@example.com'], ['config', 'user.name', 'T'],
                     ['config', 'core.autocrlf', 'false']):
            subprocess.run(['git', *args], check=True)

    def write(self, path, text):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')

    def rm(self, path):
        (self.root / path).unlink()

    def commit(self, msg='c'):
        subprocess.run(['git', 'add', '-A'], check=True)
        subprocess.run(['git', 'commit', '-q', '-m', msg], check=True)
        return subprocess.run(['git', 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()

    def close(self):
        os.chdir(self.old)
        self.tmp.cleanup()


def run_main(mod, argv):
    import sys
    old = sys.argv
    sys.argv = ['x', *argv]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = mod.main()
    finally:
        sys.argv = old
    return code, buf.getvalue()


class GuardrailsTest(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.r.write('test/a.test.js', "test('uno', () => {\n  expect(f(1)).toBe(1);\n  expect(f(2)).toBe(2);\n  expect(f(3)).toBe(3);\n});\n")
        self.r.write('test/b.test.js', "test('b', () => { expect(1).toBe(1); });\n")
        self.r.write('src/a.js', 'export const f = x => x;\n')
        self.r.write('package.json', '{\n  "c8": { "lines": 80, "branches": 70 }\n}\n')
        self.base = self.r.commit('base')

    def tearDown(self):
        self.r.close()

    def ids(self, allow=()):
        return sorted({v[0] for v in guard.check(self.base, 'HEAD', list(allow))})

    def test_detecta_todas_las_formas_de_debilitar(self):
        self.r.write('test/a.test.js', "test.skip('uno', () => {\n  expect(f(1)).toBe(1);\n});\n")
        self.r.write('src/a.js', '// eslint-disable-next-line\nexport const f = x => x;\n')
        self.r.write('package.json', '{\n  "c8": { "lines": 60 }\n}\n')
        self.r.rm('test/b.test.js')
        self.r.commit('head')
        self.assertEqual(self.ids(), ['assert-removed', 'coverage-threshold', 'policy-config', 'suppression', 'test-deleted',
                                      'test-skip'])

    def test_cambio_sano_no_genera_avisos(self):
        self.r.write('test/c.test.js', "test('AC-1: nuevo', () => { expect(f(4)).toBe(4); });\n")
        self.r.write('README.md', "Ejemplo: no uses `it.only(` ni `@ts-ignore`.\n")
        self.r.commit('head')
        self.assertEqual(self.ids(), [])

    def test_only_y_excepcion_autorizada(self):
        self.r.write('src/legacy.js', '/* eslint-disable */\nmodule.exports = 1;\n')
        self.r.write('test/d.test.js', "it.only('foco', () => { expect(1).toBe(1); });\n")
        self.r.commit('head')
        self.assertEqual(self.ids(), ['suppression', 'test-only'])
        self.assertEqual(self.ids([('src/legacy.js', 'suppression')]), ['test-only'])

    def test_renombrar_un_test_fuera_del_patron_es_borrarlo(self):
        (self.r.root / 'archive').mkdir(exist_ok=True)
        subprocess.run(['git', 'mv', 'test/b.test.js', 'archive/b.js.bak'], check=True)
        self.r.commit('head')
        self.assertEqual(self.ids(), ['test-deleted'])

    def test_policy_config_escaneres_lint_y_scripts(self):
        self.r.write('.semgrepignore', 'src/\n')
        self.r.write('package.json', '{\n  "version": "1.0.1",\n  "scripts": {"lint": "node -e 0"},\n  "c8": { "lines": 80, "branches": 70 }\n}\n')
        self.r.commit('head')
        found = guard.check(self.base, 'HEAD', [])
        self.assertEqual(sorted(p for r, p, _, _ in found if r == 'policy-config'), ['.semgrepignore', 'package.json'])
        self.assertIn('scripts.lint', next(m for r, p, _, m in found if p == 'package.json'))
        self.assertEqual(self.ids([('.semgrepignore', 'policy-config'), ('package.json', 'policy-config')]), [])

    def test_subir_version_o_dependencias_no_es_policy(self):
        self.r.write('package.json', '{\n  "version": "2.0.0",\n  "dependencies": {"x": "1"},\n  "c8": { "lines": 80, "branches": 70 }\n}\n')
        self.r.write('pyproject.toml', '[project]\nname = "x"\n')
        self.r.commit('head')
        self.assertEqual(self.ids(), [])

    def test_borrar_pyproject_con_ruff_es_policy(self):
        self.r.write('pyproject.toml', '[tool.ruff.lint]\nselect = ["ALL"]\n')
        base = self.r.commit('py')
        self.r.rm('pyproject.toml')
        self.r.commit('head')
        found = guard.check(base, 'HEAD', [])
        self.assertEqual([(r, p) for r, p, _, _ in found], [('policy-config', 'pyproject.toml')])

    def test_vite_build_no_es_policy_y_test_si(self):
        self.r.write('vite.config.js', "export default {\n  build: { sourcemap: false },\n  test: { include: ['test/**'] },\n};\n")
        base = self.r.commit('vite')
        self.r.write('vite.config.js', "export default {\n  build: { commonjsOptions: { exclude: [/node_modules/] } },\n  test: { include: ['test/**'] },\n};\n")
        self.r.commit('build')
        self.assertEqual(guard.check(base, 'HEAD', []), [])
        self.r.write('vite.config.js', "export default {\n  build: { sourcemap: false },\n  test: {\n    include: ['test/a.test.js'],\n  },\n};\n")
        self.r.commit('test')
        self.assertEqual({v[0] for v in guard.check(base, 'HEAD', [])}, {'policy-config'})

    def test_script_que_ejecuta_el_lint_es_policy(self):
        self.r.write('package.json', '{\n  "scripts": {"lint": "node tools/lint.js --strict"},\n  "c8": { "lines": 80, "branches": 70 }\n}\n')
        self.r.write('tools/lint.js', 'require("./run")();\n')
        self.r.write('tools/other.js', 'module.exports = 1;\n')
        base = self.r.commit('lint')
        self.r.write('tools/lint.js', 'process.exit(0);\n')
        self.r.write('tools/other.js', 'module.exports = 2;\n')
        self.r.commit('head')
        found = guard.check(base, 'HEAD', [])
        self.assertEqual([(r, p) for r, p, _, _ in found], [('policy-config', 'tools/lint.js')])

    def test_jscpd_config_y_marcadores(self):
        self.r.write('.jscpd.json', '{"ignorePattern": [".*"]}\n')
        self.r.write('src/b.js', '// jscpd:ignore-start\nexport const g = 1;\n// jscpd:ignore-end\n')
        self.r.commit('head')
        self.assertEqual(self.ids(), ['policy-config', 'suppression'])

    def test_pyproject_tool_ruff_es_policy(self):
        self.r.write('pyproject.toml', '[project]\nname = "x"\n')
        base = self.r.commit('py')
        self.r.write('pyproject.toml', '[project]\nname = "x"\n[tool.ruff.lint]\nselect = []\n')
        self.r.commit('head')
        self.assertEqual({v[0] for v in guard.check(base, 'HEAD', [])}, {'policy-config'})

    def test_ruta_con_acentos(self):
        self.r.write('test/café.test.js', "test.skip('x', () => { expect(1).toBe(1); });\n")
        self.r.commit('head')
        found = guard.check(self.base, 'HEAD', [])
        self.assertEqual([(r, p) for r, p, _, _ in found], [('test-skip', 'test/café.test.js')])

    def test_git_path_decodifica_comillas(self):
        self.assertEqual(guard.git_path('"b/src/caf\\303\\251 \\"x\\".py"'), 'b/src/café "x".py')
        self.assertEqual(guard.b_path('b/a b.py\t'), 'a b.py')
        self.assertIsNone(guard.b_path('/dev/null'))

    def test_main_sin_base_no_aplica(self):
        code, out = run_main(guard, ['--base', '0' * 40])
        self.assertEqual(code, 0)
        self.assertIn('no aplica', out)


class DiffCoverageTest(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.r.write('src/a.js', 'export function a(x) {\n  return x;\n}\n')
        self.base = self.r.commit('base')
        self.r.write('src/a.js', 'export function a(x) {\n  if (x < 0) {\n    return -x;\n  }\n  return x;\n}\n')
        self.r.write('src/b.js', 'export const b = () => 42;\n')
        self.r.commit('head')

    def tearDown(self):
        self.r.close()

    def lcov(self, hits):
        lines = ''.join(f'DA:{n},{h}\n' for n, h in hits.items())
        self.r.write('coverage/lcov.info', f'TN:\nSF:{self.r.root / "src" / "a.js"}\n{lines}end_of_record\n')

    def test_falla_por_debajo_del_minimo(self):
        self.lcov({1: 1, 2: 1, 3: 0, 5: 1})   # 3 y b.js sin cubrir
        code, out = run_main(cov, ['--base', self.base, '--min', '80'])
        self.assertEqual(code, 1)
        self.assertIn('ningún test carga este archivo', out)

    def test_pasa_con_excepcion_y_lineas_cubiertas(self):
        self.lcov({1: 1, 2: 1, 3: 1, 5: 1})
        self.r.write('allow', 'src/b.js diff-coverage\n')
        code, out = run_main(cov, ['--base', self.base, '--min', '80', '--allow-file', 'allow'])
        self.assertEqual(code, 0, out)
        self.assertIn('100.0 %', out)

    def test_sin_informe_falla(self):
        code, out = run_main(cov, ['--base', self.base])
        self.assertEqual(code, 1)
        self.assertIn('No hay informe de cobertura', out)


class AcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.r.write('test/s.test.js', "test('AC-1: hola mundo', () => {});\n")
        self.r.write('tests/test_x.py', 'def test_ac2_vacio():\n    pass\n')
        self.r.commit('base')

    def tearDown(self):
        self.r.close()

    def check(self, body, title):
        self.r.write('body.md', body)
        return run_main(acc, ['--body-file', 'body.md', '--title', title])

    def test_criterios_con_test(self):
        code, out = self.check('- [ ] AC-1: a\n- [x] AC-2: b\n', 'feat: slugify')
        self.assertEqual(code, 0, out)

    def test_criterio_sin_test(self):
        code, out = self.check('- [ ] AC-1: a\n- [ ] AC-3: c\n', 'feat: slugify')
        self.assertEqual(code, 1)
        self.assertIn('AC-3', out)

    def test_feat_sin_criterios_falla_y_chore_no(self):
        self.assertEqual(self.check('Sin criterios\n', 'fix: algo')[0], 1)
        self.assertEqual(self.check('Sin criterios\n', 'chore: deps')[0], 0)

    def test_un_comentario_no_es_un_test(self):
        self.r.write('test/unused.test.js', '// AC-9\n/* AC-9 */\n')
        self.r.write('tests/test_y.py', '# AC-9\ndef helper():\n    pass\n')
        self.r.commit('tests')
        code, out = self.check('- [ ] AC-9: algo\n', 'feat: x')
        self.assertEqual(code, 1, out)
        self.assertIn('AC-9', out)

    def test_python_async_cuenta_y_cadena_no(self):
        self.r.write('tests/test_z.py', 'import pytest\n\n@pytest.mark.asyncio\nasync def test_ac7_async():\n    assert True\n')
        self.r.write('tests/test_doc.py', '"""\ndef test_ac8_falso():\n    pass\n"""\n')
        self.r.commit('tests')
        self.assertEqual(self.check('- [ ] AC-7: a\n', 'feat: x')[0], 0)
        code, out = self.check('- [ ] AC-8: b\n', 'feat: x')
        self.assertEqual(code, 1, out)

    def test_ejemplo_en_comentario_de_plantilla_no_cuenta(self):
        code, _ = self.check("<!-- Ejemplo: - [ ] AC-9: x -->\n- [ ] AC-1: a\n", 'feat: x')
        self.assertEqual(code, 0)


class ProvenanceTest(unittest.TestCase):
    TEMPLATE = '## Agente de IA usado\n- [{n}] Ninguno\n- [{c}] Claude Code\n- [ ] Codex\n\n## Evidencia\n- [x] algo\n'

    def pr(self, n=' ', c=' ', user='User', head='feature/1', login='dev', fork=False):
        return {'number': 1, 'user': {'type': user, 'login': login}, 'body': self.TEMPLATE.format(n=n, c=c),
                'head': {'ref': head, 'repo': {'full_name': 'otro/app' if fork else 'org/app'}},
                'base': {'repo': {'full_name': 'org/app'}}}

    claude_commit = [{'commit': {'message': 'feat: x\n\nCo-Authored-By: Claude <noreply@anthropic.com>'}, 'author': {'login': 'dev'}}]

    def test_declaracion_correcta(self):
        errors, labels = prov.evaluate(self.pr(c='x'), self.claude_commit)
        self.assertEqual(errors, [])
        self.assertEqual(labels, {'ai:claude-code'})

    def test_ninguno_contradice_commits(self):
        errors, labels = prov.evaluate(self.pr(n='x'), self.claude_commit)
        self.assertTrue(any('Ninguno' in e for e in errors))
        self.assertEqual(labels, {'ai:claude-code'})

    def test_sin_marcar_falla(self):
        errors, labels = prov.evaluate(self.pr(), [])
        self.assertTrue(errors)
        self.assertEqual(labels, {'ai:sin-declarar'})

    def test_exenciones_verificables(self):
        exento = ([], set())
        self.assertEqual(prov.evaluate(self.pr(user='Bot', login='dependabot[bot]'), []), exento)
        self.assertNotEqual(prov.evaluate(self.pr(user='Bot', login='github-actions[bot]'), []), exento)
        release = self.pr(head='release-please--branches--main')
        changelog = {'filename': 'CHANGELOG.md', 'patch': '@@ -1 +1,3 @@\n+## 1.3.0\n+- feat: x'}
        version = {'filename': 'package.json', 'patch': '@@ -2 +2 @@\n-  "version": "1.2.0",\n+  "version": "1.3.0",'}
        manifest = {'filename': '.release-please-manifest.json', 'patch': '@@ -1 +1 @@\n-  ".": "1.2.0"\n+  ".": "1.3.0"'}
        script = {'filename': 'package.json', 'patch': '@@ -3 +3 @@\n-  "test": "node --test",\n+  "test": "exit 0",'}
        self.assertEqual(prov.evaluate(release, [], files=[changelog, version, manifest]), exento)
        self.assertNotEqual(prov.evaluate(release, [], files=[changelog, script]), exento)
        self.assertNotEqual(prov.evaluate(release, [], files=[changelog, {'filename': 'src/app.js', 'patch': '+x'}]), exento)
        self.assertNotEqual(prov.evaluate(release, [], files=[{'filename': 'package.json', 'patch': None}]), exento)
        self.assertNotEqual(prov.evaluate(release, [], files=None), exento)
        back = self.pr(head='chore/back-merge-evil')
        self.assertNotEqual(prov.evaluate(back, self.claude_commit), exento)
        self.assertEqual(prov.evaluate(back, self.claude_commit, in_main=True), exento)
        self.assertNotEqual(prov.evaluate(self.pr(head='develop', fork=True), []), exento)
        self.assertEqual(prov.evaluate(self.pr(head='develop'), []), exento)

    def test_pr_abierto_por_copilot_se_declara_solo(self):
        errors, labels = prov.evaluate(self.pr(user='Bot', login='copilot-swe-agent[bot]'), [])
        self.assertEqual((errors, labels), ([], {'ai:copilot'}))

    def test_copilot_por_login(self):
        _, labels = prov.evaluate(self.pr(n='x'), [{'commit': {'message': 'x'}, 'author': {'login': 'Copilot'}}])
        self.assertIn('ai:copilot', labels)


class RepoConfigTest(unittest.TestCase):
    def run_cfg(self, repo, root=None):
        tmp = tempfile.TemporaryDirectory()
        out = pathlib.Path(tmp.name) / 'out'
        old = dict(os.environ)
        os.environ.update({'GITHUB_REPOSITORY': f'org/{repo}', 'GITHUB_OUTPUT': str(out), 'RUNNER_TEMP': tmp.name})
        saved = repo_config.ROOT
        if root:
            repo_config.ROOT = root
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                repo_config.main()
            values = dict(line.split('=', 1) for line in out.read_text(encoding='utf-8').splitlines())
            allow = pathlib.Path(values['allow-file']).read_text(encoding='utf-8')
            return values, allow
        finally:
            repo_config.ROOT = saved
            os.environ.clear(); os.environ.update(old)
            tmp.cleanup()

    def test_repo_sin_config_usa_defectos(self):
        values, allow = self.run_cfg('otro-repo')
        self.assertEqual(values['coverage-min'], '80.0')
        self.assertEqual(values['node-version'], '24')
        self.assertEqual(allow.strip(), '')

    def test_excepciones_del_repo(self):
        values, allow = self.run_cfg('mi-app')
        self.assertIn('src/server.js  diff-coverage', allow)

    def test_config_invalida_se_rechaza(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / '_default.json').write_text('{"coverage_min": 80}', encoding='utf-8')
            (root / 'x.json').write_text('{"coverage_minimo": 10}', encoding='utf-8')
            with self.assertRaises(SystemExit):
                self.run_cfg('x', root)
            (root / 'x.json').write_text('{"exceptions": [{"path": "a.js", "rule": "todo"}]}', encoding='utf-8')
            with self.assertRaises(SystemExit):
                self.run_cfg('x', root)

class QualityTest(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.r.write('src/base.js', 'export const a = 1;\n')
        self.r.write('package.json', '{"name": "x", "dependencies": {"left-pad": "^1.3.0"}}\n')
        self.r.write('requirements.txt', 'requests==2.32.0\n')
        self.base = self.r.commit('base')

    def tearDown(self):
        self.r.close()

    def test_duplicacion_solo_cuenta_lineas_cambiadas(self):
        changed = {'src/a.js': set(range(1, 21)), 'test/a.test.js': {1, 2, 3}}
        report = {'duplicates': [
            {'lines': 6, 'firstFile': {'name': str(self.r.root / 'src' / 'a.js'), 'start': 1, 'end': 6},
             'secondFile': {'name': str(self.r.root / 'src' / 'old.js'), 'start': 10, 'end': 15}},
            {'lines': 8, 'firstFile': {'name': 'src/old.js', 'start': 1, 'end': 8},
             'secondFile': {'name': 'src/other.js', 'start': 1, 'end': 8}}]}
        pct, lines, total, clones = quality.duplication(report, changed, [])
        self.assertEqual((lines, total, len(clones)), (6, 20, 1))   # el test no cuenta; el clon antiguo tampoco
        self.assertAlmostEqual(pct, 30.0)
        pct, *_ = quality.duplication(report, changed, [('src/a.js', 'duplication')])
        self.assertEqual(pct, 0.0)

    def test_complejidad_solo_funciones_tocadas(self):
        csv_text = ('7,18,56,1,30,"grande@1-30@src/a.js","src/a.js","grande","grande ( x )",1,30\n'
                    '7,20,56,1,10,"vieja@40-50@src/a.js","src/a.js","vieja","vieja ( )",40,50\n'
                    '3,2,20,0,5,"simple@60-64@src/a.js","src/a.js","simple","simple ( )",60,64\n')
        found = quality.complexity(csv_text, {'src/a.js': {5, 61}}, 15)
        self.assertEqual([f[3] for f in found], ['grande'])

    def test_tamano_ignora_lockfiles_y_docs(self):
        numstat = '300\t50\tsrc/a.js\n5000\t0\tpackage-lock.json\n800\t0\tdocs/guia.md\n-\t-\timg.png\n'
        self.assertEqual(quality.pr_size(numstat, []), 350)

    def test_dependencias_nuevas_inventadas_o_recientes(self):
        self.r.write('package.json', '{"name": "x", "dependencies": {"left-pad": "^1.3.0", "lodash": "^4", "reqeusts-helper": "1.0.0", "local": "file:../x"}}\n')
        self.r.write('requirements.txt', 'requests==2.32.0\nfresh-pkg==0.1  # nuevo\n')
        self.r.commit('head')
        found = quality.new_dependencies(self.base)
        self.assertEqual(sorted(n for _, n, _ in found), ['fresh-pkg', 'lodash', 'reqeusts-helper'])
        now = quality.dt.datetime(2026, 11, 1, tzinfo=quality.dt.timezone.utc)
        dates = {'lodash': quality.dt.datetime(2012, 4, 23, tzinfo=quality.dt.timezone.utc),
                 'fresh-pkg': quality.dt.datetime(2026, 10, 28, tzinfo=quality.dt.timezone.utc)}
        rep = quality.Report()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            quality.check_dependencies(rep, self.base, [], 30, lookup=lambda eco, n: dates.get(n), now=now)
        self.assertTrue(rep.blocking)
        self.assertIn('2 rechazada', rep.rows[0][2])
        self.assertIn('no existe en npm', out.getvalue())
        rep = quality.Report()
        with contextlib.redirect_stdout(io.StringIO()):
            quality.check_dependencies(rep, self.base, [('reqeusts-helper', 'new-dependency'), ('fresh-pkg', 'new-dependency')], 30,
                                       lookup=lambda eco, n: dates.get(n), now=now)
        self.assertFalse(rep.blocking)

    def test_stubs_y_abstractmethod(self):
        self.r.write('src/servicio.py', (
            'from abc import ABC, abstractmethod\n\n'
            'class Base(ABC):\n'
            '    @abstractmethod\n'
            '    def area(self):\n'
            '        raise NotImplementedError\n\n'
            'def calcular(x):\n'
            '    raise NotImplementedError("luego")\n\n'
            '# TODO: revisar redondeo\n'))
        self.r.write('src/api.js', "export function pagar() { throw new Error('Not implemented'); }\n")
        self.r.write('tests/test_s.py', 'def test_x():\n    raise NotImplementedError\n')
        self.r.commit('head')
        block, warn = quality.stubs(quality.added_text(self.base), [])
        self.assertEqual(sorted((p, n) for p, n, _ in block), [('src/api.js', 1), ('src/servicio.py', 9)])
        self.assertEqual([(p, n) for p, n, _ in warn], [('src/servicio.py', 11)])

    def test_lizard_roto_es_error_no_ok(self):
        from unittest import mock
        self.r.write('src/a.py', 'def f():\n    return 1\n')
        self.r.commit('head')
        fake = subprocess.CompletedProcess([], 2, stdout='', stderr='boom')
        rep = quality.Report()
        with mock.patch.object(quality.subprocess, 'run', return_value=fake), contextlib.redirect_stdout(io.StringIO()):
            quality.check_complexity(rep, {'src/a.py': {1}}, [], 15, 'warn')
        self.assertEqual(rep.rows[0][1], 'ERROR')
        self.assertTrue(rep.blocking)

    def test_black_no_se_confunde_con_ruff(self):
        self.r.write('pyproject.toml', '[tool.black]\nline-length = 120\n')
        self.assertEqual(quality.formatter('python'), 'black')
        self.r.write('ruff.toml', 'line-length = 120\n')
        self.assertEqual(quality.formatter('python'), 'ruff')

    def test_stub_en_ruta_con_acentos(self):
        self.r.write('src/café.py', 'def f():\n    raise NotImplementedError\n')
        self.r.commit('head')
        block, _ = quality.stubs(quality.added_text(self.base), [])
        self.assertEqual([(p, n) for p, n, _ in block], [('src/café.py', 2)])
        self.assertIn('src/café.py', quality.changed_lines(self.base))

    def test_config_de_calidad_invalida(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / '_default.json').write_text('{"quality": {"complexity_max": 15}}', encoding='utf-8')
            (root / 'x.json').write_text('{"quality": {"complexity_mode": "estricto"}}', encoding='utf-8')
            with self.assertRaises(SystemExit):
                RepoConfigTest.run_cfg(self, 'x', root)
            (root / 'x.json').write_text('{"quality": {"pr_size_max": true}}', encoding='utf-8')
            with self.assertRaises(SystemExit):
                RepoConfigTest.run_cfg(self, 'x', root)


class PersonalModeTest(unittest.TestCase):
    def test_regla_de_organizacion_a_regla_de_repo(self):
        spec = importlib.util.spec_from_file_location('platform_admin', ACTIONS.parent / 'scripts' / 'platform_admin.py')
        admin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(admin)
        org = json.loads((ACTIONS.parent / 'rulesets' / 'org-develop.json').read_text(encoding='utf-8'))
        repo = admin.personal_ruleset(org, 0)
        self.assertEqual(set(repo['conditions']), {'ref_name'})
        pr = next(r for r in repo['rules'] if r['type'] == 'pull_request')['parameters']
        self.assertNotIn('required_reviewers', pr)
        self.assertEqual(pr['required_approving_review_count'], 0)
        checks = next(r for r in repo['rules'] if r['type'] == 'required_status_checks')['parameters']
        self.assertEqual(checks['required_status_checks'][0]['context'], 'pipeline / ci-gate')
        self.assertIn('required_reviewers', next(r for r in org['rules'] if r['type'] == 'pull_request')['parameters'])
        # Con 0 aprobaciones no puede exigirse "aprobación del último push" (bloquearía a una sola persona)
        self.assertFalse(pr['require_last_push_approval'])
        pr1 = next(r for r in admin.personal_ruleset(org, 1)['rules'] if r['type'] == 'pull_request')['parameters']
        self.assertTrue(pr1['require_last_push_approval'])

    def test_releases_centrales_solo_sin_llamador(self):
        spec = importlib.util.spec_from_file_location('release_targets', ACTIONS.parent / 'scripts' / 'release_targets.py')
        rt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rt)
        pages = [[{'repository_name': 'a', 'properties': [{'property_name': 'pipeline', 'value': 'agentica'}]},
                  {'repository_name': 'b', 'properties': [{'property_name': 'pipeline', 'value': 'agentica'}]},
                  {'repository_name': 'c', 'properties': [{'property_name': 'pipeline', 'value': None}]}],
                 [{'repository_name': 'd', 'properties': [{'property_name': 'pipeline', 'value': 'agentica'}]}]]
        names = list(rt.enrolled(pages))
        self.assertEqual(names, ['a', 'b', 'd'])
        out = rt.targets('org', names, caller=lambda o, r: r == 'b', rtype=lambda r: 'none' if r == 'd' else 'node')
        self.assertEqual(out, [{'repo': 'org/a', 'release_type': 'node'}])
        from unittest import mock
        forbidden = subprocess.CompletedProcess([], 1, stdout='', stderr='gh: Resource not accessible (HTTP 403)')
        missing = subprocess.CompletedProcess([], 1, stdout='', stderr='gh: Not Found (HTTP 404)')
        with mock.patch.object(rt.subprocess, 'run', return_value=missing):
            self.assertFalse(rt.has_caller('org', 'a'))
        with mock.patch.object(rt.subprocess, 'run', return_value=forbidden):
            with self.assertRaises(RuntimeError):
                rt.has_caller('org', 'a')
        with mock.patch.dict(os.environ, {'ORG': 'org'}), mock.patch.object(rt, 'account_type', return_value='Organization'), \
                mock.patch.object(rt, 'gh', side_effect=RuntimeError('HTTP 403')), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                rt.main()
            self.assertIn('Releases centrales', str(ctx.exception.code))

    def test_reglas_del_repo_central_en_cuenta_personal(self):
        spec = importlib.util.spec_from_file_location('platform_admin', ACTIONS.parent / 'scripts' / 'platform_admin.py')
        admin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(admin)
        own = json.loads((ACTIONS.parent / 'rulesets' / 'platform-repo.json').read_text(encoding='utf-8'))
        branch, tags = admin.personal_platform_rules(own)
        self.assertEqual(tags['bypass_actors'], [{'actor_id': 5, 'actor_type': 'RepositoryRole', 'bypass_mode': 'always'}])
        self.assertEqual(own['tags']['bypass_actors'][0]['actor_type'], 'OrganizationAdmin')
        pr = next(r for r in branch['rules'] if r['type'] == 'pull_request')['parameters']
        self.assertEqual((pr['required_approving_review_count'], pr['require_last_push_approval']), (0, False))
        push = json.loads((ACTIONS.parent / 'rulesets' / 'org-push-workflows.json').read_text(encoding='utf-8'))
        self.assertTrue(all(a.get('actor_id') == 1 for a in push['bypass_actors'] if a['actor_type'] == 'OrganizationAdmin'))


class FakeGitHub:
    """Sustituye a subprocess.run de platform_admin: responde a `gh api` y registra las escrituras."""
    def __init__(self, routes):
        self.routes, self.writes = routes, []

    def __call__(self, cmd, input=None, **_):
        path = cmd[2]
        method = cmd[cmd.index('--method') + 1] if '--method' in cmd else 'GET'
        if method != 'GET':
            self.writes.append((method, path, json.loads(input) if input else None))
        for (m, prefix), answer in self.routes.items():
            if m == method and path.startswith(prefix):
                if answer == 404:
                    return subprocess.CompletedProcess(cmd, 1, stdout='', stderr='gh: Not Found (HTTP 404)')
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(answer), stderr='')
        if method == 'GET':
            return subprocess.CompletedProcess(cmd, 1, stdout='', stderr='gh: Not Found (HTTP 404)')
        return subprocess.CompletedProcess(cmd, 0, stdout='{}', stderr='')


class EnrollTest(unittest.TestCase):
    """Alta de repos: uno nuevo o vacío no necesita PR ni código del proyecto; uno con código, sí."""

    def setUp(self):
        import argparse
        import shutil
        spec = importlib.util.spec_from_file_location('platform_admin', ACTIONS.parent / 'scripts' / 'platform_admin.py')
        self.admin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.admin)
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        for folder in ('caller', 'org-dot-github', 'rulesets', 'repos'):
            shutil.copytree(ACTIONS.parent / folder, root / folder)
        caller = root / 'caller' / 'agentic-ci.yml'
        caller.write_text(caller.read_text(encoding='utf-8').replace('TU-' + 'ORG', 'yo'), encoding='utf-8')  # 'TU-' + 'ORG': configure no la reescribe
        self.admin.ROOT = root
        self.args = lambda **kw: argparse.Namespace(**{'org': 'yo', 'repo': 'nueva', 'personal': True, 'enterprise': False,
                                                        'approvals': 0, 'apply': True, 'no_develop': False, **kw})

    def tearDown(self):
        self.tmp.cleanup()

    def run_enroll(self, routes, **kw):
        from unittest import mock
        fake = FakeGitHub(routes)
        with mock.patch.object(self.admin.subprocess, 'run', side_effect=fake), contextlib.redirect_stdout(io.StringIO()):
            self.admin.cmd_enroll(self.args(**kw))
        return fake.writes

    def test_repo_nuevo_se_crea_y_recibe_el_llamador_sin_pr_antes_de_las_reglas(self):
        writes = self.run_enroll({
            ('GET', 'repos/yo/nueva/git/ref/heads/main'): {'object': {'sha': 'abc'}},
            ('GET', 'repos/yo/nueva/rulesets'): [],
            ('GET', 'repos/yo/nueva'): 404, ('GET', 'user'): {'login': 'yo'},
            ('POST', 'user/repos'): {'visibility': 'public', 'default_branch': 'main'}})
        paths = [(m, p) for m, p, _ in writes]
        self.assertEqual(paths[0], ('POST', 'user/repos'))
        self.assertFalse(writes[0][2]['private'])
        puts = [(p, d) for m, p, d in writes if m == 'PUT' and '/contents/' in p]
        self.assertEqual([p.split('/contents/')[1] for p, _ in puts],
                         ['.github/workflows/agentic-ci.yml', '.github/pull_request_template.md', 'AGENTS.md'])
        self.assertTrue(all('branch' not in d for _, d in puts))  # van a la rama por defecto
        first_rule = paths.index(('POST', 'repos/yo/nueva/rulesets'))
        self.assertLess(paths.index(('PUT', 'repos/yo/nueva/contents/AGENTS.md')), first_rule)
        self.assertIn(('POST', 'repos/yo/nueva/git/refs'), paths[:first_rule])
        self.assertIn({'default_branch': 'develop'}, [d for m, p, d in writes if p == 'repos/yo/nueva'])
        self.assertNotIn(('POST', 'repos/yo/nueva/pulls'), paths)

    def test_repo_vacio_existente_sin_develop(self):
        writes = self.run_enroll({('GET', 'repos/yo/nueva/branches'): [], ('GET', 'repos/yo/nueva/rulesets'): [],
                                  ('GET', 'repos/yo/nueva'): {'visibility': 'public', 'default_branch': 'main'}},
                                 no_develop=True)
        paths = [(m, p) for m, p, _ in writes]
        self.assertNotIn(('POST', 'user/repos'), paths)
        self.assertNotIn(('POST', 'repos/yo/nueva/git/refs'), paths)
        self.assertNotIn(('POST', 'repos/yo/nueva/pulls'), paths)
        self.assertIn(('PUT', 'repos/yo/nueva/contents/.github/workflows/agentic-ci.yml'), paths)

    def test_repo_con_codigo_recibe_el_llamador_por_pr_y_sin_agents(self):
        writes = self.run_enroll({
            ('GET', 'repos/yo/nueva/branches'): [{'name': 'main'}],
            ('GET', 'repos/yo/nueva/git/ref/heads/main'): {'object': {'sha': 'abc'}},
            ('GET', 'repos/yo/nueva/git/ref/heads/platform'): 404,
            ('GET', 'repos/yo/nueva/contents/'): 404, ('GET', 'repos/yo/nueva/pulls'): [],
            ('GET', 'repos/yo/nueva/rulesets'): [],
            ('GET', 'repos/yo/nueva'): {'visibility': 'public', 'default_branch': 'main'},
            ('POST', 'repos/yo/nueva/pulls'): {'html_url': 'https://github.com/yo/nueva/pull/1'}})
        puts = [(p, d) for m, p, d in writes if m == 'PUT' and '/contents/' in p]
        self.assertTrue(puts and all(d['branch'] == 'platform/agentic-ci' for _, d in puts))
        self.assertNotIn('AGENTS.md', [p.split('/contents/')[1] for p, _ in puts])
        self.assertIn(('POST', 'repos/yo/nueva/pulls'), [(m, p) for m, p, _ in writes])

    def test_simulacion_de_repo_nuevo_no_escribe_nada(self):
        writes = self.run_enroll({('GET', 'repos/yo/nueva'): 404, ('GET', 'user'): {'login': 'yo'}}, apply=False)
        self.assertEqual(writes, [])

    def test_personal_exige_que_org_sea_tu_usuario(self):
        with self.assertRaises(SystemExit):
            self.run_enroll({('GET', 'repos/yo/nueva'): 404, ('GET', 'user'): {'login': 'otro'}})


if __name__ == '__main__':
    unittest.main()
