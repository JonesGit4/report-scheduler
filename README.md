# Report Scheduler — DuoBro

Scheduler Python autossuficiente que substitui os workflows n8n de reports automáticos.

## Stack

| Componente | Tech |
|-----------|------|
| Linguagem | Python 3.10+ |
| Auth Google | JWT com Service Account (RS256) |
| APIs | GA4, Meta Ads, Meta Pixel, Search Console |
| Dedup | NocoDB REST API |
| Entrega | Telegram Bot API |
| Dependências | cryptography, pytz |

## Servidor

- **Host:** manager1 (116.203.22.114)
- **SSH:** `ssh -i ~/.ssh/hermes_portainer -p 3022 root@116.203.22.114`
- **Path:** `/opt/report-scheduler/scheduler.py`
- **SA Key:** `/opt/report-scheduler/sa-key.pem` (NÃO versionada)

## Deploy

```bash
# 1. Copiar scheduler
scp scheduler.py root@manager1:/opt/report-scheduler/scheduler.py

# 2. Copiar SA key (obter do n8n workflow ou secrets manager)
scp sa-key.pem root@manager1:/opt/report-scheduler/sa-key.pem
chmod 600 /opt/report-scheduler/sa-key.pem

# 3. Verificar dependências
ssh root@manager1 'python3 -c "from cryptography.hazmat.primitives import hashes; import pytz; print(\"OK\")"'

# 4. Testar
ssh root@manager1 'cd /opt/report-scheduler && python3 scheduler.py'

# 5. Cron (7h BRT = 10h UTC)
# Adicionar: 0 10 * * * cd /opt/report-scheduler && /usr/bin/python3 scheduler.py
```

## Pipeline

1. Cron dispara às 10h UTC (7h BRT) no manager1
2. Scheduler consulta NocoDB `schedule_locks` para dedup
3. Decide reports devidos: diário (sempre), semanal (segunda), mensal (dia 1)
4. Gera Google JWT via Service Account
5. Chama APIs em paralelo: GA4 × 4, Meta Ads, Meta Pixel, Search Console
6. Formata report em Markdown com `md_escape()`
7. Envia via Telegram Bot API (`@portainer01bot`) no grupo Alertas Clientes
8. Marca dedup no NocoDB (`{cliente}-{tipo}`)

## Arquivos

- `scheduler.py` — Código completo (584 linhas)
- `/opt/report-scheduler/sa-key.pem` — Google SA private key (NÃO versionada)
- `/opt/report-scheduler/scheduler.log` — Logs de execução

## Pitfalls

- **NUNCA** usar `parse_mode: "Markdown"` sem `md_escape()` em strings dinâmicas — caracteres como `*`, `_`, `[` quebram o Telegram (HTTP 400)
- O cron usa **10h UTC** (7h BRT). Se mudar horário de verão, ajustar
- Meta token (`META_TOKEN`) é hardcoded — se expirar, gerar novo token via Meta Business Suite
- Meta Pixel: apenas agregação `event` sem filtro funciona (sem `event=PageView,Lead...`)
- Se adicionar novo cliente, criar registros dedup no NocoDB com `last_run_date: "2020-01-01"`
- SA key precisa das scopes: `analytics.readonly`, `webmasters.readonly`

## Comandos úteis

```bash
# Ver logs
ssh manager1 'tail -50 /opt/report-scheduler/scheduler.log'

# Forçar re-run (ignorar dedup)
ssh manager1 'cd /opt/report-scheduler && python3 -c "
import scheduler
scheduler.dedup_check = lambda *a: False
scheduler.main()
"'

# Testar API específica
ssh manager1 'cd /opt/report-scheduler && python3 -c "
import scheduler
token = scheduler.google_jwt(\"https://www.googleapis.com/auth/analytics.readonly\")
print(token[:30])
"'
```

## Variáveis de ambiente

Todas as credenciais estão hardcoded no `scheduler.py` (config section):
- `NOCO_TOKEN` — NocoDB API token
- `TELEGRAM_BOT_TOKEN` — Token do @portainer01bot
- `SA_EMAIL` — Email da Google Service Account
- `META_TOKEN` — Meta System User access token
