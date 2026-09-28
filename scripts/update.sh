#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Uso: scripts/update.sh [--no-restart]

Atualiza a branch atual a partir do upstream (somente fast-forward), instala o
pacote na .venv e reinicia pdv-device-bridge.service.

--no-restart  Atualiza e instala sem acessar o systemd (desenvolvimento local).
EOF
}

restart_service=1
case "${1:-}" in
  "") ;;
  --no-restart) restart_service=0 ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
if (( $# > 1 )); then
  usage >&2
  exit 2
fi

fail() {
  printf 'Erro: %s\n' "$*" >&2
  exit 1
}

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$repo_dir"

command -v git >/dev/null || fail 'git nao encontrado.'
[[ "$(git rev-parse --show-toplevel 2>/dev/null)" == "$repo_dir" ]] || fail 'O script precisa estar no checkout do projeto.'
git symbolic-ref --quiet --short HEAD >/dev/null || fail 'Checkout em detached HEAD; selecione uma branch antes de atualizar.'
git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' >/dev/null 2>&1 || fail 'A branch atual nao possui upstream configurado.'

if [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
  fail 'O checkout tem alteracoes locais. Revise/guarde as alteracoes antes de atualizar.'
fi

if (( restart_service )); then
  command -v systemctl >/dev/null || fail 'systemctl nao encontrado; use --no-restart apenas para desenvolvimento local.'
  systemctl cat pdv-device-bridge.service >/dev/null 2>&1 || fail 'A unidade pdv-device-bridge.service nao esta instalada.'
  [[ -r /etc/pdv-device-bridge/config.toml ]] || fail 'Configuracao /etc/pdv-device-bridge/config.toml nao encontrada ou sem permissao de leitura.'
  if (( EUID != 0 )); then
    command -v sudo >/dev/null || fail 'sudo nao encontrado para reiniciar o servico.'
    sudo -v || fail 'Permissao sudo necessaria para reiniciar o servico.'
  fi
fi

old_revision="$(git rev-parse --short HEAD)"
printf 'Atualizando Git (%s)...\n' "$old_revision"
git pull --ff-only
new_revision="$(git rev-parse --short HEAD)"

if [[ ! -e .venv/bin/python ]]; then
  [[ ! -e .venv ]] || fail 'A .venv existe, mas nao contem bin/python.'
  command -v python3 >/dev/null || fail 'python3 nao encontrado para criar a .venv.'
  python3 -m venv .venv
fi

printf 'Instalando a revisao %s na .venv...\n' "$new_revision"
.venv/bin/python -m pip install -e .
.venv/bin/pdv-device-bridge --help >/dev/null

if (( restart_service )); then
  printf 'Reiniciando pdv-device-bridge.service...\n'
  if (( EUID == 0 )); then
    systemctl restart pdv-device-bridge.service
    systemctl is-active --quiet pdv-device-bridge.service || fail 'O servico nao ficou ativo apos o reinicio.'
  else
    sudo systemctl restart pdv-device-bridge.service
    sudo systemctl is-active --quiet pdv-device-bridge.service || fail 'O servico nao ficou ativo apos o reinicio.'
  fi

  .venv/bin/python - <<'PY'
import json
from pathlib import Path
import time
import tomllib
from urllib.request import ProxyHandler, build_opener

config = tomllib.loads(Path('/etc/pdv-device-bridge/config.toml').read_text())
server = config.get('server', {})
host = server.get('host', '0.0.0.0')
host = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(host, host)
if ':' in host:
    host = f'[{host}]'
url = f"http://{host}:{server.get('port', 8787)}/health"
opener = build_opener(ProxyHandler({}))
deadline = time.monotonic() + 30
last_error = 'sem resposta'

while time.monotonic() < deadline:
    try:
        with opener.open(url, timeout=2) as response:
            health = json.load(response)
        if health.get('status') in ('ok', 'degraded'):
            print(f"Bridge respondeu em {url}: {health['status']}")
            break
        last_error = f"status inesperado: {health.get('status')}"
    except Exception as exc:
        last_error = str(exc)
    time.sleep(1)
else:
    raise SystemExit(f'Erro: /health nao respondeu apos o reinicio: {last_error}')
PY
fi

printf 'Atualizacao concluida: %s -> %s\n' "$old_revision" "$new_revision"
