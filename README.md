# platform-pipeline · el pipeline agéntico, centralizado

> Paso a paso para montarlo y aprenderlo: **guía V3** (`guide/guia-agentic-cicd-centralizado-v3.pdf` en el repositorio de referencia) y el mapa interactivo con voz (`guide/mapa-pipeline-agentico.html`).

Este repositorio contiene **todas las normas y todo el pipeline** de los repos de código de la organización. Solo lo cambian los administradores. Los desarrolladores **no configuran nada** en sus repos: abren PRs y el pipeline central los comprueba.

```
carlos-anpr/platform-pipeline   (PRIVADO · escritura: solo administradores)
├── .github/workflows/agentic-ci.yml   ← el pipeline: 8 checks + ci-gate (reusable workflow)
├── .github/workflows/required.yml     ← entrada directa para Enterprise Cloud (sin archivo en los repos)
├── .github/workflows/self-test.yml    ← el CI de este repo
├── .github/workflows/release-central.yml ← (Enterprise sin archivo llamador) releases de todos los repos
├── actions/                            ← la lógica: quality, guardrails, diff-coverage, acceptance, provenance,
│                                          security-scan, run-project (detecta Node/Python), repo-config
├── repos/_default.json                 ← configuración para todos los repos
├── repos/<repo>.json                   ← lo específico de un repo (cobertura, excepciones, comandos)
├── rulesets/                           ← las normas: reglas de organización y de este repo
├── caller/agentic-ci.yml               ← (plan Team) el archivo de 15 líneas que el admin instala
├── caller/AGENTS.md                    ← AGENTS.md inicial que recibe cada repo nuevo
├── org-dot-github/                     ← plantillas de PR e issue para el repo público carlos-anpr/.github
├── scripts/platform_admin.py           ← configure · bootstrap · enroll (se puede repetir sin miedo)
├── scripts/release_targets.py          ← qué repos publica release-central.yml
└── tests/                              ← 44 pruebas de los checks, la configuración y el modo prueba
```

## Qué hace el pipeline en cada PR

| Check | Qué comprueba |
|---|---|
| `commits` | Título del PR en Conventional Commits |
| `build-test` | Instala, build y tests **con cobertura**, detectando el proyecto sin configuración (Node: scripts de `package.json`, Vitest, Jest o `node --test`; Python: pytest + pytest-cov), y **cobertura ≥ 80 % de las líneas cambiadas** |
| `quality` | **Calidad del código cambiado**, 7 reglas (detalle abajo): lint, formato, duplicación, complejidad, tamaño del PR, dependencias nuevas y código sin implementar |
| `guardrails` | Que el cambio no desactive tests (`.skip`/`.only`), no borre ni renombre tests, no borre aserciones, no añada supresiones, no rebaje umbrales de cobertura y **no cambie la configuración que decide qué se comprueba** (`policy-config`: lint, formato, tests, cobertura y escáneres; detalle abajo) |
| `acceptance` | Cada criterio `AC-n` de la issue o del PR aparece en el **nombre** de un test (`it('AC-1: …')`, `def test_ac1_…`; un comentario no vale); un PR `feat`/`fix` debe declarar criterios |
| `provenance` | El PR declara qué agente de IA se usó y no contradice las firmas `Co-authored-by` de los commits |
| `security` | gitleaks (secretos), **Semgrep CE** (análisis estático del código, open source), OSV-Scanner (dependencias) y zizmor (workflows), con versiones fijadas y verificadas. gitleaks usa la **configuración central** (`actions/security-scan/gitleaks.toml`) e ignora `.gitleaks.toml`, `.gitleaksignore` y los comentarios `gitleaks:allow` del repo; OSV no se deja esconder lockfiles por `.gitignore`. Funciona igual en cualquier plan: no depende de CodeQL, que en repos privados es de pago |
| `smoke` | La app arranca y responde (`npm start` o el comando configurado) |
| `provenance` · exenciones | Solo Dependabot y Renovate, los PRs de release que tocan únicamente versión y CHANGELOG, los back-merge cuyo contenido ya está en `main` y `develop`→`main`. Un PR abierto por Copilot se declara solo |
| **`ci-gate`** | Agrega los ocho. Es el único check que exigen las reglas: **`pipeline / ci-gate`** |
| `labels` | (no bloquea) Etiqueta el PR con `ai:<agente>` para medir |
| `release` | (no bloquea; solo al cambiar `main` y **con `ci-gate` en verde**) release-please abre o actualiza el PR de release con versión semver y CHANGELOG; al mergearlo crea el tag y la GitHub Release. Tipo por repo en `repos/<repo>.json` → `release_type` (`node`, `python`, `simple`, `none`). En Enterprise sin archivo llamador lo hace `release-central.yml` cada hora |

