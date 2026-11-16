#!/usr/bin/env python3
"""Telegram bot for the AI Usage Tracker.

Reads the tracker's read-only JSON API (GET /api/accounts) and answers a few
commands with the current subscription limits and how much is left.

Standard library only: no dependencies, no webhook, no inbound port. Telegram
long polling.

Environment:
  TELEGRAM_BOT_TOKEN   bot token from @BotFather (required)
  TRACKER_URL          e.g. https://uso.nsid.app (required)
  TRACKER_TOKEN        read-only API token from Settings -> API tokens (required)
  ALLOWED_CHAT_IDS     comma-separated chat ids; empty = first chat to /start claims it
  ALERT_THRESHOLD      percent used that triggers a push (default 85, 0 disables)
  ALERT_POLL_SECONDS   how often alerts are checked (default 300)
  STATE_DIR            where the claimed chat id and alert state live (default /data)
  TZ                   timezone for reset times (default Europe/Lisbon)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.telegram.org/bot{token}/{method}"

# Friendly names per provider id, so a message reads like the dashboard.
PROVIDER_LABELS = {
    "chatgpt-sub": "ChatGPT",
    "opencode-console": "OpenCode Go",
    "mistral-vibe": "Mistral Vibe",
    "claude-sub": "Claude",
    "openrouter": "OpenRouter",
    "anthropic-api": "Anthropic API",
    "openai-api": "OpenAI API",
    "opencode": "OpenCode (local)",
}

WINDOW_LABELS = {
    "5h": "5 horas",
    "rolling": "5 horas",
    "weekly": "semana",
    "7d": "semana",
    "monthly": "mês",
    "30d": "mês",
}


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} {msg}", flush=True)


# ---------------------------------------------------------------- tracker API


class TrackerError(RuntimeError):
    pass


def api_accounts(base_url: str, token: str, timeout: int = 20) -> list[dict]:
    url = base_url.rstrip("/") + "/api/accounts"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise TrackerError("o token da API do tracker foi recusado") from exc
        raise TrackerError(f"o tracker respondeu HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise TrackerError(f"não consegui falar com o tracker ({exc.reason})") from exc
    except ValueError as exc:
        raise TrackerError("o tracker devolveu algo que não é JSON") from exc


# ------------------------------------------------------------------ rendering


def bar(used: float, width: int = 10) -> str:
    used = max(0.0, min(100.0, used))
    filled = round(used / 100 * width)
    return "█" * filled + "░" * (width - filled)


def human_left(seconds: float) -> str:
    if seconds <= 0:
        return "já reposto"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def provider_label(account: dict) -> str:
    prov = account.get("provider") or ""
    return PROVIDER_LABELS.get(prov, prov or "conta")


def render_account(account: dict, now: float | None = None) -> str:
    now = time.time() if now is None else now
    name = account.get("name") or provider_label(account)
    plan = account.get("plan") or ""
    head = f"*{escape(name)}*"
    if plan:
        head += f" · {escape(plan)}"
    lines = [head]

    if account.get("last_error"):
        lines.append(f"⚠️ erro: {escape(str(account['last_error'])[:180])}")

    quotas = account.get("quotas") or []
    if not quotas:
        lines.append("_sem limites reportados_")
    for quota in quotas:
        used = float(quota.get("used_percent") or 0)
        left = max(0.0, 100.0 - used)
        window = WINDOW_LABELS.get(str(quota.get("window") or ""), str(quota.get("window") or "janela"))
        detail = quota.get("detail") or ""
        resets = int(quota.get("resets_at") or 0)
        line = f"{window}: {bar(used)} {left:.0f}% livre"
        if resets:
            line += f" · repõe em {human_left(resets - now)}"
        if detail:
            line += f"\n   _({escape(detail)})_"
        lines.append(line)

    balance = account.get("balance_usd")
    if balance is not None:
        lines.append(f"saldo: ${balance:.2f}")

    polled = int(account.get("last_poll") or 0)
    if polled:
        lines.append(f"_lido há {human_left(now - polled)}_")
    return "\n".join(lines)


def escape(text: str) -> str:
    # Markdown (legacy) only needs these escaped.
    out = str(text)
    for ch in ("_", "*", "`", "["):
        out = out.replace(ch, "\\" + ch)
    return out


def render(accounts: list[dict], provider_filter: str | None = None, now: float | None = None) -> str:
    if provider_filter:
        accounts = [a for a in accounts if (a.get("provider") or "") == provider_filter]
        if not accounts:
            return "Não tenho essa conta ligada no tracker."
    if not accounts:
        return ("Ainda não há contas no tracker.\n"
                "Liga-as em " + os.environ.get("TRACKER_URL", "").rstrip("/") + "/accounts")
    blocks = [render_account(a, now) for a in accounts]
    return "\n\n".join(blocks)


# -------------------------------------------------------------------- telegram


class Telegram:
    def __init__(self, token: str):
        self.token = token

    def call(self, method: str, http_timeout: int = 60, **params):
        data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(API.format(token=self.token, method=method), data=data or None)
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if not body.get("ok"):
            raise RuntimeError(f"telegram {method}: {body.get('description')}")
        return body.get("result")

    def send(self, chat_id: int | str, text: str) -> None:
        for chunk in chunks(text, 4000):
            self.call("sendMessage", chat_id=chat_id, text=chunk, parse_mode="Markdown",
                      disable_web_page_preview=True)

    def updates(self, offset: int | None) -> list[dict]:
        try:
            return self.call("getUpdates", http_timeout=70, offset=offset, timeout=50) or []
        except (urllib.error.URLError, TimeoutError):
            return []


def chunks(text: str, size: int) -> list[str]:
    out, current = [], ""
    for line in text.split("\n"):
        while len(line) > size:
            out.append(line[:size])
            line = line[size:]
        if len(current) + len(line) + 1 > size:
            out.append(current.rstrip())
            current = ""
        current += line + "\n"
    if current.strip():
        out.append(current.rstrip())
    return out or [text[:size]]


# ------------------------------------------------------------------- behaviour


HELP = (
    "*Consultar consumo*\n"
    "/uso — todas as subscrições\n"
    "/gpt — ChatGPT Plus\n"
    "/go — OpenCode Go\n"
    "/mistral — Mistral Vibe\n"
    "/id — o meu chat id (para allowlist)\n\n"
    "Os números vêm do tracker, que lê cada provider de 2 em 2 minutos."
)


class State:
    def __init__(self, directory: str):
        self.path = os.path.join(directory, "state.json")
        self.data = {"chat_id": None, "alerts": {}}
        try:
            with open(self.path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh)
        os.replace(tmp, self.path)


def allowed_chats(env_value: str) -> set[int]:
    out: set[int] = set()
    for part in (env_value or "").replace(" ", "").split(","):
        if part:
            try:
                out.add(int(part))
            except ValueError:
                log(f"ALLOWED_CHAT_IDS: ignorado {part!r}")
    return out


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tracker_url = os.environ.get("TRACKER_URL", "").strip()
    tracker_token = os.environ.get("TRACKER_TOKEN", "").strip()
    missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", token), ("TRACKER_URL", tracker_url),
                              ("TRACKER_TOKEN", tracker_token)) if not v]
    if missing:
        log("faltam variáveis de ambiente: " + ", ".join(missing))
        return 2

    threshold = float(os.environ.get("ALERT_THRESHOLD", "85") or 0)
    alert_every = int(os.environ.get("ALERT_POLL_SECONDS", "300") or 300)
    state = State(os.environ.get("STATE_DIR", "/data"))
    allow = allowed_chats(os.environ.get("ALLOWED_CHAT_IDS", ""))
    tg = Telegram(token)

    me = tg.call("getMe")
    log(f"a correr como @{me.get('username')} (threshold={threshold:g}%, alerts a cada {alert_every}s)")

    offset = None
    next_alert = time.time() + alert_every

    def chat_allowed(chat_id: int) -> bool:
        if allow:
            return chat_id in allow
        if state.data.get("chat_id") is None or state.data["chat_id"] == chat_id:
            return True
        return False

    while True:
        try:
            for update in tg.updates(offset):
                offset = update["update_id"] + 1
                message = update.get("message") or update.get("edited_message") or {}
                text = (message.get("text") or "").strip()
                chat = message.get("chat") or {}
                chat_id = chat.get("id")
                if not text or chat_id is None:
                    continue
                if allow and chat_id not in allow:
                    tg.send(chat_id, "Não estás autorizado. Pede ao dono para adicionar o teu chat id.")
                    continue
                if not allow and state.data.get("chat_id") is None:
                    state.data["chat_id"] = chat_id
                    state.save()
                    log(f"chat reclamado: {chat_id}")
                command = text.split()[0].split("@")[0].lower()

                if command == "/start":
                    tg.send(chat_id, "Olá. " + HELP)
                    continue
                if command == "/id":
                    tg.send(chat_id, f"chat id: `{chat_id}`")
                    continue
                if command in ("/help", "/ajuda"):
                    tg.send(chat_id, HELP)
                    continue

                provider = {"/gpt": "chatgpt-sub", "/go": "opencode-console",
                            "/mistral": "mistral-vibe", "/uso": None}.get(command)
                if provider is None and command not in ("/uso",):
                    tg.send(chat_id, "Não conheço esse comando.\n\n" + HELP)
                    continue
                try:
                    accounts = api_accounts(tracker_url, tracker_token)
                    tg.send(chat_id, render(accounts, provider))
                except TrackerError as exc:
                    tg.send(chat_id, f"Não consegui ler o tracker: {escape(str(exc))}")

            if threshold > 0 and time.time() >= next_alert:
                next_alert = time.time() + alert_every
                chat_id = state.data.get("chat_id") or (next(iter(allow)) if allow else None)
                if chat_id is not None:
                    check_alerts(tg, chat_id, tracker_url, tracker_token, state, threshold)
        except Exception as exc:  # keep the loop alive; a bot that dies is worse than a noisy one
            log(f"erro no ciclo: {type(exc).__name__}: {exc}")
            time.sleep(5)


def check_alerts(tg: "Telegram", chat_id, tracker_url: str, tracker_token: str,
                 state: "State", threshold: float) -> None:
    try:
        accounts = api_accounts(tracker_url, tracker_token)
    except TrackerError as exc:
        log(f"alertas: {exc}")
        return

    alerts: dict = state.data.setdefault("alerts", {})
    changed = False
    for account in accounts:
        name = account.get("name") or provider_label(account)
        for quota in account.get("quotas") or []:
            key = f"{account.get('id')}:{quota.get('window')}"
            used = float(quota.get("used_percent") or 0)
            previous = float(alerts.get(key, 0))
            if used >= threshold and previous < threshold:
                left = max(0.0, 100.0 - used)
                tg.send(chat_id, f"⚠️ *{escape(str(name))}* — {quota.get('window')} a {used:.0f}% usado "
                                 f"({left:.0f}% livre).")
            elif previous >= threshold and used < 5:
                tg.send(chat_id, f"✅ *{escape(str(name))}* — {quota.get('window')} reposto.")
            if abs(previous - used) > 0.01:
                alerts[key] = used
                changed = True
    if changed:
        state.save()


if __name__ == "__main__":
    sys.exit(main())
