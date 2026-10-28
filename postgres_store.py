"""Persistent backend for webhook hosting; never fall back to ephemeral SQLite."""
import json
from bot import Store


class Row(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def row_factory(cursor):
    names = [col.name for col in cursor.description] if cursor.description else []
    return lambda values: Row(zip(names, values))


class PostgresStore(Store):
    def __init__(self, url):
        import psycopg
        self.db = psycopg.connect(url, autocommit=True, row_factory=row_factory,
                                 connect_timeout=10, options="-c statement_timeout=15000")
        statements = [
            "CREATE TABLE IF NOT EXISTS users(id BIGINT PRIMARY KEY, created TEXT DEFAULT CURRENT_TIMESTAMP::text)",
            "CREATE TABLE IF NOT EXISTS items(id BIGSERIAL PRIMARY KEY, category TEXT NOT NULL, title TEXT NOT NULL, "
            "description TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, filename TEXT NOT NULL DEFAULT '', "
            "active INTEGER NOT NULL DEFAULT 1, created TEXT DEFAULT CURRENT_TIMESTAMP::text)",
            "CREATE TABLE IF NOT EXISTS favorites(user_id BIGINT, item_id BIGINT REFERENCES items(id), PRIMARY KEY(user_id,item_id))",
            "CREATE TABLE IF NOT EXISTS sessions(user_id BIGINT PRIMARY KEY, data TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS deliveries(id BIGSERIAL PRIMARY KEY, user_id BIGINT, item_id BIGINT, created TEXT DEFAULT CURRENT_TIMESTAMP::text)",
            "CREATE TABLE IF NOT EXISTS reports(user_id BIGINT, item_id BIGINT, created TEXT DEFAULT CURRENT_TIMESTAMP::text, PRIMARY KEY(user_id,item_id))",
            "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS processed_updates(id BIGINT PRIMARY KEY, created TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)",
        ]
        with self.db.transaction():
            for sql in statements:
                self.db.execute(sql)

    @staticmethod
    def translate(sql):
        if sql.startswith("INSERT OR IGNORE INTO "):
            sql = sql.replace("INSERT OR IGNORE INTO ", "INSERT INTO ", 1) + " ON CONFLICT DO NOTHING"
        elif sql.startswith("INSERT OR REPLACE INTO sessions "):
            sql = sql.replace("INSERT OR REPLACE INTO", "INSERT INTO", 1)
            sql += " ON CONFLICT (user_id) DO UPDATE SET data=EXCLUDED.data"
        elif sql.startswith("INSERT OR REPLACE INTO meta "):
            sql = sql.replace("INSERT OR REPLACE INTO", "INSERT INTO", 1)
            sql += " ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value"
        return sql.replace("?", "%s")

    def run(self, sql, args=()):
        return self.db.execute(self.translate(sql), args)

    def publish(self, uid, nonce):
        with self.db.transaction():
            row = self.db.execute("SELECT data FROM sessions WHERE user_id=%s FOR UPDATE", (uid,)).fetchone()
            draft = json.loads(row[0]) if row else {}
            if draft.get("stage") != "preview" or draft.get("nonce") != nonce:
                return None
            row = self.db.execute(
                "INSERT INTO items(category,title,description,kind,payload,filename) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
                tuple(draft.get(k, "") for k in ("category", "title", "description", "kind", "payload", "filename"))).fetchone()
            self.db.execute("DELETE FROM sessions WHERE user_id=%s", (uid,))
            return row[0]

    def processed(self, update_id):
        return bool(self.run("SELECT 1 FROM processed_updates WHERE id=?", (update_id,)).fetchone())

    def mark_processed(self, update_id):
        self.run("INSERT OR IGNORE INTO processed_updates(id) VALUES(?)", (update_id,))
        self.run("DELETE FROM processed_updates WHERE created < CURRENT_TIMESTAMP - INTERVAL '7 days'")
