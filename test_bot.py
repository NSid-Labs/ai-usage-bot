import os
import unittest
from datetime import datetime

os.environ.setdefault("TZ", "Europe/Lisbon")

import bot

NOW = 1_700_000_000  # 2023-11-14 22:13:20 UTC / 23:13 Lisbon


def account(**over):
    base = {
        "id": 1,
        "name": "ChatGPT",
        "provider": "chatgpt-sub",
        "plan": "plus",
        "last_poll": NOW,
        "quotas": [
            {"window": "7d", "used_percent": 31, "resets_at": NOW + 6 * 86400},
            {"window": "5h", "used_percent": 18, "resets_at": NOW + 3600},
        ],
    }
    base.update(over)
    return base


def code_block(text):
    start = text.index("```")
    end = text.index("```", start + 3)
    return [line for line in text[start + 3:end].strip("\n").split("\n")]


class BarTests(unittest.TestCase):
    def test_bar(self):
        self.assertEqual(bot.bar(0), "░" * 10)
        self.assertEqual(bot.bar(100), "█" * 10)
        self.assertEqual(bot.bar(50), "█" * 5 + "░" * 5)
        self.assertEqual(bot.bar(-10), "░" * 10)
        self.assertEqual(bot.bar(150), "█" * 10)


class HumanLeftTests(unittest.TestCase):
    def test_windows(self):
        self.assertEqual(bot.human_left(-5), "já reposto")
        self.assertEqual(bot.human_left(59), "0m")
        self.assertEqual(bot.human_left(4 * 3600 + 12 * 60), "4h12m")
        self.assertEqual(bot.human_left(6 * 86400 + 3 * 3600), "6d03h")


class FormatTests(unittest.TestCase):
    def test_money_detail(self):
        self.assertEqual(bot.money_detail("6.48 of 127.50"), "6,48 € de 127,50 €")
        self.assertEqual(bot.money_detail("qualquer coisa"), "qualquer coisa")
        self.assertEqual(bot.money_detail(""), "")

    def test_money_detail_currency_override(self):
        os.environ["CURRENCY"] = "$"
        try:
            self.assertEqual(bot.money_detail("1 of 2"), "1,00 $ de 2,00 $")
        finally:
            os.environ.pop("CURRENCY", None)

    def test_local_stamp_same_day_vs_other_day(self):
        self.assertRegex(bot.local_stamp(NOW + 3600, NOW), r"^\d{2}:\d{2}$")
        self.assertRegex(bot.local_stamp(NOW + 6 * 86400, NOW), r"^\d{2}/\d{2}$")

    def test_windows_are_ordered(self):
        quotas = bot.ordered_quotas(account(quotas=[
            {"window": "monthly", "used_percent": 1},
            {"window": "weekly", "used_percent": 2},
            {"window": "5h", "used_percent": 3},
        ]))
        self.assertEqual([q["window"] for q in quotas], ["5h", "weekly", "monthly"])

    def test_plan_label(self):
        self.assertEqual(bot.plan_label("go plus"), "Go Plus")
        self.assertEqual(bot.plan_label("Student"), "Student")
        self.assertEqual(bot.plan_label(""), "")


class AlignmentTests(unittest.TestCase):
    def test_bars_start_in_the_same_column(self):
        card = bot.account_card(account(quotas=[
            {"window": "5h", "used_percent": 1, "resets_at": NOW + 3600},
            {"window": "weekly", "used_percent": 0, "resets_at": NOW + 86400},
            {"window": "monthly", "used_percent": 12, "resets_at": NOW + 9 * 86400},
        ]), NOW)
        rows = code_block(card)
        columns = {min(r.index("█"), r.index("░")) if "█" in r else r.index("░") for r in rows}
        self.assertEqual(len(columns), 1, rows)
        # And the percentage column is aligned too.
        pcts = {r.index("%") for r in rows}
        self.assertEqual(len(pcts), 1, rows)

    def test_labels_are_padded(self):
        rows = code_block(bot.account_card(account(), NOW))
        self.assertTrue(rows[0].startswith("5h   "))
        self.assertTrue(rows[1].startswith("sem  "))