El pipeline se relanza también al **editar el título o la descripción** del PR, para que `commits`, `acceptance` y `provenance` comprueben siempre el texto actual.

Nada de esto se puede tocar desde un repo de código: la lógica y las excepciones viven aquí.

### Las 7 reglas de `quality`

Todas miran **solo lo que cambia el PR**: el código antiguo no bloquea a nadie. Las cinco reglas que no necesitan el proyecto (duplicación, complejidad, tamaño, dependencias nuevas y stubs) se ejecutan **antes de instalar o ejecutar nada del PR**; después se instala y se pasan lint y formato. Están pensadas para lo que más falla en el código escrito por agentes de IA (copiar en vez de reutilizar, funciones enormes, PRs gigantes, paquetes inventados y funciones a medio hacer).

| Regla | Qué hace | Por defecto | Umbral (`quality` en `repos/*.json`) |
|---|---|---|---|
| `lint` | Pasa el linter del proyecto (`commands.lint` o script `lint`). Si no tiene, **Biome 2.5.15** (JS/TS) o **Ruff 0.17.1** (Python) con sus reglas recomendadas sobre los archivos cambiados. Otros lenguajes deben declarar `commands.lint` | bloquea si hay errores (los avisos no) | — |
| `format` | Si el proyecto tiene formateador (Prettier, Biome, Ruff format o **Black 26.10.1** si solo declara `[tool.black]`), los archivos cambiados deben estar formateados. Si no tiene, avisa | bloquea / aviso | `format_required: true` exige tener formateador |
| `duplication` | **jscpd 5.4.1**: % de líneas cambiadas que forman parte de un bloque copiado (≥ 5 líneas y 50 tokens), sin contar tests | bloquea | `duplication_max: 3` (%) |
| `complexity` | **lizard 1.24.1**: complejidad ciclomática de cada función tocada | **solo avisa** (primeras semanas) | `complexity_max: 15`, `complexity_mode: warn \| block \| off` |
| `pr-size` | Líneas cambiadas, sin contar lockfiles, generados ni documentación | aviso > 400, bloquea > 1500 | `pr_size_warn`, `pr_size_max` |
| `new-dependency` | Cada paquete nuevo en `package.json`, `requirements*.txt` o `pyproject.toml` debe **existir** en npm/PyPI y tener **≥ 30 días**. Para los nombres inventados por un agente que un atacante registra después (*slopsquatting*) | bloquea | `new_dependency_min_age_days: 30` |
| `stub` | Código de producción sin implementar: `raise NotImplementedError`, `throw new Error('Not implemented')`, `NotImplementedException`, `todo!()`… (no cuenta tests ni `@abstractmethod`). `TODO`/`FIXME` nuevos solo avisan | bloquea / aviso | — |

Ejemplo de excepciones y umbrales para un repo:

```json
{
  "quality": {"complexity_mode": "block", "pr_size_max": 2500},
  "exceptions": [
    {"path": "src/generated/**", "rule": "duplication", "reason": "código generado por OpenAPI"},
    {"path": "left-pad", "rule": "new-dependency", "reason": "aprobado por tech-leads, issue #42"}
  ]
}
```

