"""WSGI webhook entrypoint for a single Gunicorn worker on Render Free."""
import hmac
import json
import logging
import os
import re
import threading
import time
from bot import APIError, Bot, CATEGORIES, Telegram, load_env, safe_url
from postgres_store import PostgresStore

log = logging.getLogger("bc-bot")
REQUIRED = ("BOT_TOKEN", "DATABASE_URL", "WEBHOOK_SECRET")


class CloudApp:
    def __init__(self, config=None, store_factory=PostgresStore, api_factory=Telegram):
        self.config = dict(os.environ if config is None else config)
        self.store_factory, self.api_factory = store_factory, api_factory
        self.bot = None
        self.lock = threading.Lock()
        self.error = ""

    def missing(self):
        missing = [key for key in REQUIRED if not self.config.get(key)]
        if not (self.config.get("APP_URL") or self.config.get("RENDER_EXTERNAL_URL")):
            missing.append("APP_URL")
        return missing

    def initialize(self):
        with self.lock:
            if self.bot or self.missing():
                return bool(self.bot)
            c = self.config
            if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", c["BOT_TOKEN"]):
                raise ValueError("Invalid BOT_TOKEN")
            if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", c["WEBHOOK_SECRET"]):
                raise ValueError("Invalid WEBHOOK_SECRET")
            admins = {int(v.strip()) for v in c.get("ADMIN_IDS", "").split(",") if v.strip()}
            if any(uid <= 0 for uid in admins):
                raise ValueError("Invalid ADMIN_IDS")
            name = c.get("BOT_NAME", "BC-bot")
            if not 1 <= len(name) <= 64:
                raise ValueError("Invalid BOT_NAME")
            icons = json.loads(c.get("CUSTOM_EMOJI_IDS", "{}"))
            if not isinstance(icons, dict) or any(k not in CATEGORIES or not isinstance(v, str) or not v.isdigit() for k, v in icons.items()):
                raise ValueError("Invalid CUSTOM_EMOJI_IDS")
            base = safe_url(c.get("APP_URL") or c["RENDER_EXTERNAL_URL"]).rstrip("/")
            support = c.get("SUPPORT_URL", "https://t.me/N777NN52")
            if support:
                support = safe_url(support)
            store = self.store_factory(c["DATABASE_URL"])
            try:
                api = self.api_factory(c["BOT_TOKEN"])
                api.call("getMe")
                api.call("setMyName", name=name)
                api.call("setMyCommands", commands=[
                    {"command": "start", "description": "Открыть каталог"},
                    {"command": "id", "description": "Мой Telegram ID"},
                    {"command": "cancel", "description": "Отмена"}])
                api.call("setWebhook", url=base+"/telegram", secret_token=c["WEBHOOK_SECRET"],
                         allowed_updates=["message", "callback_query"], max_connections=1,
                         drop_pending_updates=False)
                self.bot = Bot(api, store, admins, name, icons, support)
                self.error = ""
                log.info("Webhook configured; administrators configured: %s", bool(admins))
            except Exception:
                store.db.close()
                raise
            return True

    def bootstrap(self):
        # Run only in the Gunicorn worker (post_worker_init), never in the preloaded master.
        while not self.bot and not self.missing():
            try:
                if self.initialize():
                    return
            except Exception as exc:
                self.error = type(exc).__name__
                log.warning("Bot initialization failed (%s); retrying", self.error)
            time.sleep(20)

    @staticmethod
    def response(start_response, status, body, content_type="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
        start_response(status, [("Content-Type", content_type), ("Content-Length", str(len(data))),
                                ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff")])
        return [data]

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "/")
        if path in {"/", "/health"} and environ.get("REQUEST_METHOD") == "GET":
            # Liveness is intentionally independent from bot readiness during secret setup.
            return self.response(start_response, "200 OK", {"app": "BC-bot", "bot_ready": bool(self.bot)})
        if path == "/ready" and environ.get("REQUEST_METHOD") == "GET":
            return self.response(start_response, "200 OK" if self.bot else "503 Service Unavailable", {"ready": bool(self.bot)})
        if path != "/telegram" or environ.get("REQUEST_METHOD") != "POST":
            return self.response(start_response, "404 Not Found", {"error": "Not found"})
        expected = self.config.get("WEBHOOK_SECRET", "")
        actual = environ.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN", "")
        if not expected or not hmac.compare_digest(expected.encode(), actual.encode()):
            return self.response(start_response, "403 Forbidden", {"error": "Forbidden"})
        if not self.bot:
            return self.response(start_response, "503 Service Unavailable", {"error": "Bot not configured"})
        try:
            size = int(environ.get("CONTENT_LENGTH", "0"))
            if not 0 < size <= 1024*1024:
                return self.response(start_response, "413 Payload Too Large", {"error": "Invalid size"})
            update = json.loads(environ["wsgi.input"].read(size))
            if not isinstance(update, dict) or type(update.get("update_id")) is not int:
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return self.response(start_response, "400 Bad Request", {"error": "Invalid update"})
        with self.lock:
            try:
                s = self.bot.s
                if getattr(s.db, "closed", False):
                    s = self.store_factory(self.config["DATABASE_URL"])
                    self.bot.s = s
                if s.processed(update["update_id"]):
                    return self.response(start_response, "200 OK", {"ok": True})
                self.bot.update(update)
                s.mark_processed(update["update_id"])
                return self.response(start_response, "200 OK", {"ok": True})
            except APIError as exc:
                log.warning("Telegram request failed (code %s)", exc.code)
                # Let Telegram retry transient failures. Permanent errors must not block its queue.
                if exc.code in {0, 429} or exc.code >= 500:
                    return self.response(start_response, "503 Service Unavailable", {"error": "Temporary failure"})
                return self.response(start_response, "200 OK", {"ok": True})
            except Exception as exc:
                log.warning("Update failed (%s)", type(exc).__name__)
                return self.response(start_response, "503 Service Unavailable", {"error": "Temporary failure"})


load_env()
app = CloudApp()
