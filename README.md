# pdv-device-bridge (Python 3.11+)

Bridge serial para Raspberry Pi que atende balanças e impressoras ESC/POS para vários caixas via HTTP na LAN.

## Requisitos

- Python 3.11, 3.12 ou 3.13
- Linux com `/dev/serial/by-id`
- Permissão de acesso serial (grupos `dialout` e `lp` conforme hardware)

## Instalação

```bash
cd utils/pdv-device-bridge
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .
```

## Configuração

1. Copie `config.example.toml` para `/etc/pdv-device-bridge/config.toml`.
2. Ajuste `id`, `path` e parâmetros seriais de cada dispositivo.
3. O bind HTTP deve permanecer na LAN (ex.: `0.0.0.0:8787` em rede interna).
4. Defina `server.cors_allowed_origins` com as origens do PDV web (ex.: `http://localhost:8080` no desenvolvimento).

## Execução local

```bash
pdv-device-bridge --config ./config.example.toml
```

## Endpoints

- `GET /health`
- `GET /v1/devices`
- `GET /v1/scales/{scale_id}/read?max_age_ms=1500`
- `POST /v1/printers/{printer_id}/jobs`
- `GET /v1/printers/{printer_id}/jobs/{job_id}`

### Exemplo de job ESC/POS bruto

```bash
PAYLOAD_BASE64=$(printf '\x1b@Teste bridge\n\x1dVA\x10' | base64)

curl -X POST "http://127.0.0.1:8787/v1/printers/printer-caixa-1/jobs" \
  -H "Content-Type: application/json" \
  -d "{\"payload_base64\":\"${PAYLOAD_BASE64}\",\"content_type\":\"escpos_raw\",\"request_id\":\"sale-123\"}"
```

## systemd

Arquivo de unidade pronto em:

- `systemd/pdv-device-bridge.service`

Config padrão aplicada:

- `Restart=always`
- `RestartSec=2`
- `WatchdogSec=20`

O processo envia `READY=1` e `WATCHDOG=1` automaticamente quando executado com `Type=notify`.

## Testes

```bash
cd utils/pdv-device-bridge
source .venv/bin/activate
python -m pip install -e .[dev]
pytest
```

## Políticas operacionais implementadas

- Leitura da balança: timeout `800ms`, comando `0x04 0x05`, até `200` bytes.
- Cache de peso válido: `1500ms` (ajustável por `max_age_ms`).
- Fila por impressora: tamanho máximo `100`.
- Retry de impressão: backoff `200ms`, `500ms`, `1000ms` (1 envio inicial + 3 retries).
- Escrita da impressora: chunks de `512` bytes, pausa `15ms` entre chunks, settle final `1000ms`, timeout de escrita `3000ms`.
