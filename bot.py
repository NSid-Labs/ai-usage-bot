#!/usr/bin/env python3
"""Telegram bot for the AI Usage Tracker.

Reads the tracker's read-only JSON API (GET /api/accounts) and shows the
subscription limits as aligned monospace tables, with figures for what is left,
when it resets and — from its own samples — whether the current pace runs out
before the reset.

Standard library only: no dependencies, no webhook, no inbound port.

Environment:
  TELEGRAM_BOT_TOKEN   bot token from @BotFather (required)
  TRACKER_URL          e.g. https://uso.nsid.app (required)
  TRACKER_TOKEN        read-only API token from Settings -> API tokens (required)
  ALLOWED_CHAT_IDS     comma-separated chat ids; empty = first chat to /start claims it
  CURRENCY             symbol for money figures (default €)
  ALERT_THRESHOLD      percent used that triggers a push (default 85, 0 disables)
  ALERT_POLL_SECONDS   how often limits are sampled and alerts checked (default 300)
  DIGEST_AT            local time for the daily digest, HH:MM (default 09:00, empty disables)
  STATE_DIR            where the chat id, samples and alert state live (default /data)
  TZ                   timezone for reset times and the digest (default Europe/Lisbon)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - zoneinfo is stdlib from 3.9
    ZoneInfo = None  # type: ignore

API = "https://api.telegram.org/bot{token}/{method}"

PROVIDER_LABELS = {
    "chatgpt-sub": "ChatGPT",
    "opencode-console": "OpenCode",
    "mistral-vibe": "Mistral",
    "claude-sub": "Claude",
    "openrouter": "OpenRouter",
    "anthropic-api": "Anthropic API",
    "openai-api": "OpenAI API",
    "opencode": "OpenCode (local)",
}

PLAN_SUFFIX = {"plus": "Plus", "pro": "Pro", "max": "Max", "free": "Free",
               "student": "Student", "go": "Go", "go plus": "Go Plus",
               "go teams": "Go Teams", "free tier": "free tier"}

# Windows have a fixed order and a fixed-width label so the bars line up.
WINDOW_ORDER = {"5h": 0, "rolling": 0, "weekly": 1, "7d": 1, "monthly": 2, "30d": 2}
WINDOW_SHORT = {"5h": "5h", "rolling": "5h", "weekly": "sem", "7d": "sem",
                "monthly": "mês", "30d": "30d"}
LABEL_WIDTH = 4
BAR_WIDTH = 10
SAMPLE_KEEP = 48          # ~4h at one sample every 5m
SAMPLE_WINDOW = 3600      # forecast looks at the last hour
SAMPLE_MIN_SPAN = 900     # and needs at least 15 minutes of history
BUSY_THRESHOLD = 40       # show the "tightest" line from here up


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} {msg}", flush=True)


def tzinfo():
    name = os.environ.get("TZ", "Europe/Lisbon")
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def currency() -> str:
    return os.environ.get("CURRENCY", "€") or "€"


# ---------------------------------------------------------------- tracker API


class TrackerError(RuntimeError):
    pass


# Cloudflare in front of the tracker rejects requests whose User-Agent looks like
# a bare script (python-urllib gets a 403, error 1010), so identify the bot.
USER_AGENT = "ai-usage-bot/1.0 (+https://github.com/NSid-Labs/ai-usage-bot)"


def api_accounts(base_url: str, token: str, timeout: int = 20) -> list[dict]:
    url = base_url.rstrip("/") + "/api/accounts"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise TrackerError("o token da API do tracker foi recusado (401)") from exc
        if exc.code == 403:
            raise TrackerError("o tracker recusou o pedido (403) — token sem permissão ou bloqueio do Cloudflare") from exc
        raise TrackerError(f"o tracker respondeu HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise TrackerError(f"não consegui falar com o tracker ({exc.reason})") from exc
    except ValueError as exc:
        raise TrackerError("o tracker devolveu algo que não é JSON") from exc


# ------------------------------------------------------------------ formatting


def bar(used: float, width: int = BAR_WIDTH) -> str:
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


def local_stamp(unix: int, now: float | None = None) -> str:
    """Reset time as a person reads it: time today, date otherwise."""
    if not unix:
        return ""
    tz = tzinfo()
    moment = datetime.fromtimestamp(unix, tz)
    current = datetime.fromtimestamp(time.time() if now is None else now, tz)
    if moment.date() == current.date():
        return moment.strftime("%H:%M")
    if moment.year == current.year:
        return moment.strftime("%d/%m")
    return moment.strftime("%d/%m/%y")


def money_detail(detail: str) -> str:
    """'6.48 of 127.50' -> '6,48 € de 127,50 €'. Anything else is left alone."""
    match = re.fullmatch(r"\s*([\d.]+)\s+of\s+([\d.]+)\s*", detail or "")
    if not match:
        return detail
    sym = currency()
    a = f"{float(match.group(1)):.2f}".replace(".", ",")
    b = f"{float(match.group(2)):.2f}".replace(".", ",")
    return f"{a} {sym} de {b} {sym}"


def plan_label(plan: str) -> str:
    if not plan:
        return ""
    key = plan.strip().lower()
    return PLAN_SUFFIX.get(key, plan.strip())


def provider_label(account: dict) -> str:
    prov = account.get("provider") or ""
    return PROVIDER_LABELS.get(prov, prov or "conta")


def heading(account: dict) -> str:
    name = account.get("name") or provider_label(account)
    plan = plan_label(account.get("plan") or "")
    return f"{name} · {plan}" if plan else str(name)


def ordered_quotas(account: dict) -> list[dict]:
    return sorted(account.get("quotas") or [],
                  key=lambda q: (WINDOW_ORDER.get(str(q.get("window")), 9), str(q.get("window"))))


def used_of(account: dict) -> float:
    return max((float(q.get("used_percent") or 0) for q in account.get("quotas") or []), default=0.0)


def pace_note(account: dict, quota: dict, samples: dict | None, now: float) -> str:
    """'100% em 2h40 (antes de repor)' when the current pace runs out early."""
    if not samples:
        return ""
    key = f"{account.get('id')}:{quota.get('window')}"
    history = [p for p in samples.get(key, []) if now - p[0] <= SAMPLE_WINDOW]
    if len(history) < 2:
        return ""
    span = history[-1][0] - history[0][0]
    if span < SAMPLE_MIN_SPAN:
        return ""
    gain = history[-1][1] - history[0][1]
    if gain <= 0.5:
        return ""
    per_hour = gain / (span / 3600)
    used = float(quota.get("used_percent") or 0)
    resets = int(quota.get("resets_at") or 0)
    if used >= 100 or per_hour <= 0:
        return ""
    eta = (100 - used) / per_hour * 3600
    if resets and now + eta >= resets:
        return ""
    return f"100% em {human_left(eta)}"


def account_card(account: dict, now: float, samples: dict | None = None) -> str:
    lines = [f"*{escape(heading(account))}*"]
    if account.get("last_error"):
        lines.append(f"⚠️ {escape(str(account['last_error'])[:180])}")

    quotas = ordered_quotas(account)
    if not quotas:
        today = account.get("cost_today_usd")
        extra = f" · ${today:.2f} hoje" if today is not None else ""
        lines.append(f"{escape(heading(account))}{extra}")
        return "\n".join(lines)

    rows = []
    for quota in quotas:
        used = float(quota.get("used_percent") or 0)
        window = str(quota.get("window") or "")
        label = WINDOW_SHORT.get(window, window)[:LABEL_WIDTH].ljust(LABEL_WIDTH)
        resets = int(quota.get("resets_at") or 0)
        row = f"{label} {bar(used)} {used:>3.0f}%"
        if resets:
            row += f"  repõe {local_stamp(resets, now)}"
        note = pace_note(account, quota, samples, now)
        if note:
            row += f" · {note}"
        rows.append(row)
    lines.append("```\n" + "\n".join(rows) + "\n```")

    details = [money_detail(q.get("detail") or "") for q in quotas if q.get("detail")]
    details = [d for d in details if d]
    if details:
        lines.append("_" + escape(" · ".join(details)) + "_")

    balance = account.get("balance_usd")
    if balance is not None:
        lines.append(f"saldo: ${balance:.2f}")
    return "\n".join(lines)


def escape(text: str) -> str:
    out = str(text)
    for ch in ("_", "*", "`", "["):
        out = out.replace(ch, "\\" + ch)
    return out


def tightest(accounts: list[dict]) -> tuple[dict, dict] | None:
    best = None
    for account in accounts:
        for quota in account.get("quotas") or []:
            used = float(quota.get("used_percent") or 0)
            if best is None or used > best[0]:
                best = (used, account, quota)
    if best is None or best[0] < BUSY_THRESHOLD:
        return None
    return best[1], best[2]


def keyboard(selected: str | None) -> dict:
    if selected:
        return {"inline_keyboard": [[
            {"text": "◀ Voltar", "callback_data": "p:all"},
            {"text": "Actualizar", "callback_data": f"p:{selected}"},
        ]]}
    return {"inline_keyboard": [
        [{"text": "ChatGPT", "callback_data": "p:chatgpt-sub"},
         {"text": "OpenCode", "callback_data": "p:opencode-console"},
         {"text": "Mistral", "callback_data": "p:mistral-vibe"}],
        [{"text": "Actualizar", "callback_data": "p:all"}],
    ]}


def render(accounts: list[dict], provider: str | None = None, now: float | None = None,
           samples: dict | None = None, poll_minutes: int | None = None) -> tuple[str, dict]:
    now = time.time() if now is None else now
    tz = tzinfo()
    clock = datetime.fromtimestamp(now, tz).strftime("%H:%M")

    if provider:
        accounts = [a for a in accounts if (a.get("provider") or "") == provider]
        if not accounts:
            return "Não tenho essa conta ligada no tracker.", keyboard(None)
        title = f"📊 *{escape(provider_label(accounts[0]))}* · {clock}"
        head = [title]
        body = [account_card(a, now, samples) for a in accounts]
    else:
        if not accounts:
            url = os.environ.get("TRACKER_URL", "").rstrip("/")
            return (f"Ainda não há contas no tracker.\nLiga-as em {url}/accounts",
                    keyboard(None))
        accounts = sorted(accounts, key=lambda a: -used_of(a))
        head = [f"📊 *Consumo* · {clock}"]
        busy = tightest(accounts)
        if busy:
            account, quota = busy
            window = WINDOW_SHORT.get(str(quota.get("window")), str(quota.get("window")))
            head.append(f"⚠️ mais apertado: *{escape(heading(account))}* {window} — "
                        f"{float(quota.get('used_percent') or 0):.0f}% usado")
        body = [account_card(a, now, samples) for a in accounts]

    poll = os.environ.get("TRACKER_POLL_INTERVAL") or ""
    footer = f"actualizado {clock}"
    if poll_minutes or poll:
        footer += f" · polling {poll or f'{poll_minutes}m'}"
    text = "\n\n".join(head + body + ["_" + footer + "_"])
    return text, keyboard(provider)


def digest(accounts: list[dict], now: float) -> str:
    """Morning summary: the tightest window and what resets today."""
    tz = tzinfo()
    today = datetime.fromtimestamp(now, tz).date()
    lines = [f"☀️ *Resumo* · {datetime.fromtimestamp(now, tz).strftime('%d/%m')}"]

    busy = tightest(accounts)
    if busy:
        account, quota = busy
        window = WINDOW_SHORT.get(str(quota.get("window")), str(quota.get("window")))
        lines.append(f"mais apertado: *{escape(heading(account))}* {window} — "
                     f"{float(quota.get('used_percent') or 0):.0f}% usado")
    else:
        lines.append("tudo folgado, nada acima de 40%")

    resets_today = []
    for account in accounts:
        for quota in account.get("quotas") or []:
            resets = int(quota.get("resets_at") or 0)
            if not resets:
                continue
            when = datetime.fromtimestamp(resets, tz)
            if when.date() == today:
                window = WINDOW_SHORT.get(str(quota.get("window")), str(quota.get("window")))
                resets_today.append(f"{escape(heading(account))} {window} às {when.strftime('%H:%M')}")
    if resets_today:
        lines.append("repõe hoje: " + "; ".join(resets_today))
    lines.append("_/uso para o detalhe_")
    return "\n".join(lines)


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


# -------------------------------------------------------------------- telegram


class Telegram:
    def __init__(self, token: str):
        self.token = token

    def call(self, method: str, http_timeout: int = 60, **params):
        payload = {k: v for k, v in params.items() if v is not None}
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(API.format(token=self.token, method=method), data=data or None)
        req.add_header("User-Agent", USER_AGENT)
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if not body.get("ok"):
            raise RuntimeError(f"telegram {method}: {body.get('description')}")
        return body.get("result")

    def send(self, chat_id: int | str, text: str, reply_markup: dict | None = None) -> None:
        for index, chunk in enumerate(chunks(text, 4000)):
            self.call("sendMessage", chat_id=chat_id, text=chunk, parse_mode="Markdown",
                      disable_web_page_preview=True,
                      reply_markup=json.dumps(reply_markup) if reply_markup and index == 0 else None)

    def edit(self, chat_id: int | str, message_id: int, text: str, reply_markup: dict | None = None) -> None:
        try:
            self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                      parse_mode="Markdown", disable_web_page_preview=True,
                      reply_markup=json.dumps(reply_markup) if reply_markup else None)
        except urllib.error.HTTPError as exc:
            # "message is not modified" when the user taps Actualizar twice: harmless.
            if exc.code != 400:
                raise

    def updates(self, offset: int | None) -> list[dict]:
        try:
            return self.call("getUpdates", http_timeout=70, offset=offset, timeout=50) or []
        except (urllib.error.URLError, TimeoutError):
            return []

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text or None)
        except (urllib.error.URLError, RuntimeError):
            pass


# ------------------------------------------------------------------- behaviour


HELP = (
    "*Consultar consumo*\n"
    "/uso — todas as subscrições\n"
    "/gpt — ChatGPT\n"
    "/go — OpenCode Go\n"
    "/mistral — Mistral Vibe\n"
    "/id — o meu chat id\n\n"
    "Nos botões também dá. Os números vêm do tracker, que lê cada provider "
    "de 2 em 2 minutos."
)


class State:
    def __init__(self, directory: str):
        self.path = os.path.join(directory, "state.json")
        self.data = {"chat_id": None, "alerts": {}, "samples": {}, "last_digest": ""}
        try:
            with open(self.path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                for key, value in loaded.items():
                    self.data[key] = value
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh)
        os.replace(tmp, self.path)

    def sample(self, accounts: list[dict], now: float) -> None:
        samples = self.data.setdefault("samples", {})
        for account in accounts:
            for quota in account.get("quotas") or []:
                key = f"{account.get('id')}:{quota.get('window')}"
                series = [p for p in samples.get(key, []) if now - p[0] <= 4 * SAMPLE_WINDOW]
                series.append([now, float(quota.get("used_percent") or 0)])
                samples[key] = series[-SAMPLE_KEEP:]
        self.save()


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
    every = int(os.environ.get("ALERT_POLL_SECONDS", "300") or 300)
    digest_at = (os.environ.get("DIGEST_AT", "09:00") or "").strip()
    state = State(os.environ.get("STATE_DIR", "/data"))
    allow = allowed_chats(os.environ.get("ALLOWED_CHAT_IDS", ""))
    tg = Telegram(token)

    me = tg.call("getMe")
    log(f"a correr como @{me.get('username')} (threshold={threshold:g}%, amostras a cada {every}s, "
        f"digest={digest_at or 'off'})")

    offset = None
    next_tick = time.time() + every

    def chat_id_for_alerts() -> int | None:
        return state.data.get("chat_id") or (next(iter(allow)) if allow else None)

    while True:
        try:
            for update in tg.updates(offset):
                offset = update["update_id"] + 1

                callback = update.get("callback_query")
                if callback:
                    chat = (callback.get("message") or {}).get("chat") or {}
                    chat_id = chat.get("id")
                    message_id = (callback.get("message") or {}).get("message_id")
                    data = callback.get("data") or ""
                    if chat_id is None:
                        continue
                    if allow and chat_id not in allow:
                        tg.answer_callback(callback["id"], "Sem permissão.")
                        continue
                    provider = None if data in ("p:all", "all") else data[2:] if data.startswith("p:") else None
                    try:
                        accounts = api_accounts(tracker_url, tracker_token)
                        state.sample(accounts, time.time())
                        text, markup = render(accounts, provider, samples=state.data.get("samples"))
                        if message_id:
                            tg.edit(chat_id, message_id, text, markup)
                        else:
                            tg.send(chat_id, text, markup)
                        tg.answer_callback(callback["id"])
                    except TrackerError as exc:
                        tg.answer_callback(callback["id"], str(exc)[:180])
                    continue

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
                if command not in ("/uso", "/gpt", "/go", "/mistral"):
                    tg.send(chat_id, "Não conheço esse comando.\n\n" + HELP)
                    continue
                try:
                    accounts = api_accounts(tracker_url, tracker_token)
                    state.sample(accounts, time.time())
                    text, markup = render(accounts, provider, samples=state.data.get("samples"))
                    tg.send(chat_id, text, markup)
                except TrackerError as exc:
                    tg.send(chat_id, f"Não consegui ler o tracker: {escape(str(exc))}")

            now = time.time()
            if now >= next_tick:
                next_tick = now + every
                try:
                    accounts = api_accounts(tracker_url, tracker_token)
                    state.sample(accounts, now)
                    chat_id = chat_id_for_alerts()
                    if chat_id is not None:
                        if threshold > 0:
                            check_alerts(tg, chat_id, accounts, state, threshold)
                        maybe_digest(tg, chat_id, accounts, state, digest_at)
                except TrackerError as exc:
                    log(f"ciclo: {exc}")
        except Exception as exc:  # keep the loop alive; a bot that dies is worse than a noisy one
            log(f"erro no ciclo: {type(exc).__name__}: {exc}")
            time.sleep(5)


def check_alerts(tg: "Telegram", chat_id, accounts: list[dict], state: "State", threshold: float) -> None:
    alerts: dict = state.data.setdefault("alerts", {})
    changed = False
    for account in accounts:
        for quota in account.get("quotas") or []:
            key = f"{account.get('id')}:{quota.get('window')}"
            used = float(quota.get("used_percent") or 0)
            previous = float(alerts.get(key, 0))
            if used >= threshold and previous < threshold:
                left = max(0.0, 100.0 - used)
                tg.send(chat_id, f"⚠️ *{escape(heading(account))}* — {quota.get('window')} a "
                                 f"{used:.0f}% usado ({left:.0f}% livre).")
            elif previous >= threshold and used < 5:
                tg.send(chat_id, f"✅ *{escape(heading(account))}* — {quota.get('window')} reposto.")
            if abs(previous - used) > 0.01:
                alerts[key] = used
                changed = True
    if changed:
        state.save()


def maybe_digest(tg: "Telegram", chat_id, accounts: list[dict], state: "State", digest_at: str) -> None:
    if not digest_at:
        return
    try:
        hour, minute = (int(x) for x in digest_at.split(":"))
    except ValueError:
        log(f"DIGEST_AT inválido: {digest_at!r}")
        return
    tz = tzinfo()
    now = datetime.now(tz)
    today = now.strftime("%Y-%m-%d")
    if state.data.get("last_digest") == today:
        return
    if (now.hour, now.minute) < (hour, minute):
        return
    tg.send(chat_id, digest(accounts, time.time()))
    state.data["last_digest"] = today
    state.save()


if __name__ == "__main__":
    sys.exit(main())