### `policy-config`: la configuración de los checks también es una norma

Un PR (o un agente) podría relajar los checks sin tocar este repo: un `.semgrepignore` que excluye `src/`, un `"lint": "exit 0"` en `package.json` o un `select = []` en Ruff. Por eso `guardrails` bloquea cualquier cambio en:

- `.eslintrc*`, `eslint.config.*`, `biome.json(c)`, `.prettierrc*`, `prettier.config.*`, `ruff.toml`, `.flake8`, `.pylintrc`, `mypy.ini`
- `jest|vitest|karma.config.*`, `.mocharc*`, `.nycrc*`, `.c8rc*`, `.coveragerc`, `pytest.ini`, `tox.ini`, `setup.cfg`, `codecov.yml`, y la sección `test` de `vite.config.*`
- `.gitleaks.toml`, `.gitleaksignore`, `.semgrepignore`, `.semgrep.yml`, `osv-scanner.toml`, `.trivyignore`
- en `package.json`, los scripts `lint`, `test`, `coverage`, `pretest`, `posttest`, `format`, `check`, `typecheck` y las claves `jest`, `c8`, `nyc`, `mocha`, `ava`, `eslintConfig`, `prettier`, `vitest` (subir la versión o añadir dependencias **no** cuenta)
- en `pyproject.toml`, `[tool.ruff|black|pytest|coverage|mypy|pylint|flake8|isort]`
- `.jscpd.json` y `.config/jscpd.json` (además, jscpd usa siempre la configuración central) y los marcadores `jscpd:ignore-start` y `gitleaks:allow` (como supresiones)
- **los archivos que ejecutan esos scripts**: si `"lint": "node tools/lint.js"`, cambiar `tools/lint.js` también cuenta

Se vigila también al **borrar o renombrar** cualquiera de ellos (borrar `pyproject.toml` con `[tool.ruff]` relaja el lint).

**Proyecto nuevo:** si el commit base no tiene ningún manifiesto (`package.json`, `pyproject.toml`, `requirements*.txt`, `go.mod`, `pom.xml`…), como pasa en el primer PR de un repo recién dado de alta, **crear** esa configuración es solo un **aviso** en el PR: no hay nada que debilitar. Cambiarla o borrarla después bloquea como siempre.

Si el cambio es legítimo (p. ej. añadir un formateador), plataforma añade `{"path": "biome.json", "rule": "policy-config", "reason": "…"}` en `repos/<repo>.json`, publica `v1` y, cuando el PR entra, puede retirar la excepción. En una organización, estos archivos también piden revisión del equipo `platform` (rulesets).

**Falsos positivos de gitleaks**: plataforma los aprueba en `actions/security-scan/gitleaks.toml` (`[allowlist]` con la ruta o el patrón, y un comentario con el repo y el motivo) y publica `v1`. Si un repo traía un `.gitleaksignore`, hay que pasar sus entradas ahí.

> 🆓 Todas las herramientas son open source y gratuitas, en cualquier plan. Se instalan con versión fijada en cada ejecución, desde el registro público (un `.npmrc` del PR no puede redirigirlas); `npm_config_ignore_scripts` evita que ejecuten scripts de instalación. La comprobación de dependencias consulta las APIs públicas de npm y PyPI; si no responden, la regla da error en vez de pasar en silencio.

## Quién ve y quién toca qué

| | Administradores | Equipo `platform` | Desarrolladores |
|---|---|---|---|
| Este repo | escribir y publicar versiones (`v1`) | leer (o escribir, si queréis delegar) | **leer** (recomendado, para entender por qué falla algo) o nada |
| Logs y resultados del CI | sí | sí | sí, en el PR de su repo |
| `.github/workflows/` de los repos de código | sí (bypass) | no | **no**: el ruleset de push lo bloquea |
| Reglas de rama de los repos de código | sí (rulesets de organización) | no | no |

