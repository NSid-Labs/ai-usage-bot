import unittest

import bot


def account(**over):
    base = {
        "id": 1,
        "name": "ChatGPT",
        "provider": "chatgpt-sub",
        "plan": "plus",
        "last_poll": 1_700_000_000,
        "quotas": [
            {"window": "5h", "used_percent": 18, "resets_at": 1_700_003_600},
            {"window": "7d", "used_percent": 3, "resets_at": 1_700_500_000},
        ],
    }
    base.update(over)
    return base


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


class RenderTests(unittest.TestCase):
    def test_render_account(self):
        text = bot.render([account()], now=1_700_000_000)
        self.assertIn("*ChatGPT*", text)
        self.assertIn("plus", text)
        self.assertIn("5 horas", text)
        self.assertIn("82% livre", text)
        self.assertIn("repõe em", text)
        self.assertIn("semana", text)

    def test_detail_and_error(self):
        text = bot.render([account(last_error="boom", quotas=[
            {"window": "monthly", "used_percent": 5, "detail": "6.48 of 127.50", "resets_at": 0},
        ])], now=1_700_000_000)
        self.assertIn("erro: boom", text)
        self.assertIn("6.48 of 127.50", text)
        self.assertNotIn("repõe em", text)

    def test_filter(self):
        self.assertIn("Não tenho essa conta", bot.render([account()], "mistral-vibe"))

    def test_empty_tracker(self):
        self.assertIn("Ainda não há contas", bot.render([]))

    def test_escaping(self):
        text = bot.render([account(name="a_b*c")], now=1_700_000_000)
        self.assertIn("a\\_b\\*c", text)


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


class ChunkTests(unittest.TestCase):
    def test_chunks_keep_lines(self):
        text = "\n".join(f"linha {i}" for i in range(500))
        parts = bot.chunks(text, 100)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 100 for p in parts))
        self.assertEqual("\n".join(parts).split(), text.split())

    def test_short(self):
        self.assertEqual(bot.chunks("oi", 100), ["oi"])


class AllowlistTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(bot.allowed_chats(""), set())
        self.assertEqual(bot.allowed_chats("1, 2,-3"), {1, 2, -3})
        self.assertEqual(bot.allowed_chats("x,4"), {4})

    def test_provider_labels(self):
        self.assertEqual(bot.provider_label({"provider": "mistral-vibe"}), "Mistral Vibe")
        self.assertEqual(bot.provider_label({"provider": "nope"}), "nope")


if __name__ == "__main__":
    unittest.main()
