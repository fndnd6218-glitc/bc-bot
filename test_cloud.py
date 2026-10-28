import io
import json
import tempfile
import unittest
from pathlib import Path
from bot import APIError, Store
from cloud import CloudApp
from postgres_store import PostgresStore


class TestStore(Store):
    def __init__(self, path):
        super().__init__(path)
        self.run("CREATE TABLE processed_updates(id INTEGER PRIMARY KEY)")

    def processed(self, update_id):
        return bool(self.run("SELECT 1 FROM processed_updates WHERE id=?", (update_id,)).fetchone())

    def mark_processed(self, update_id):
        self.run("INSERT INTO processed_updates VALUES(?)", (update_id,))


class API:
    def __init__(self, token):
        self.calls = []
        self.fail = False

    def call(self, method, **data):
        self.calls.append((method, data))
        if self.fail and method == "sendMessage":
            raise APIError(429)
        return True


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = {"BOT_TOKEN": "12345:" + "x"*24, "DATABASE_URL": str(Path(self.tmp.name)/"db.sqlite3"),
            "WEBHOOK_SECRET": "s"*40, "APP_URL": "https://example.onrender.com"}
        self.app = CloudApp(self.config, TestStore, API)
        self.app.initialize()

    def tearDown(self):
        self.app.bot.s.db.close()
        self.tmp.cleanup()

    def request(self, body=None, path="/telegram", secret=None, method="POST", app=None):
        raw = json.dumps(body).encode() if body is not None else b""
        env = {"PATH_INFO": path, "REQUEST_METHOD": method, "CONTENT_LENGTH": str(len(raw)),
            "wsgi.input": io.BytesIO(raw), "HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": secret if secret is not None else self.config["WEBHOOK_SECRET"]}
        response = []
        data = b"".join((app or self.app)(env, lambda status, headers: response.append(status)))
        return response[0], json.loads(data)

    def update(self, update_id=1, text="/start"):
        return {"update_id": update_id, "message": {"chat": {"id": 200, "type": "private"},
            "from": {"id": 200, "first_name": "Анна"}, "text": text}}

    def test_start_and_duplicate_delivery(self):
        self.assertEqual(self.request(self.update())[0], "200 OK")
        count = len(self.app.bot.api.calls)
        self.assertEqual(self.request(self.update())[0], "200 OK")
        self.assertEqual(len(self.app.bot.api.calls), count)
        self.assertTrue(any("Анна" in p.get("text", "") for m,p in self.app.bot.api.calls))

    def test_forged_webhook_rejected_without_side_effects(self):
        count = len(self.app.bot.api.calls)
        self.assertEqual(self.request(self.update(), secret="wrong")[0], "403 Forbidden")
        self.assertEqual(len(self.app.bot.api.calls), count)

    def test_missing_settings_cannot_accept_updates(self):
        app = CloudApp({})
        self.assertEqual(self.request(path="/health", method="GET", app=app)[1]["bot_ready"], False)
        self.assertEqual(self.request(path="/ready", method="GET", app=app)[0], "503 Service Unavailable")
        self.assertEqual(self.request(self.update(), app=app)[0], "403 Forbidden")

    def test_invalid_payload_rejected(self):
        self.assertEqual(self.request({"update_id": "fake"})[0], "400 Bad Request")

    def test_transient_telegram_failure_is_retried(self):
        self.app.bot.api.fail = True
        self.assertEqual(self.request(self.update())[0], "503 Service Unavailable")
        self.assertFalse(self.app.bot.s.processed(1))
        self.app.bot.api.fail = False
        self.assertEqual(self.request(self.update())[0], "200 OK")
        self.assertTrue(self.app.bot.s.processed(1))

    def test_admin_id_bootstrap_does_not_grant_admin_rights(self):
        self.request(self.update(text="/id"))
        self.assertIn("200", self.app.bot.api.calls[-1][1]["text"])
        self.assertEqual(self.app.bot.admins, set())

    def test_webhook_config_keeps_queue_and_authenticates(self):
        config = next(p for m,p in self.app.bot.api.calls if m == "setWebhook")
        self.assertFalse(config["drop_pending_updates"])
        self.assertEqual(config["max_connections"], 1)
        self.assertEqual(config["secret_token"], self.config["WEBHOOK_SECRET"])
        self.assertEqual(config["url"], "https://example.onrender.com/telegram")

    def test_postgres_conflicts_use_valid_placeholder_style(self):
        sql = PostgresStore.translate("INSERT OR IGNORE INTO users(id) VALUES(?)")
        self.assertEqual(sql, "INSERT INTO users(id) VALUES(%s) ON CONFLICT DO NOTHING")
        self.assertIn("ON CONFLICT (user_id)", PostgresStore.translate("INSERT OR REPLACE INTO sessions VALUES(?,?)"))


if __name__ == "__main__":
    unittest.main()
