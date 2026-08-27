# PDV Device Bridge

Bridge serial para Raspberry Pi que atende balanças e impressoras ESC/POS para vários caixas via HTTP na LAN.

## Requisitos

- Python 3.11, 3.12 ou 3.13
- Linux com `/dev/serial/by-id`
- Permissão de acesso serial (grupos `dialout` e `lp` conforme hardware)

Este é o repositório standalone e a fonte oficial do bridge. Ele contém o serviço operacional sem privilégios, o agente de controle/OTA, o instalador e a geração de imagens Raspberry Pi OS Lite arm64.

## Instalação de desenvolvimento

```bash
cd pdv-device-bridge
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .
```

## Configuração

1. Copie `config.example.toml` para `/var/lib/pdv-device-bridge/config.toml`.
2. Ajuste `id`, `path` e parâmetros seriais de cada dispositivo.
3. O bind HTTP deve permanecer na LAN (ex.: `0.0.0.0:8787` em rede interna).
4. Defina `server.cors_allowed_origins` com as origens do PDV web (ex.: `http://localhost:8080` no desenvolvimento).

## Execução local

```bash
pdv-device-bridge --config ./config.example.toml
```

## Endpoints

- `GET /health`
- `GET /v1/identity`
- `GET /v1/devices`
- `GET /v1/serial-ports`
- `PUT /v1/configuration`
- `GET /v1/scales/{scale_id}/read?max_age_ms=1500`
- `POST /v1/printers/{printer_id}/jobs`
- `GET /v1/printers/{printer_id}/jobs/{job_id}`

### Exemplo de job ESC/POS bruto

```bash
PAYLOAD_BASE64=$(printf '\x1b@Teste bridge\n\x1dVA\x10' | base64)

curl -X POST "http://127.0.0.1:8787/v1/printers/printer-caixa-1/jobs" \
  -H "Authorization: Bearer TOKEN_LAN" \
  -H "Content-Type: application/json" \
  -d "{\"payload_base64\":\"${PAYLOAD_BASE64}\",\"content_type\":\"escpos_raw\",\"request_id\":\"sale-123\"}"
```

Depois da matrícula, todos os endpoints `/v1/*` exigem o token LAN. O `/health` permanece público e mínimo. Antes do pareamento, o modo legado continua funcionando sem autenticação.

## Instalação headless

O dashboard do `pdv-device-control` gera `device_id`, código curto, hostname e token individual. Para um Raspberry com sistema existente, execute `install/install.sh` com os parâmetros mostrados por `--help`/mensagem de validação. O instalador:

- valida arquitetura arm64 e SHA-256 do bundle;
- instala o serviço operacional como usuário `pdvbridge`;
- instala o agente de controle endurecido;
- registra `_pdvbridge._tcp` no Avahi e define `freeline-bridge-XXXXXX.local`;
- habilita `unattended-upgrades` e mantém SSH apenas por chave.

As receitas oficiais de imagem estão em [`image/README.md`](image/README.md). O JSON individual deve ser gravado como `pdv-device-bridge.json` na partição de boot; ele é consumido e apagado no primeiro boot.

## OTA

`scripts/build_release.py` gera wheelhouse arm64 para Raspberry Pi OS Bookworm/Trixie, bundle, manifesto, SHA-256 e assinatura Ed25519. O agente prepara a versão em diretório separado e só ativa às 03:00 (ou em urgência autorizada) quando impressão e balança estão ociosas. A troca do symlink é atômica e `/health` precisa responder em até 60 segundos; caso contrário, a versão anterior é restaurada. O marcador persistente cobre recuperação após queda de energia.

## systemd

Arquivo de unidade pronto em:

- `systemd/pdv-device-bridge.service`
- `systemd/pdv-device-agent.service`

Config padrão aplicada:

- `Restart=always`
- `RestartSec=2`
- `WatchdogSec=20`

O processo envia `READY=1` e `WATCHDOG=1` automaticamente quando executado com `Type=notify`.

## Testes

```bash
cd pdv-device-bridge
source .venv/bin/activate
python -m pip install -e .[dev]
pytest
```

## Políticas operacionais implementadas

- Leitura da balança: timeout total `800ms`, comando `0x04 0x05`, até `200` bytes; encerra em `CR/LF` ou após `30ms` sem novos bytes.
- Cache de peso válido: `1500ms` (ajustável por `max_age_ms`).
- Fila por impressora: tamanho máximo `100`.
- Retry de impressão: backoff `200ms`, `500ms`, `1000ms` (1 envio inicial + 3 retries).
- Escrita da impressora: chunks de `512` bytes, pausa `15ms` entre chunks, settle final `1000ms`, timeout de escrita `3000ms`.
