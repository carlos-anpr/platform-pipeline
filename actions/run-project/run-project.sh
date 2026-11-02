#!/usr/bin/env bash
# Ejecuta el proyecto SIN configuración en el repo del código: detecta Node o Python y usa
# sus scripts habituales. Los comandos especiales salen de repos/<repo>.json (plataforma).
# Uso: run-project.sh detect | setup | test | smoke
set -euo pipefail
TASK="$1"

run() { echo "+ $*"; bash -c "$*"; }
pkg_script() { node -e "const s=(require('./package.json').scripts)||{};process.stdout.write(s[process.argv[1]]||'')" "$1"; }
pkg_dep() { node -e "const p=require('./package.json');const d={...p.dependencies,...p.devDependencies};process.exit(d[process.argv[1]]?0:1)" "$1"; }

detect() {
  if [ -f package.json ]; then echo node
  elif [ -f pyproject.toml ] || [ -f requirements.txt ] || [ -f setup.py ]; then echo python
  else echo none; fi
}

install() {
  if [ -n "${CMD_INSTALL:-}" ]; then run "$CMD_INSTALL"; return; fi
  case "$STACK" in
    node)
      if [ -f package-lock.json ]; then run "npm ci --ignore-scripts"
      else echo "::error title=Proyecto::Falta package-lock.json: súbelo para que la instalación sea reproducible."; exit 1; fi ;;
    python)
      run "python -m pip install --disable-pip-version-check pip==26.2.1"
      for f in requirements.txt requirements-dev.txt; do
        if [ -f "$f" ]; then run "pip install -r $f"; fi
      done
      if [ -f setup.py ] || grep -q '^\[project\]' pyproject.toml 2>/dev/null; then run "pip install -e ."; fi
      run "pip install pytest==9.1.1 pytest-cov==7.1.0" ;;
  esac
}

case "$TASK" in
  detect)
    STACK=$(detect)
    echo "stack=$STACK" >> "$GITHUB_OUTPUT"
    echo "Proyecto detectado: $STACK" ;;

  setup)
    case "$STACK" in
      node|python) install ;;
      *) if [ -n "${CMD_INSTALL:-}" ]; then run "$CMD_INSTALL"; fi ;;
    esac ;;

  test)
    # El lint lo hace el job quality (obligatorio, con linter por defecto si el proyecto no tiene).
    # Se borran los informes de cobertura que vengan en el repo: solo vale el que generan estos tests.
    rm -rf coverage coverage.xml lcov.info .coverage
    case "$STACK" in
      node)
        install
        if [ -n "${CMD_BUILD:-}" ]; then run "$CMD_BUILD"; else run "npm run build --if-present"; fi
        mkdir -p coverage
        if [ -n "${CMD_TEST:-}" ]; then run "$CMD_TEST"
        elif [ -n "$(pkg_script coverage)" ]; then run "npm run coverage"
        elif pkg_dep vitest; then run "npx --no-install vitest run --coverage --coverage.reporter=lcov --coverage.reportsDirectory=coverage"
        elif pkg_dep jest; then run "npx --no-install jest --coverage --coverageReporters=lcov --coverageDirectory=coverage"
        elif pkg_script test | grep -q 'node --test'; then
          run "node --test --experimental-test-coverage --test-reporter=spec --test-reporter-destination=stdout --test-reporter=lcov --test-reporter-destination=coverage/lcov.info"
        else
          echo "::warning title=Cobertura::No sé generar cobertura para este proyecto; el check de cobertura fallará. Plataforma: define commands.test en repos/<repo>.json."
          run "npm test"
        fi ;;
      python)
        install
        if [ -n "${CMD_BUILD:-}" ]; then run "$CMD_BUILD"; fi
        if [ -n "${CMD_TEST:-}" ]; then run "$CMD_TEST"
        else run "python -m pytest --cov --cov-report=xml:coverage.xml"; fi ;;
      none)
        if [ -n "${CMD_TEST:-}" ]; then
          if [ -n "${CMD_INSTALL:-}" ]; then run "$CMD_INSTALL"; fi
          if [ -n "${CMD_BUILD:-}" ]; then run "$CMD_BUILD"; fi
          run "$CMD_TEST"
        elif git ls-files | grep -Eq '\.(java|kt|cs|go|rb|php|rs|swift|scala|cpp|c)$'; then
          echo "::error title=Proyecto no soportado::Hay código pero no es Node ni Python. Plataforma: define commands (install, build, test) en repos/<repo>.json."
          exit 1
        else
          echo "::notice::No hay código ejecutable (solo documentación o configuración): no hay tests que ejecutar."
        fi ;;
    esac ;;

  smoke)
    START="${SMOKE_START:-}"
    if [ -z "$START" ] && [ "$STACK" = node ] && [ -n "$(pkg_script start)" ]; then START="npm start"; fi
    if [ -z "$START" ]; then
      echo "::notice::Sin comando de arranque (librería o no configurado en repos/<repo>.json): el smoke test no aplica."
      exit 0
    fi
    install
    if [ "$STACK" = node ]; then
      if [ -n "${CMD_BUILD:-}" ]; then run "$CMD_BUILD"; else run "npm run build --if-present"; fi
    fi
    echo "+ $START (en segundo plano, PORT=$PORT)"
    bash -c "$START" > app.log 2>&1 &
    for _ in $(seq 1 30); do
      if curl -fsS "$SMOKE_URL" > /dev/null; then echo "OK: $SMOKE_URL responde"; exit 0; fi
      sleep 2
    done
    echo "::error title=Smoke test::La app no respondió en $SMOKE_URL en 60 segundos. Últimas líneas del log:"
    tail -n 50 app.log
    exit 1 ;;
esac
