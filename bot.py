#!/usr/bin/env python3
"""FreeWarpBot: private-chat catalogue, Python 3.11+, no pip dependencies."""
import html
import json
import logging
import os
import re
import signal
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from pathlib import Path

CATEGORIES = {"warp": "⚡ WARP", "vpn": "🛡 VPN-приложения", "proxy": "🌐 Telegram-прокси",
              "dns": "🧩 DNS", "news": "📰 Новости", "ai": "🤖 ИИ", "other": "📦 Другое"}
PAGE = 6
log = logging.getLogger("freewarp")


def load_env(path=".env"):
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def safe_url(value, proxy=False):
    """Validate links without connecting to their destination."""
    if len(value) > 2000 or re.search(r"[\s\x00-\x1f]", value):
        raise ValueError("Пришли одну ссылку без пробелов, длиной до 2000 символов.")
    p = urllib.parse.urlsplit(value)
    if p.username or p.password:
        raise ValueError("Логин и пароль в адресе сайта не поддерживаются.")
    is_proxy = (p.scheme == "tg" and p.netloc in {"proxy", "socks"}) or (
        p.scheme == "https" and p.netloc == "t.me" and p.path in {"/proxy", "/socks"})
    if proxy or p.scheme == "tg":
        if not is_proxy:
            raise ValueError("Нужна ссылка https://t.me/proxy?... или tg://proxy?... (также SOCKS5).")
        q = urllib.parse.parse_qs(p.query)
        try:
            port = int(q.get("port", [""])[0])
        except ValueError:
            port = 0
        kind = p.netloc if p.scheme == "tg" else p.path[1:]
        if not q.get("server") or not 1 <= port <= 65535 or (kind == "proxy" and not q.get("secret")):
            raise ValueError("В прокси нужны server, port (1–65535), а для MTProxy ещё secret.")
        # HTTPS deep links work in inline URL buttons on Telegram clients.
        return "https://t.me/" + kind + "?" + urllib.parse.urlencode({k: v[0] for k, v in q.items()})
    if p.scheme != "https" or not p.hostname:
        raise ValueError("Для приложения нужна полная HTTPS-ссылка или файл.")
    return value


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, created TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS items(
          id INTEGER PRIMARY KEY, category TEXT NOT NULL, title TEXT NOT NULL,
          description TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
          filename TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
          created TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS favorites(user_id INTEGER, item_id INTEGER REFERENCES items(id),
          PRIMARY KEY(user_id,item_id));
        CREATE TABLE IF NOT EXISTS sessions(user_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS deliveries(id INTEGER PRIMARY KEY, user_id INTEGER, item_id INTEGER,
          created TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS reports(user_id INTEGER, item_id INTEGER,
          created TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id,item_id));
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    def run(self, sql, args=()):
        with self.db:
            return self.db.execute(sql, args)

    def session(self, uid):
        row = self.run("SELECT data FROM sessions WHERE user_id=?", (uid,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set_session(self, uid, data):
        self.run("INSERT OR REPLACE INTO sessions VALUES(?,?)", (uid, json.dumps(data)))

    def item(self, item_id, admin=False):
        return self.run("SELECT * FROM items WHERE id=?" + ("" if admin else " AND active=1"), (item_id,)).fetchone()

    def publish(self, uid, nonce):
        # Draft consumption and insertion are one transaction; repeated presses are harmless.
        with self.db:
            draft = self.session(uid)
            if draft.get("stage") != "preview" or draft.get("nonce") != nonce:
                return None
            cur = self.db.execute("INSERT INTO items(category,title,description,kind,payload,filename) VALUES(?,?,?,?,?,?)",
                                  tuple(draft.get(k, "") for k in ("category", "title", "description", "kind", "payload", "filename")))
            self.db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            return cur.lastrowid

    def listing(self, mode, page, uid, admin=False):
        args = []
        sql = "SELECT i.* FROM items i WHERE " + ("1=1" if admin else "i.active=1")
        if mode in CATEGORIES:
            sql += " AND category=?"
            args.append(mode)
        elif mode == "favorites":
            sql += " AND EXISTS(SELECT 1 FROM favorites f WHERE f.item_id=i.id AND f.user_id=?)"
            args.append(uid)
        elif mode == "search":
            query = self.session(uid).get("query", "")
            sql += " AND (instr(lower(title),lower(?))>0 OR instr(lower(description),lower(?))>0)"
            args.extend([query, query])
        elif mode != "all":
            raise ValueError("Неизвестный раздел")
        # Python casefold supports Cyrillic as well as Latin; SQLite lower() does not.
        if mode == "search":
            rows = self.run("SELECT * FROM items WHERE active=1 ORDER BY id DESC").fetchall()
            query = self.session(uid).get("query", "").casefold()
            rows = [r for r in rows if query in (r["title"] + "\n" + r["description"]).casefold()]
            return rows[page * PAGE:(page + 1) * PAGE + 1]
        return self.run(sql + " ORDER BY id DESC LIMIT ? OFFSET ?", (*args, PAGE + 1, page * PAGE)).fetchall()


class APIError(Exception):
    def __init__(self, code, retry_after=0):
        self.code, self.retry_after = code, retry_after
        super().__init__(f"Telegram API error {code}")  # Never print token-bearing URLs.


class Telegram:
    def __init__(self, token):
        self.base = "https://api.telegram.org/bot" + token + "/"

    def call(self, method, **data):
        req = urllib.request.Request(self.base + method, data=json.dumps(data).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=40) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                result = json.loads(exc.read())
            except (ValueError, OSError):
                raise APIError(exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise APIError(0) from None
        if not result.get("ok"):
            raise APIError(result.get("error_code", 0), result.get("parameters", {}).get("retry_after", 0))
        return result["result"]


class Bot:
    def __init__(self, api, store, admins, name="BC-bot", icons=None, support="https://t.me/N777NN52"):
        self.api, self.s, self.admins = api, store, set(admins)
        self.name, self.icons, self.support = name, icons or {}, support
        self.rates = defaultdict(deque)

    def button(self, text, data=None, url=None, style=None, icon=None):
        b = {"text": text}
        b["url" if url else "callback_data"] = url or data
        if "Назад" in text or text.startswith("‹"):
            style = "danger"
        if style:
            b["style"] = style
        if icon and self.icons.get(icon):
            b["icon_custom_emoji_id"] = self.icons[icon]
        return b

    def send(self, uid, text, rows=None):
        args = {"chat_id": uid, "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        if rows:
            args["reply_markup"] = {"inline_keyboard": rows}
        return self.api.call("sendMessage", **args)

    def home(self, uid, first_name=None):
        rows = [[self.button(label, "list:" + key + ":0", style="danger", icon=key)] for key, label in CATEGORIES.items()]
        rows += [[self.button("⭐ Избранное", "list:favorites:0"), self.button("🔎 Поиск", "search")],
                 [self.button("🆕 Новинки", "list:all:0"), self.button("📖 Как пользоваться", "help")]]
        if self.support:
            rows.append([self.button("💬 Поддержка", url=self.support)])
        if uid in self.admins:
            rows.append([self.button("⚙️ Админ-панель", "admin")])
        greeting = f"Привет, <b>{html.escape(first_name)}</b>! 👋" if first_name else "Привет! 👋"
        self.send(uid, greeting + f"\nДобро пожаловать в <b>{html.escape(self.name)}</b>! ❤️\n\n"
                  "🚀 Здесь ты сможешь найти лучшие приложения для обхода блокировок, VPN и другие полезные инструменты!\n\n"
                  "⚡ WARP — конфиги и настройки\n"
                  "🛡 VPN — приложения и ссылки\n"
                  "🌐 Telegram-прокси — быстрое подключение\n"
                  "🧩 DNS — адреса серверов и инструкции\n"
                  "📰 Новости — свежие публикации\n"
                  "🤖 ИИ — сервисы и приложения\n\n"
                  "🎁 Все материалы в каталоге доступны бесплатно.\n"
                  "👇 Выбирай нужный раздел кнопками ниже!", rows)

    def admin(self, uid):
        self.send(uid, "<b>⚙️ Управление каталогом</b>\nДобавляй материалы, проверяй карточки и скрывай устаревшие.",
                  [[self.button("➕ Добавить материал", "add", style="success")],
                   [self.button("📚 Все материалы", "manage:0"), self.button("📊 Статистика", "stats")],
                   [self.button("‹ Назад", "home")]])

    def listing(self, uid, mode, page, admin=False):
        page = max(0, min(page, 100000))
        rows = self.s.listing(mode, page, uid, admin)
        buttons = [[self.button(("◼ " if not r["active"] else "") + r["title"], f"item:{r['id']}")] for r in rows[:PAGE]]
        nav = []
        prefix = "manage" if admin else "list:" + mode
        if page:
            nav.append(self.button("‹ Назад", f"{prefix}:{page-1}"))
        if len(rows) > PAGE:
            nav.append(self.button("Далее ›", f"{prefix}:{page+1}"))
        if nav:
            buttons.append(nav)
        buttons.append([self.button("‹ Назад в меню", "admin" if admin else "home")])
        title = "Управление материалами" if admin else CATEGORIES.get(mode, {"all": "🆕 Новинки", "favorites": "⭐ Избранное", "search": "🔎 Результаты поиска"}.get(mode, "Каталог"))
        self.send(uid, f"<b>{html.escape(title)}</b> · {page+1}\n\n" + ("Выбери материал:" if rows else "Пока пусто. Загляни позже."), buttons)

    def card(self, uid, iid):
        r = self.s.item(iid, uid in self.admins)
        if not r:
            return self.send(uid, "Материал скрыт или ещё не опубликован. Открой /start.")
        fav = self.s.run("SELECT 1 FROM favorites WHERE user_id=? AND item_id=?", (uid, iid)).fetchone()
        rows = []
        if r["active"]:
            # Callback sends the link too, allowing delivery statistics without tracking external clicks.
            label = {"proxy": "🌐 Получить прокси", "dns": "🧩 Получить DNS", "news": "📰 Читать"}.get(r["category"], "⬇️ Получить")
            rows.append([self.button(label, f"get:{iid}", style="success")])
            rows.append([self.button("⭐ Убрать из избранного" if fav else "☆ В избранное", f"fav:{iid}")])
            rows.append([self.button("⚠️ Не работает", f"report:{iid}")])
        if uid in self.admins:
            rows.append([self.button("🙈 Скрыть" if r["active"] else "👁 Опубликовать снова", f"toggle:{iid}", style="danger" if r["active"] else "success")])
        rows.append([self.button("‹ В раздел", f"list:{r['category']}:0"), self.button("🏠 Меню", "home")])
        self.send(uid, f"<b>{html.escape(r['title'])}</b>\n{CATEGORIES[r['category']]}"
                  + (" · Скрыт" if not r["active"] else "") + f"\n\n{html.escape(r['description'])}\n\n"
                  f"<i>Добавлено: {r['created'][:10]} · Бесплатно</i>", rows)

    def update(self, update):
        cb = update.get("callback_query")
        m = cb.get("message", {}) if cb else update.get("message", {})
        user = cb.get("from", {}) if cb else m.get("from", {})
        uid = user.get("id")
        if not uid or m.get("chat", {}).get("type") != "private":
            return
        if cb:
            try:
                self.api.call("answerCallbackQuery", callback_query_id=cb["id"])
            except APIError:
                pass
        now = time.monotonic()
        q = self.rates[uid]
        while q and now - q[0] > 10:
            q.popleft()
        if len(q) >= 12:
            return
        q.append(now)
        if len(self.rates) > 10000:
            self.rates = defaultdict(deque, {k: v for k, v in self.rates.items() if v and now-v[-1] < 60})
        self.s.run("INSERT OR IGNORE INTO users(id) VALUES(?)", (uid,))
        if cb:
            try:
                return self.callback(uid, cb.get("data", ""), user.get("first_name"))
            except (ValueError, IndexError):
                return self.send(uid, "Кнопка устарела. Открой /start.")
        text = m.get("text", "").strip()
        cmd = text.split()[0].split("@")[0] if text else ""
        if cmd in {"/start", "/cancel"}:
            self.s.set_session(uid, {})
            return self.home(uid, user.get("first_name"))
        if cmd == "/id":
            return self.send(uid, f"Твой Telegram ID: <code>{uid}</code>")
        if cmd == "/admin":
            return self.admin(uid) if uid in self.admins else self.send(uid, "Этот раздел доступен владельцу.")
        draft = self.s.session(uid)
        if draft.get("stage") == "search":
            if not text or len(text) > 100:
                return self.send(uid, "Введи запрос длиной 1–100 символов.")
            self.s.set_session(uid, {"query": text})
            return self.listing(uid, "search", 0)
        if uid in self.admins and draft.get("stage"):
            return self.upload(uid, m, draft)
        self.send(uid, "Выбери раздел в /start." + (" Для публикации открой /admin." if uid in self.admins else ""))

    def callback(self, uid, data, first_name=None):
        parts = data.split(":")
        action = parts[0]
        admin_actions = {"admin", "add", "category", "publish", "toggle", "manage", "stats", "draftback", "keep"}
        if action in admin_actions and uid not in self.admins:
            return self.send(uid, "Этот раздел доступен владельцу.")
        if action == "home":
            self.s.set_session(uid, {})
            return self.home(uid, first_name)
        if action == "admin":
            self.s.set_session(uid, {})
            return self.admin(uid)
        if action == "help":
            return self.send(uid, "<b>📖 Как пользоваться</b>\n\n"
                "⚡ WARP: скачай конфиг и импортируй в совместимое приложение, например WireGuard.\n"
                "🛡 VPN: открой карточку и получи приложение или ссылку.\n"
                "🌐 Прокси: нажми «Получить прокси», затем «Подключить» и подтверди в Telegram.\n\n"
                "🧩 DNS: получи адреса и следуй инструкции в карточке; настройки меняются на твоём устройстве.\n"
                "📰 Новости: читай публикации владельца каталога.\n"
                "🤖 ИИ: ссылки на сервисы и приложения. У внешних сервисов могут быть свои тарифы.\n\n"
                "Прокси работает для Telegram. Для VPN нужен отдельный клиент. Бот раздаёт материалы, "
                "а подключение выполняет приложение на твоём устройстве. Работоспособность зависит от самого сервера.\n\n"
                "⭐ Сохраняй полезное в избранное. Если материал устарел, нажми «Не работает».\n"
                "Для поиска используй кнопку в меню; /cancel отменяет добавление.", [[self.button("‹ Назад", "home")]])
        if action == "search":
            self.s.set_session(uid, {"stage": "search"})
            return self.send(uid, "🔎 Напиши название или часть описания. Для отмены — /cancel.", [[self.button("‹ Назад", "home")]])
        if action == "list":
            return self.listing(uid, parts[1], int(parts[2]))
        if action == "manage":
            return self.listing(uid, "all", int(parts[1]), True)
        if action == "item":
            return self.card(uid, int(parts[1]))
        if action in {"get", "fav", "report", "toggle"}:
            iid = int(parts[1])
            r = self.s.item(iid, action == "toggle")
            if not r:
                return self.send(uid, "Материал больше недоступен. Открой /start.")
            if action == "toggle":
                self.s.run("UPDATE items SET active=1-active WHERE id=?", (iid,))
                return self.card(uid, iid)
            if action == "get":
                if r["kind"] == "file":
                    self.api.call("sendDocument", chat_id=uid, document=r["payload"], caption=r["title"],
                                  reply_markup={"inline_keyboard": [[self.button("‹ Назад к карточке", f"item:{iid}")]]})
                elif r["kind"] == "text":
                    self.send(uid, f"<b>{html.escape(r['title'])}</b>\n\n{html.escape(r['payload'])}",
                              [[self.button("‹ Назад к карточке", f"item:{iid}")]])
                else:
                    self.send(uid, f"<b>{html.escape(r['title'])}</b>\n\n<code>{html.escape(r['payload'])}</code>",
                              [[self.button("🌐 Подключить" if r["category"] == "proxy" else "↗️ Открыть ссылку", url=r["payload"], style="success")],
                               [self.button("‹ Назад к карточке", f"item:{iid}")]])
                self.s.run("INSERT INTO deliveries(user_id,item_id) VALUES(?,?)", (uid, iid))
                return
            if action == "fav":
                old = self.s.run("SELECT 1 FROM favorites WHERE user_id=? AND item_id=?", (uid, iid)).fetchone()
                self.s.run("DELETE FROM favorites WHERE user_id=? AND item_id=?" if old else "INSERT INTO favorites VALUES(?,?)", (uid, iid))
                return self.card(uid, iid)
            cur = self.s.run("INSERT OR IGNORE INTO reports(user_id,item_id) VALUES(?,?)", (uid, iid))
            self.send(uid, "Спасибо! Сигнал сохранён для администратора." if cur.rowcount else "Ты уже сообщил об этом материале.")
            if cur.rowcount:
                for admin in self.admins:
                    try:
                        self.send(admin, f"⚠️ Жалоба на материал #{iid}: {html.escape(r['title'])}", [[self.button("Проверить", f"item:{iid}")]])
                    except APIError:
                        log.warning("Could not notify an administrator")
            return
        if action == "add":
            self.s.set_session(uid, {"stage": "category"})
            return self.draft_prompt(uid, self.s.session(uid))
        if action == "category":
            if self.s.session(uid).get("stage") != "category" or parts[1] not in CATEGORIES:
                return self.send(uid, "Начни добавление заново через /admin.")
            draft = self.s.session(uid)
            draft.update(stage="title", category=parts[1])
            self.s.set_session(uid, draft)
            return self.draft_prompt(uid, draft)
        if action == "draftback":
            draft = self.s.session(uid)
            previous = {"title": "category", "description": "title", "payload": "description", "preview": "payload"}
            stage = draft.get("stage")
            if stage != parts[1] or stage not in previous:
                return self.send(uid, "Эта кнопка устарела. Продолжи последний шаг или открой /admin.")
            draft["stage"] = previous[stage]
            self.s.set_session(uid, draft)
            return self.draft_prompt(uid, draft)
        if action == "keep":
            draft = self.s.session(uid)
            stage = parts[1]
            if stage not in {"title", "description"} or draft.get("stage") != stage or not draft.get(stage):
                return self.send(uid, "Эта кнопка устарела. Продолжи последний шаг.")
            return self.upload(uid, {"text": draft[stage]}, draft)
        if action == "publish":
            iid = self.s.publish(uid, parts[1])
            return self.card(uid, iid) if iid else self.send(uid, "Черновик уже опубликован или отменён.")
        if action == "stats":
            counts = [self.s.run(sql).fetchone()[0] for sql in (
                "SELECT count(*) FROM users", "SELECT count(*) FROM items WHERE active=1",
                "SELECT count(*) FROM deliveries", "SELECT count(*) FROM reports")]
            return self.send(uid, f"<b>📊 Статистика</b>\n\nПользователей: {counts[0]}\nМатериалов: {counts[1]}\n"
                             f"Успешных выдач файлов/ссылок: {counts[2]}\nСигналов «не работает»: {counts[3]}\n\n"
                             "Выдача ссылки не означает успешное подключение.", [[self.button("‹ Админ-панель", "admin")]])

    def draft_prompt(self, uid, draft):
        stage = draft["stage"]
        if stage == "category":
            return self.send(uid, "<b>1 / 5 · Раздел</b>\nКуда добавить материал?",
                [[self.button(v, "category:"+k)] for k, v in CATEGORIES.items()] + [[self.button("‹ Назад", "admin")]])
        prompts = {"title": "<b>2 / 5 · Название</b>\nНапиши короткое название (до 70 символов).",
                   "description": "<b>3 / 5 · Описание</b>\nУкажи платформу, страну, версию и инструкцию (до 700 символов).",
                   "payload": "<b>4 / 5 · Материал</b>\n" + (
                       "Пришли ссылку MTProxy или SOCKS5." if draft["category"] == "proxy" else
                       "Пришли адреса DNS и инструкцию текстом (до 1500 символов), HTTPS-ссылку или файл." if draft["category"] == "dns" else
                       "Пришли текст новости (до 1500 символов), HTTPS-ссылку или файл." if draft["category"] == "news" else
                       "Пришли документ: APK, ZIP, .conf и т. п. Или HTTPS-ссылку.")}
        text = prompts[stage]
        old = draft.get(stage)
        if old and stage in {"title", "description"}:
            text += "\n\nСохранено: " + html.escape(old) + "\nПришли новый текст или нажми «Оставить» в следующем сообщении."
        rows = [[self.button("‹ Назад", "draftback:"+stage)]]
        if old and stage in {"title", "description"}:
            # A callback uses the retained value through the same validation path.
            rows.insert(0, [self.button("Оставить сохранённое", "keep:"+stage)])
        self.send(uid, text + "\n/cancel — отменить.", rows)

    def upload(self, uid, m, draft):
        text = m.get("text", "").strip()
        stage = draft["stage"]
        if stage == "title":
            if not 1 <= len(text) <= 70:
                return self.send(uid, "Название должно содержать 1–70 символов.")
            draft.update(title=text, stage="description")
        elif stage == "description":
            if not 1 <= len(text) <= 700:
                return self.send(uid, "Описание должно содержать 1–700 символов.")
            draft.update(description=text, stage="payload")
        elif stage == "payload":
            doc = m.get("document")
            if doc and draft["category"] != "proxy":
                draft.update(kind="file", payload=doc["file_id"], filename=doc.get("file_name", "Файл"))
            elif draft["category"] in {"dns", "news"} and text and not re.match(r"(?i)https?://|tg://", text):
                if len(text) > 1500:
                    return self.send(uid, "Текст должен содержать до 1500 символов.")
                draft.update(kind="text", payload=text, filename="")
            else:
                try:
                    url = safe_url(text, proxy=draft["category"] == "proxy")
                except ValueError as exc:
                    return self.send(uid, html.escape(str(exc)))
                draft.update(kind="url", payload=url, filename="")
            draft.update(stage="preview", nonce=os.urandom(8).hex())
            self.s.set_session(uid, draft)
            return self.send(uid, "<b>5 / 5 · Предпросмотр</b>\n\n" + html.escape(draft["title"]) + "\n\n" +
                             html.escape(draft["description"]) + "\n\n" + html.escape(draft.get("filename") or draft["payload"]),
                             [[self.button("✅ Опубликовать бесплатно", "publish:"+draft["nonce"], style="success")],
                              [self.button("‹ Назад", "draftback:preview"), self.button("Отменить", "admin")]])
        else:
            return self.send(uid, "Используй кнопки в последнем шаге или /cancel.")
        self.s.set_session(uid, draft)
        self.draft_prompt(uid, draft)


def main():
    load_env()
    token = os.getenv("BOT_TOKEN", "")
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
        raise SystemExit("Заполни BOT_TOKEN в .env (токен от @BotFather).")
    try:
        admins = {int(v.strip()) for v in os.getenv("ADMIN_IDS", "").split(",") if v.strip()}
        icons = json.loads(os.getenv("CUSTOM_EMOJI_IDS", "{}"))
        if not admins or any(v <= 0 for v in admins) or not isinstance(icons, dict):
            raise ValueError
        if any(k not in CATEGORIES or not isinstance(v, str) or not v.isdigit() for k, v in icons.items()):
            raise ValueError
    except (ValueError, TypeError):
        raise SystemExit("Заполни ADMIN_IDS: числовые ID через запятую. CUSTOM_EMOJI_IDS: JSON с ID-строками.") from None
    support = os.getenv("SUPPORT_URL", "https://t.me/N777NN52")
    if support:
        support = safe_url(support)
    api = Telegram(token)
    s = Store(os.getenv("DB_PATH", "data/bot.sqlite3"))
    bot = Bot(api, s, admins, os.getenv("BOT_NAME", "BC-bot"), icons, support)
    if not 1 <= len(bot.name) <= 64:
        raise SystemExit("BOT_NAME должен содержать от 1 до 64 символов.")
    api.call("getMe")
    if api.call("getWebhookInfo").get("url"):
        raise SystemExit("У бота уже установлен webhook. Удали его перед запуском polling; см. README.")
    api.call("setMyName", name=bot.name)
    api.call("setMyCommands", commands=[{"command": "start", "description": "Открыть каталог"},
        {"command": "id", "description": "Мой Telegram ID"}, {"command": "cancel", "description": "Отмена"}])
    row = s.run("SELECT value FROM meta WHERE key='offset'").fetchone()
    offset = int(row[0]) if row else 0
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("Bot started")
    try:
        while running:
            try:
                updates = api.call("getUpdates", offset=offset, timeout=25, limit=50, allowed_updates=["message", "callback_query"])
            except APIError as exc:
                if exc.code in {401, 409}:
                    raise SystemExit("Проверь токен и убедись, что запущен только один экземпляр бота.") from None
                log.warning("Polling temporarily unavailable (code %s)", exc.code)
                time.sleep(min(max(exc.retry_after, 3), 30))
                continue
            for update in updates:
                try:
                    bot.update(update)
                except APIError as exc:
                    # Retry from the current offset; earlier outputs may be duplicated after a timeout.
                    if exc.code in {0, 429} or exc.code >= 500:
                        time.sleep(min(max(exc.retry_after, 2), 30))
                        break
                    log.warning("Update not completed (Telegram code %s); check content and emoji permissions", exc.code)
                except Exception as exc:
                    log.error("Update not completed (%s)", type(exc).__name__)
                offset = update["update_id"] + 1
                s.run("INSERT OR REPLACE INTO meta VALUES('offset',?)", (str(offset),))
    finally:
        s.db.close()
        log.info("Bot stopped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
