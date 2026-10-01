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

## Atualização de uma instalação via Git

No Raspberry Pi, execute como o usuário dono do checkout em `/opt/pdv-device-bridge`:

```bash
cd /opt/pdv-device-bridge
./scripts/update.sh
```

O script exige uma branch com upstream e checkout sem alterações locais. Ele faz
`git pull --ff-only`, instala a revisão recebida na `.venv`, confere o comando
do bridge, reinicia `pdv-device-bridge.service` com `sudo` quando necessário e
confere a resposta de `/health` (inclusive `degraded` quando um dispositivo
está desconectado).
Se o Git ou a instalação falhar, o serviço não é reiniciado. Para atualizar uma
instalação de desenvolvimento sem `systemd`, use `./scripts/update.sh --no-restart`.
Execute o script novamente se precisar reinstalar as dependências sem haver
novos commits.

## Configuração

1. Copie `config.example.toml` para `/etc/pdv-device-bridge/config.toml`.
2. Ajuste `id`, `path` e parâmetros seriais de cada dispositivo.
3. O bind HTTP deve permanecer na LAN (ex.: `0.0.0.0:8787` em rede interna).
4. Defina `server.cors_allowed_origins` com as origens do PDV web (ex.: `http://localhost:8080` no desenvolvimento).

Para registrar o Raspberry no Device Control, instale `systemd/pdv-device-agent.service`, ajustando o `ExecStart` para o caminho real da `.venv` no aparelho, configure `agent.example.toml` em `/etc/pdv-device-bridge/agent.toml` e use o JSON de pré-vínculo fornecido pelo serviço central como arquivo de provisionamento. O agente mantém UUID e credenciais em `/var/lib/pdv-device-bridge` e envia heartbeat e eventos pelo canal de saída. Durante a migração de terminais com URL manual, mantenha `local.enforce_lan_auth = false`; ative a autenticação LAN só depois de migrá-los para o UUID do bridge. Essa mudança reinicia o bridge e deve ser feita com os periféricos ociosos.

## Execução local

```bash
pdv-device-bridge --config ./config.example.toml
```

## Endpoints

- `GET /health`
- `GET /v1/devices`
- `GET /v1/scales/{scale_id}/read?max_age_ms=1500`
- `GET /v1/scales/{scale_id}/readings?limit=50` (leituras recentes em memória)
- `GET /v1/scales/{scale_id}/events` (SSE; evento `scale` com `state=weight|empty|error`)
- `POST /v1/printers/{printer_id}/jobs`
- `GET /v1/printers/{printer_id}/jobs/{job_id}`
- `GET /v1/printers/{printer_id}/jobs?limit=50` (fila e jobs recentes)
- `POST /v1/printers/{printer_id}/jobs/{job_id}/retry` (reenvia um job com falha)

O histórico de pesagens mantém até 500 leituras por balança e o de impressões,
até 500 jobs por impressora enquanto o processo do bridge estiver ativo. Ambos
são voláteis e são limpos ao reiniciar o serviço. `/health` e `/v1/status`
informam a versão instalada. Um reenvio cria um novo `job_id`,
aponta para o job original em `retry_of` e pode imprimir duplicado se a falha
original ocorreu depois de os dados chegarem à impressora. Jobs em andamento ou
já impressos não podem ser reenviados por essa operação.

## Diagnostico da balanca no Raspberry Pi

Execute no diretorio do projeto, com o ambiente virtual instalado:

```bash
.venv/bin/python scripts/troubleshoot_scale.py --config /etc/pdv-device-bridge/config.toml
```

O relatorio confere o caminho `/dev/serial/by-id`, a porta real, permissao do usuario
atual, enumeracao USB, estado do servico e eventos recentes do kernel. Ele nao
envia comandos a balanca por padrao. Para conferir uma leitura real pelo bridge:

```bash
.venv/bin/python scripts/troubleshoot_scale.py --read-api
```

Para isolar erros de abertura como `cp210x_open - Unable to enable UART`, pare
temporariamente o servico e abra a porta diretamente, sem enviar bytes:

```bash
sudo systemctl stop pdv-device-bridge
.venv/bin/python scripts/troubleshoot_scale.py --open-port
sudo systemctl start pdv-device-bridge
```

O teste direto se recusa a abrir a porta enquanto o servico esta ativo. A abertura
serial pode alterar as linhas de controle DTR/RTS, mesmo sem enviar bytes. Se houver
mais de uma balanca, indique `--scale-id ID`. Erros antigos do kernel aparecem
como historico; uma abertura ou leitura nova confirma o estado atual.

Com `--read-api`, o diagnostico tenta ate tres leituras novas. Se uma falhar e a
seguinte funcionar, informa a falha intermitente como aviso. Nesse modo, os eventos
USB mostrados sao apenas os registrados durante a execucao do teste.

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

- Leitura da balança: timeout serial `800ms` e limite da operação `2500ms`, comando `0x04 0x05`, até `200` bytes; encerra em `CR/LF` ou após `30ms` sem novos bytes. Apenas um quadro serial válido com peso zero retorna `state=empty`, `grams=0` e HTTP `200`. Ausência de bytes, payload não reconhecido e falhas seriais retornam HTTP `502` e degradam `/health`. Um payload inválido é registrado em hexadecimal no log para diagnóstico do protocolo. Uma leitura válida posterior recupera a saúde.
- O stream SSE faz leituras novas enquanto houver assinantes, com uma única rotina por balança. Para avaliar a meta de 500 ms, use `scripts/troubleshoot_scale.py --read-api` para ver a duração HTTP e filme a colocação do item junto com a tela do PDV. Ajuste `scale.read_timeout_ms` no Raspberry somente após medir as respostas reais; o padrão de 800 ms pode impedir essa meta quando a balança não responde.
- A porta da balança permanece aberta entre consultas e é reaberta se o caminho USB mudar ou uma operação serial falhar. Assim o adaptador não precisa ser aberto a cada atualização da tela.
- Cache da última leitura, inclusive peso zero: `1500ms` (ajustável por `max_age_ms`; as telas ao vivo pedem leitura nova).
- Fila por impressora: tamanho máximo `100`.
- Retry de impressão: backoff `200ms`, `500ms`, `1000ms` (1 envio inicial + 3 retries).
- Escrita da impressora: chunks de `512` bytes, pausa `15ms` entre chunks, settle final `1000ms`, timeout de escrita `3000ms`.