> El repo es privado. GitHub permite que los workflows de los repos privados de la organización lo usen sin dar acceso de lectura a nadie: *Settings → Actions → General → Access → "Accessible from repositories in the organization"* (lo activa `bootstrap`). Aviso de GitHub: los colaboradores externos de un repo de código pueden ver los logs de estos workflows.

## Plan Team o Enterprise Cloud

| | Team | Enterprise Cloud |
|---|---|---|
| Archivos en el repo de código | **uno**, `.github/workflows/agentic-ci.yml` (15 líneas). Lo instala el admin con `enroll` y los devs **no pueden modificarlo** | **ninguno** |
| Cómo se ejecuta | el archivo llamador usa `agentic-ci.yml@v1` | la regla de organización *"Require workflows to pass"* ejecuta `required.yml@v1` |
| Ruleset extra | — | `rulesets/org-required-workflow.json` |

## Puesta en marcha (una vez)

Requisitos: ser **owner** de la organización, `gh auth login`, Python 3.

1. **Equipos**: crea `tech-leads` (aprueban el código; mínimo 2 personas) y `platform` (aprueban `.github/`, `AGENTS.md` y los umbrales de cobertura; puede ser el mismo grupo). Ambos con permiso *Write* en los repos de código.
2. **Este repo**: créalo **privado** como `carlos-anpr/platform-pipeline`, copia esta carpeta y personaliza la organización:
   ```bash
   python scripts/platform_admin.py configure --org carlos-anpr
   git add -A && git commit -m "feat: pipeline central" && git push
   python -m unittest discover -s tests       # deben pasar las 44 pruebas
   ```
3. **Primera versión**: `git tag v1 && git push origin v1`.
4. **Normas** (simulación y después aplicar):
   ```bash
   python scripts/platform_admin.py bootstrap --org carlos-anpr            # muestra lo que haría
   python scripts/platform_admin.py bootstrap --org carlos-anpr --apply    # Enterprise: añade --enterprise
   ```
   Crea la propiedad de repo `pipeline`, comparte este repo con la organización, protege su `main` y sus etiquetas `v*`, y crea las reglas de organización: `plataforma-develop`, `plataforma-main` y `plataforma-workflows-bloqueados` (y, en Enterprise, `plataforma-pipeline-requerido`).
5. **Plantillas de PR e issue**: crea el repo **público** `carlos-anpr/.github` con el contenido de `org-dot-github/`. GitHub exige que sea público; solo contiene plantillas. La plantilla de PR incluye la sección *Agente de IA usado*, que comprueba `provenance`.
6. **Releases**: crea el secreto de organización `RELEASE_PLEASE_TOKEN` (token *fine-grained* de una cuenta de servicio o de un admin, con *Contents* y *Pull requests* en lectura y escritura sobre los repos de código). Hace falta un token propio porque los PRs creados con `GITHUB_TOKEN` no disparan el CI. Sin él, el pipeline funciona igual pero no publica releases. **Enterprise sin archivo llamador**: dale además el permiso de organización *Custom properties: read*, da acceso al secreto también a este repo y `release-central.yml` preparará cada hora los releases de los repos dados de alta.
7. **Opcional**: en las reglas `plataforma-develop` y `plataforma-main`, activa *Automatically request Copilot code review* (si tenéis Copilot). Y activa el bloqueo de secretos para toda la organización en *Settings → Code security → Configurations*.

## Dar de alta un repo de código

```bash
python scripts/platform_admin.py enroll --org carlos-anpr --repo mi-app            # simulación
python scripts/platform_admin.py enroll --org carlos-anpr --repo mi-app --apply    # Enterprise: añade --enterprise
```

**Proyecto nuevo (lo normal):** plataforma da de alta el repo y los desarrolladores solo suben código, como en GitLab.

