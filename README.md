# AI Usage Bot

Bot de Telegram para consultar os limites das subscrições de IA que estão no
[ai-usage-tracker](https://github.com/NSid-Labs/ai-usage-tracker).

Não fala com os providers: lê a API read-only do tracker (`GET /api/accounts`),
que por sua vez faz o polling ao ChatGPT, OpenCode Go e Mistral Vibe de 2 em 2
minutos. Um contentor, zero dependências (só a biblioteca padrão do Python), sem
porta de entrada — usa long polling do Telegram.

## Comandos

| Comando | O que devolve |
|---|---|
| `/uso` | Todas as contas, cada janela com barra, % livre e quando repõe |
| `/gpt` | ChatGPT Plus (5h + semanal) |
| `/go` | OpenCode Go (5h, semanal, mensal) |
| `/mistral` | Mistral Vibe (mensal) |
| `/id` | O teu chat id, para a allowlist |
| `/help` | Ajuda |

Exemplo:

```
ChatGPT · plus
5 horas: █░░░░░░░░░ 82% livre · repõe em 4h01m
semana:  █████████░ 97% livre · repõe em 6d23h
_lido há 2m_
```

Além de responder, empurra um aviso quando uma janela passa de
`ALERT_THRESHOLD` (85% por omissão) e outro quando repõe. O estado vive em
`/data/state.json`, por isso um restart não repete avisos.

## Variáveis

| Variável | Obrigatória | O que é |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | sim | Token do @BotFather |
| `TRACKER_URL` | sim | Ex.: `https://uso.nsid.app` |
| `TRACKER_TOKEN` | sim | Token read-only do tracker: *Settings → API tokens* |
| `ALLOWED_CHAT_IDS` | não | Lista `1,2,3`. Vazio = o primeiro chat a fazer `/start` fica dono |
| `ALERT_THRESHOLD` | não | % usado que dispara o aviso (85; `0` desliga) |
| `ALERT_POLL_SECONDS` | não | Intervalo da verificação de alertas (300) |
| `STATE_DIR` | não | `/data` (volume) |
| `TZ` | não | `Europe/Lisbon` |

## Deploy no Coolify

Igual ao tracker: build pack **Dockerfile**, porta nenhuma (não expõe nada),
volume `/data`, restart `unless-stopped`.

1. Criar bot com o [@BotFather](https://t.me/BotFather) → `/newbot` → copiar o token.
2. No tracker, *Settings → API tokens* → criar um token **read-only** (só serve
   `/api/accounts`; não dá acesso à UI nem às credenciais).
3. Criar a app no Coolify a partir deste repo, com as variáveis acima.
4. Falar com o bot: `/start`. Se `ALLOWED_CHAT_IDS` estiver vazio, esse chat fica
   registado e mais ninguém consegue consultar.

## Local

```bash
export TELEGRAM_BOT_TOKEN=... TRACKER_URL=https://uso.nsid.app TRACKER_TOKEN=...
python3 bot.py
```

Testes: `python3 -m unittest -v`.
