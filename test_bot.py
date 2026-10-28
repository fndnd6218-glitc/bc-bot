import tempfile
import unittest
from pathlib import Path
from bot import Bot, CATEGORIES, Store, safe_url


class FakeAPI:
    def __init__(self):
        self.calls = []

    def call(self, method, **data):
        self.calls.append((method, data))
        return {"message_id": len(self.calls)}


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "db.sqlite3")
        self.s = Store(self.path)
        self.api = FakeAPI()
        self.bot = Bot(self.api, self.s, {100})

    def tearDown(self):
        self.s.db.close()
        self.tmp.cleanup()

    def message(self, uid, text=None, document=None):
        m = {"from": {"id": uid}, "chat": {"id": uid, "type": "private"}}
        if text is not None:
            m["text"] = text
        if document:
            m["document"] = document
        self.bot.update({"message": m})

    def callback(self, uid, data):
        self.bot.update({"callback_query": {"id": "cb", "from": {"id": uid}, "data": data,
            "message": {"chat": {"id": uid, "type": "private"}}}})

    def draft(self, category="warp"):
        self.callback(100, "add")
        self.callback(100, "category:" + category)
        self.message(100, "Тестовый <материал>")
        self.message(100, "Инструкция & версия")

    def publish(self, category="warp", payload=None):
        self.draft(category)
        if payload is None:
            self.message(100, document={"file_id": "file-123", "file_name": "warp.conf"})
        else:
            self.message(100, payload)
        d = self.s.session(100)
        self.callback(100, "publish:" + d["nonce"])
        return self.s.run("SELECT max(id) FROM items").fetchone()[0]

    def test_file_publication_and_free_delivery(self):
        iid = self.publish()
        self.callback(200, f"get:{iid}")
        files = [p for m, p in self.api.calls if m == "sendDocument"]
        self.assertEqual(files[-1]["document"], "file-123")
        self.assertEqual(files[-1]["reply_markup"]["inline_keyboard"][0][0]["style"], "danger")
        self.assertEqual(self.s.run("SELECT count(*) FROM deliveries").fetchone()[0], 1)

    def test_repeated_publish_is_idempotent(self):
        self.draft()
        self.message(100, "https://example.com/download")
        nonce = self.s.session(100)["nonce"]
        self.callback(100, "publish:" + nonce)
        self.callback(100, "publish:" + nonce)
        self.assertEqual(self.s.run("SELECT count(*) FROM items").fetchone()[0], 1)

    def test_non_admin_cannot_publish_or_toggle(self):
        iid = self.publish()
        for data in ["add", "publish:any", "toggle:"+str(iid), "manage:0", "stats", "draftback:preview", "keep:title"]:
            self.callback(200, data)
        self.assertEqual(self.s.item(iid)["active"], 1)
        self.assertEqual(self.s.session(200), {})

    def test_hidden_material_not_delivered_via_old_button(self):
        iid = self.publish()
        self.callback(100, f"toggle:{iid}")
        self.callback(200, f"get:{iid}")
        self.assertFalse(any(m == "sendDocument" for m, _ in self.api.calls))
        self.assertEqual(self.s.listing("warp", 0, 200), [])

    def test_red_back_keeps_draft_and_can_keep_title(self):
        self.draft()
        self.callback(100, "draftback:payload")
        self.assertEqual(self.s.session(100)["stage"], "description")
        self.callback(100, "draftback:description")
        self.assertEqual(self.s.session(100)["title"], "Тестовый <материал>")
        self.callback(100, "keep:title")
        self.assertEqual(self.s.session(100)["stage"], "description")
        for method, msg in self.api.calls:
            for row in msg.get("reply_markup", {}).get("inline_keyboard", []):
                for b in row:
                    if b["text"].startswith("‹"):
                        self.assertEqual(b["style"], "danger")

    def test_cyrillic_search_and_html_escaping(self):
        self.publish()
        self.callback(200, "search")
        self.message(200, "ТЕСТОВЫЙ")
        rows = self.s.listing("search", 0, 200)
        self.assertEqual(len(rows), 1)
        self.bot.card(200, rows[0]["id"])
        self.assertIn("&lt;материал&gt;", self.api.calls[-1][1]["text"])

    def test_favorites_and_unique_reports(self):
        iid = self.publish()
        self.callback(200, f"fav:{iid}")
        self.assertEqual(len(self.s.listing("favorites", 0, 200)), 1)
        self.callback(200, f"report:{iid}")
        self.callback(200, f"report:{iid}")
        self.assertEqual(self.s.run("SELECT count(*) FROM reports").fetchone()[0], 1)
        self.callback(200, f"fav:{iid}")
        self.assertEqual(self.s.listing("favorites", 0, 200), [])

    def test_dns_text_delivery(self):
        iid = self.publish("dns", "1.1.1.1\n1.0.0.1\nВпиши адреса в настройки DNS роутера.")
        self.callback(200, f"get:{iid}")
        self.assertEqual(self.s.item(iid)["kind"], "text")
        self.assertIn("1.1.1.1", self.api.calls[-1][1]["text"])

    def test_news_text_and_ai_categories(self):
        self.publish("news", "Новость: обновлены материалы каталога.")
        self.assertEqual(len(self.s.listing("news", 0, 200)), 1)
        self.assertIn("ai", CATEGORIES)
        self.bot.home(200)
        buttons = [b for row in self.api.calls[-1][1]["reply_markup"]["inline_keyboard"] for b in row]
        for category in ("dns", "news", "ai"):
            self.assertTrue(any(b.get("callback_data") == "list:"+category+":0" for b in buttons))

    def test_state_survives_restart(self):
        iid = self.publish()
        self.s.set_session(100, {"stage": "title", "category": "ai"})
        other = Store(self.path)
        try:
            self.assertIsNotNone(other.item(iid))
            self.assertEqual(other.session(100)["category"], "ai")
        finally:
            other.db.close()

    def test_no_group_uploads_or_admin_actions(self):
        self.bot.update({"message": {"from": {"id": 100}, "chat": {"id": -123, "type": "group"}, "text": "/admin"}})
        self.assertEqual(self.api.calls, [])

    def test_proxy_validation_and_normalization(self):
        self.assertEqual(safe_url("tg://proxy?server=example.org&port=443&secret=abc", True),
                         "https://t.me/proxy?server=example.org&port=443&secret=abc")
        self.assertIn("/socks?", safe_url("https://t.me/socks?server=example.org&port=1080", True))
        for url in ("javascript:alert(1)", "http://example.com", "https://t.me/proxy?server=x&port=99999&secret=s", "https://example.com"):
            with self.assertRaises(ValueError):
                safe_url(url, True)


if __name__ == "__main__":
    unittest.main()