1. Si el repo **no existe**, lo crea vacío (privado en una organización, público con `--personal`).
2. Si está **vacío**, el archivo llamador y un `AGENTS.md` inicial (y con `--personal` la plantilla de PR) entran como **primer commit directo, sin PR** y sin código del proyecto. Crea `develop` y la deja como rama por defecto (`--no-develop` para trabajar solo con `main`).
3. Después marca el repo con `pipeline=agentica` (desde ese momento le aplican las reglas), activa el auto-merge y el bloqueo de secretos al hacer push y crea las etiquetas `ai:*`.

Los desarrolladores clonan, crean su rama, suben código y abren su PR: el pipeline salta solo. Plataforma no ve ni mergea código.

**Repo que ya tiene código (migración):** el llamador llega en un PR que solo contiene archivos de plataforma. Cuando `pipeline / ci-gate` esté en verde, mergéalo. Se puede repetir (p. ej. tras un fallo a medias o para cambiar `--approvals`): reutiliza la rama, los archivos y el PR que ya existan.

Para que **ningún** repo quede fuera del pipeline, limita en la organización quién crea repos (*Settings → Member privileges → Repository creation*): así todos nacen con `enroll`.

Lo único que el proyecto necesita **es suyo, no del pipeline**: un lockfile (`package-lock.json`), sus scripts normales (`test`, `start`) y tests. Recomendado, no obligatorio: un `AGENTS.md` con las reglas para sus agentes de IA.

## Probar gratis (cuenta personal, repos públicos)

Para ver el flujo completo sin plan de pago: el repo central y el de la app, **públicos** en tu cuenta personal, y solo con código de prueba.

```bash
python scripts/platform_admin.py configure --org TU-USUARIO
# sube el repo platform-pipeline (público) y crea la etiqueta v1
python scripts/platform_admin.py bootstrap --org TU-USUARIO --personal --apply
python scripts/platform_admin.py enroll    --org TU-USUARIO --repo mi-app --personal --apply
```

Diferencias con el modo de organización: las reglas de rama van en el propio repo de la app (con 0 aprobaciones y sin "aprobación del último push", porque no puedes aprobar tus propios PRs), la excepción de las etiquetas `v*` es el rol admin del repo, no hay revisores por equipo ni bloqueo de `.github/workflows/`, y la plantilla de PR va en el mismo PR del archivo llamador. **El pipeline y sus checks son exactamente los mismos.**

## Cambiar normas o configuración

Todo por PR **en este repo** y, tras el merge, publicando versión:

| Quiero… | Dónde |
|---|---|
| Cambiar la cobertura mínima de un repo, o de todos | `repos/<repo>.json` → `coverage_min` (o `_default.json`) |
| Permitir una excepción justificada (p. ej. código generado con `eslint-disable`) | `repos/<repo>.json` → `exceptions: [{path, rule, reason}]` |
| Endurecer o relajar una regla de calidad | `repos/<repo>.json` → `quality` (o `_default.json`) |
| Un proyecto que no se detecta solo (Java, .NET, Go, monorepo…) | `repos/<repo>.json` → `commands: {install, lint, build, test}` (el de test debe dejar `coverage/lcov.info` o `coverage.xml`) |
| Arranque distinto para el smoke test | `repos/<repo>.json` → `smoke: {start, url, port}` |
| Tipo de release, o desactivarlos | `repos/<repo>.json` → `release_type` |
| Cambiar un check o añadir uno | `actions/` y `.github/workflows/agentic-ci.yml` |
| Cambiar quién aprueba qué, o los métodos de merge | `rulesets/*.json` + `bootstrap --apply` |

Reglas de excepción válidas: `test-skip`, `test-only`, `suppression`, `coverage-ignore`, `assert-removed`, `test-deleted`, `coverage-threshold`, `diff-coverage`, `lint`, `format`, `duplication`, `complexity`, `pr-size`, `new-dependency`, `stub`, `policy-config` o `*`. En `new-dependency`, `path` es el nombre del paquete.

## Publicar una versión (y volver atrás)

Los repos usan `@v1`. Publicar es mover esa etiqueta; solo pueden hacerlo los administradores.