class RenderTests(unittest.TestCase):
    def test_summary(self):
        text, markup = bot.render([account()], now=NOW)
        self.assertIn("📊 *Consumo*", text)
        self.assertIn("*ChatGPT · Plus*", text)
        self.assertIn("actualizado", text)
        self.assertEqual(len(markup["inline_keyboard"]), 2)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "p:chatgpt-sub")

    def test_tightest_only_when_busy(self):
        text, _ = bot.render([account()], now=NOW)
        self.assertNotIn("mais apertado", text)
        text, _ = bot.render([account(quotas=[{"window": "5h", "used_percent": 62}])], now=NOW)
        self.assertIn("mais apertado", text)
        self.assertIn("62% usado", text)

    def test_provider_view_has_back_button(self):
        text, markup = bot.render([account()], "chatgpt-sub", now=NOW)
        self.assertIn("📊 *ChatGPT*", text)
        self.assertEqual(markup["inline_keyboard"][0][0]["text"], "◀ Voltar")

    def test_unknown_provider(self):
        text, _ = bot.render([account()], "mistral-vibe", now=NOW)
        self.assertIn("Não tenho essa conta", text)

    def test_empty_tracker(self):
        text, _ = bot.render([], now=NOW)
        self.assertIn("Ainda não há contas", text)

    def test_account_without_quotas_shows_spend(self):
        text, _ = bot.render([account(provider="openrouter", name="OpenRouter", plan="free tier",
                                      quotas=[], cost_today_usd=0)], now=NOW)
        self.assertIn("sem quotas · $0.00 hoje", text)
        self.assertNotIn("```", text)

    def test_credit_account_shows_balance(self):
        text, _ = bot.render([account(provider="refuseless", name="Refuseless", plan=None,
                                      quotas=[], balance_usd=2.89)], now=NOW)
        self.assertIn("*Refuseless*", text)
        self.assertIn("saldo $2.89", text)
        self.assertNotIn("sem quotas", text)

    def test_detail_is_localised(self):
        text, _ = bot.render([account(provider="mistral-vibe", name="Mistral", plan="Student", quotas=[
            {"window": "monthly", "used_percent": 5, "detail": "6.48 of 127.50", "resets_at": NOW + 86400},
        ])], now=NOW)
        self.assertIn("6,48 € de 127,50 €", text)

    def test_escape(self):
        text, _ = bot.render([account(name="a_b*c")], now=NOW)
        self.assertIn("a\\_b\\*c", text)


class PaceTests(unittest.TestCase):
    def test_runs_out_before_reset(self):
        samples = {"1:5h": [[NOW - 3600, 10.0], [NOW, 20.0]]}
        quota = {"window": "5h", "used_percent": 20, "resets_at": NOW + 86400}
        self.assertEqual(bot.pace_note(account(), quota, samples, NOW), "100% em 8h00m")

    def test_no_note_when_reset_comes_first(self):
        samples = {"1:5h": [[NOW - 3600, 10.0], [NOW, 20.0]]}
        quota = {"window": "5h", "used_percent": 20, "resets_at": NOW + 3600}
        self.assertEqual(bot.pace_note(account(), quota, samples, NOW), "")

    def test_needs_history(self):
        quota = {"window": "5h", "used_percent": 20, "resets_at": NOW + 86400}
        self.assertEqual(bot.pace_note(account(), quota, {"1:5h": [[NOW, 20.0]]}, NOW), "")
        self.assertEqual(bot.pace_note(account(), quota, {"1:5h": [[NOW - 60, 19.0], [NOW, 20.0]]}, NOW), "")


class StateTests(unittest.TestCase):
    def test_samples_are_kept_per_quota(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            state = bot.State(tmp)
            state.sample([account()], NOW)
            state.sample([account()], NOW + 300)
            key = "1:5h"
            self.assertEqual([p[1] for p in state.data["samples"][key]], [18.0, 18.0])
            reloaded = bot.State(tmp)
            self.assertEqual(len(reloaded.data["samples"][key]), 2)

    def test_allowed_chats(self):
        self.assertEqual(bot.allowed_chats(""), set())
        self.assertEqual(bot.allowed_chats("1, 2,-3"), {1, 2, -3})
        self.assertEqual(bot.allowed_chats("x,4"), {4})


class DigestTests(unittest.TestCase):
    def test_digest_lists_resets_today(self):
        now = datetime(2026, 11, 16, 9, 0, tzinfo=bot.tzinfo()).timestamp()
        text = bot.digest([account(quotas=[
            {"window": "5h", "used_percent": 55, "resets_at": now + 2 * 3600},
        ])], now)
        self.assertIn("Resumo", text)
        self.assertIn("mais apertado", text)
        self.assertIn("repõe hoje", text)
        self.assertIn("11:00", text)


class ChunkTests(unittest.TestCase):
    def test_chunks_keep_lines(self):
        text = "\n".join(f"linha {i}" for i in range(500))
        parts = bot.chunks(text, 100)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 100 for p in parts))
        self.assertEqual("\n".join(parts).split(), text.split())

    def test_short(self):
        self.assertEqual(bot.chunks("oi", 100), ["oi"])


class ApiTests(unittest.TestCase):
    def setUp(self):
        self._old = bot.urllib.request.urlopen

    def tearDown(self):
        bot.urllib.request.urlopen = self._old

    def test_sends_bot_user_agent_and_parses(self):
        captured = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'[{"id":1}]'

        def fake(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            return Resp()

        bot.urllib.request.urlopen = fake
        self.assertEqual(bot.api_accounts("https://x/", "tok"), [{"id": 1}])
        self.assertEqual(captured["url"], "https://x/api/accounts")
        self.assertEqual(captured["headers"]["authorization"], "Bearer tok")
        # Cloudflare blocks a bare python-urllib UA with 403/1010.
        self.assertIn("ai-usage-bot", captured["headers"]["user-agent"])

    def _raise(self, code):
        def fake(req, timeout=None):
            raise bot.urllib.error.HTTPError(req.full_url, code, "nope", {}, None)
        bot.urllib.request.urlopen = fake
        with self.assertRaises(bot.TrackerError) as ctx:
            bot.api_accounts("https://x", "tok")
        return str(ctx.exception)

    def test_401_is_a_token_problem(self):
        self.assertIn("401", self._raise(401))

    def test_403_mentions_cloudflare(self):
        self.assertIn("Cloudflare", self._raise(403))


if __name__ == "__main__":
    unittest.main()