```bash
git switch main && git pull
git tag v1.3.0 && git push origin v1.3.0      # versión fija, para poder volver
git tag -f v1 && git push -f origin v1        # todos los repos la usan en su próximo PR
# Volver atrás: git tag -f v1 v1.2.0 && git push -f origin v1
```

Un cambio incompatible (p. ej. un check nuevo que haría fallar PRs existentes) va en `v2`: avisas, actualizas el archivo llamador y retiras `v1` cuando todos hayan migrado.

## Límites conocidos

- **Plan Team**: un PR podría intentar modificar el archivo llamador, pero el ruleset de push lo rechaza para todos menos los administradores. En Enterprise Cloud no existe ni ese archivo.
- **Checks por nombre**: las reglas exigen el contexto `pipeline / ci-gate`. Si cambias el id del job `pipeline` en el archivo llamador o el nombre `ci-gate`, actualiza `rulesets/` en el mismo cambio.
- **Pendiente de centralizar**: el revisor de IA con Claude (`ai-review.yml`), los agentes de gh-aw (triage, ci-doctor) y el build firmado con despliegue GitOps (nivel 3). *Agent Merge* (Watch and fix) y la revisión de Copilot no necesitan configuración en el repo.
- **Calidad**: las herramientas se instalan con versión fijada pero sin hash (`pip`/`npx`). La duplicación y la complejidad solo cubren los lenguajes que soportan jscpd y lizard (la mayoría de los habituales).
- **Código hostil a propósito**: `build-test` ejecuta los tests del PR y después valida la cobertura en la misma máquina, así que un PR escrito *para engañar* (scripts que fabrican el informe de cobertura o alteran el validador) podría dar un verde falso. El pipeline frena los atajos de un agente (tests desactivados, configuración relajada, criterios sin test…); contra la mala fe deliberada, la defensa es la aprobación humana. Mitigado: las reglas estáticas de `quality` corren antes de ejecutar nada del PR y los informes de cobertura del repo se borran antes de los tests.
- **Instalación**: en Node se instala con `npm ci --ignore-scripts`; en Python, `pip` ejecuta el código de instalación de los paquetes (sdists) en `build-test` y `smoke`, en paralelo a la comprobación de dependencias nuevas. No se usan aún los lockfiles de uv o poetry.
- **Criterios**: `acceptance` exige que `AC-n` esté en el nombre de un test, pero no comprueba que el runner lo descubra; eso lo cubre la revisión. Si se editan los criterios en la **issue** enlazada, el check no se relanza solo (*Re-run jobs*).
- **`policy-config`** cubre la configuración conocida y los archivos que los scripts `lint`/`test`/`coverage` ejecutan directamente; no sigue cadenas de `import` arbitrarias. Las configuraciones que ya fueran laxas antes del alta las revisa plataforma al dar de alta el repo.
- **Admisión de dependencias**: si `quality` rechaza una dependencia nueva, no instala el proyecto (lint y formato se ejecutan cuando se resuelva).
- **Verificado en GitHub real (2026-11-02, modo `--personal`)**: el nombre exacto del check (`pipeline / ci-gate`) y que la regla de rama lo reconoce. **Pendiente**: que los rulesets acepten el evento `edited` en el workflow requerido (Enterprise).
- **Back-merge tras cada release**: el commit de release queda en `main`; devuélvelo a `develop` con un PR desde una rama `chore/back-merge-vX.Y.Z`.
- **Validación**: las 44 pruebas, actionlint y zizmor pasan, el modo "cero configuración" se probó de extremo a extremo con la app de ejemplo y las 7 reglas de `quality` se probaron en local con un PR correcto y otro con defectos (Node y Python). El job `quality` **aún no se ha ejecutado en GitHub Actions real**. Las llamadas de `platform_admin.py` a la API de GitHub siguen la especificación oficial, pero **no se han ejecutado contra una organización real**: haz la primera vez en simulación y revisa la salida.
