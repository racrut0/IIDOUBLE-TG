"""SQLite storage with append-only archives and delta writes for legacy UI flows."""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from retrieval import dialogue_examples, terms


class StaleMemoryError(RuntimeError):
    pass


class BusyUserError(RuntimeError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def item_key(value):
    if isinstance(value, dict) and value.get("id"):
        return str(value["id"])
    return hashlib.sha256(encode(value).encode()).hexdigest()


class MemorySnapshot(dict):
    def __init__(self, data, uid, generation, store):
        super().__init__(data)
        self.baseline = copy.deepcopy(data)
        self.uid, self.generation, self.store = str(uid), generation, store


def merge_dict(latest, before, changed):
    result = copy.deepcopy(latest)
    for key in before.keys() - changed.keys():
        result.pop(key, None)
    for key, value in changed.items():
        if key in before and value == before[key]:
            continue
        if key == "last_exchange":
            result[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(before.get(key), dict):
            result[key] = merge_dict(result.get(key, {}), before[key], value)
        elif key == "message_count":
            result[key] = result.get(key, 0) + value - before.get(key, 0)
        else:
            result[key] = copy.deepcopy(value)
    return result


class MemoryStore:
    def __init__(self, path, legacy_dir=None):
        self.path = Path(path).resolve()
        self.legacy_dir = Path(legacy_dir).resolve() if legacy_dir else self.path.parent
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    uid TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_items (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL,
                    category TEXT NOT NULL, item_key TEXT NOT NULL,
                    data TEXT NOT NULL, search_text TEXT NOT NULL,
                    UNIQUE(uid, category, item_key)
                );
                CREATE INDEX IF NOT EXISTS items_owner ON memory_items(uid, category, seq);
                CREATE VIRTUAL TABLE IF NOT EXISTS item_search USING fts5(
                    search_text, content='memory_items', content_rowid='seq', tokenize='unicode61'
                );
                CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON memory_items BEGIN
                    INSERT INTO item_search(rowid, search_text) VALUES(new.seq, new.search_text);
                END;
                CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON memory_items BEGIN
                    INSERT INTO item_search(item_search, rowid, search_text) VALUES('delete', old.seq, old.search_text);
                END;
                CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON memory_items BEGIN
                    INSERT INTO item_search(item_search, rowid, search_text) VALUES('delete', old.seq, old.search_text);
                    INSERT INTO item_search(rowid, search_text) VALUES(new.seq, new.search_text);
                END;
                CREATE TABLE IF NOT EXISTS turn_locks(uid TEXT PRIMARY KEY, owner TEXT, expires REAL);
                CREATE TABLE IF NOT EXISTS request_metrics (
                    id INTEGER PRIMARY KEY, uid TEXT NOT NULL, created REAL NOT NULL, data TEXT NOT NULL
                );
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _put(self, db, uid, category, value):
        text = encode(value)
        searchable = text.lower() + " " + " ".join(sorted(terms(text)))
        db.execute("""INSERT INTO memory_items(uid,category,item_key,data,search_text) VALUES(?,?,?,?,?)
            ON CONFLICT(uid,category,item_key) DO UPDATE SET data=excluded.data,search_text=excluded.search_text
            WHERE memory_items.data != excluded.data""", (uid, category, item_key(value), text, searchable))

    def _ensure_user(self, db, uid):
        if db.execute("SELECT 1 FROM users WHERE uid=?", (uid,)).fetchone():
            return
        legacy = self.legacy_dir / f"{uid}.json"
        data = {}
        if legacy.is_file():
            # Migration fails visibly on corruption; never overwrite the original file.
            data = json.loads(legacy.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("Файл старой памяти должен содержать объект JSON")
        scalar = {k: v for k, v in data.items() if not isinstance(v, list)}
        db.execute("INSERT INTO users(uid,data) VALUES(?,?)", (uid, encode(scalar)))
        for category, values in data.items():
            if isinstance(values, list):
                for index, value in enumerate(values):
                    if category == "history" and isinstance(value, dict):
                        value = {**value, "id": f"legacy-{index}"}
                    self._put(db, uid, category, value)
        if data.get("imported_dialogues") and not data.get("examples"):
            for example in dialogue_examples(data["imported_dialogues"]):
                self._put(db, uid, "examples", example)

    def load(self, uid):
        uid = str(uid)
        if not uid.isdecimal():
            raise ValueError("Некорректный Telegram user id")
        with self.transaction() as db:
            self._ensure_user(db, uid)
            row = db.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()
            data = json.loads(row["data"])
            # Large archives stay in SQLite; retrieval reads just matching examples.
            rows = db.execute("""SELECT category,data FROM memory_items WHERE uid=?
                AND category NOT IN ('history','imported_dialogues','examples') ORDER BY seq""", (uid,)).fetchall()
            for item in rows:
                data.setdefault(item["category"], []).append(json.loads(item["data"]))
            history = db.execute("SELECT data FROM memory_items WHERE uid=? AND category='history' ORDER BY seq DESC LIMIT 20", (uid,)).fetchall()
            data["history"] = [json.loads(item["data"]) for item in reversed(history)]
            data["imported_dialogues"], data["examples"] = [], []
            return MemorySnapshot(data, uid, row["generation"], self)

    def save(self, uid, memory):
        uid = str(uid)
        if not isinstance(memory, MemorySnapshot):
            snapshot = self.load(uid)
            snapshot.update(memory)
            memory = snapshot
        if memory.uid != uid:
            raise ValueError("Память принадлежит другому пользователю")
        before = memory.baseline
        with self.transaction() as db:
            row = db.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()
            if not row or row["generation"] != memory.generation:
                raise StaleMemoryError("Память была сброшена. Повтори действие.")
            scalar_before = {k: v for k, v in before.items() if not isinstance(v, list)}
            scalar_after = {k: v for k, v in memory.items() if not isinstance(v, list)}
            latest = merge_dict(json.loads(row["data"]), scalar_before, scalar_after)
            db.execute("UPDATE users SET data=? WHERE uid=?", (encode(latest), uid))
            current_examples = {item_key(item) for item in memory.get("examples", [])}
            for record in memory.get("imported_dialogues", []):
                if isinstance(record, dict) and record.get("id") and record.get("role") != "me" and record["id"] not in current_examples:
                    db.execute("DELETE FROM memory_items WHERE uid=? AND category='examples' AND item_key=?", (uid, record["id"]))
            for category, values in memory.items():
                if not isinstance(values, list):
                    continue
                old = {item_key(v): v for v in before.get(category, [])}
                new = {item_key(v): v for v in values}
                for key in old.keys() - new.keys():
                    db.execute("DELETE FROM memory_items WHERE uid=? AND category=? AND item_key=?", (uid, category, key))
                for key, value in new.items():
                    if key not in old or value != old[key]:
                        self._put(db, uid, category, value)
        memory.baseline = copy.deepcopy(dict(memory))

    def reset(self, uid):
        uid = str(uid)
        with self.transaction() as db:
            self._ensure_user(db, uid)
            db.execute("UPDATE users SET generation=generation+1,data='{}' WHERE uid=?", (uid,))
            db.execute("DELETE FROM memory_items WHERE uid=?", (uid,))
            db.execute("DELETE FROM request_metrics WHERE uid=?", (uid,))
            db.execute("DELETE FROM turn_locks WHERE uid=?", (uid,))

    def search_examples(self, uid, query, limit=60):
        query_terms = sorted(terms(query))[:30]
        with closing(self.connect()) as db:
            rows = []
            if query_terms:
                match = " OR ".join('"' + word + '"*' for word in query_terms)
                rows = db.execute("""SELECT m.data FROM item_search JOIN memory_items m ON m.seq=item_search.rowid
                    WHERE item_search MATCH ? AND m.uid=? AND m.category='examples'
                    ORDER BY bm25(item_search) LIMIT ?""", (match, str(uid), limit)).fetchall()
            approved = db.execute("""SELECT data FROM memory_items WHERE uid=? AND category IN
                ('qa','good_examples','correction_examples') ORDER BY seq DESC LIMIT 60""", (str(uid),)).fetchall()
            return [json.loads(row["data"]) for row in [*rows, *approved]]

    def count(self, uid, category):
        with closing(self.connect()) as db:
            return db.execute("SELECT count(*) FROM memory_items WHERE uid=? AND category=?", (str(uid), category)).fetchone()[0]

    @contextmanager
    def turn(self, uid):
        owner = uuid.uuid4().hex
        with self.transaction() as db:
            row = db.execute("SELECT expires FROM turn_locks WHERE uid=?", (str(uid),)).fetchone()
            if row and row[0] > time.time():
                raise BusyUserError("Предыдущий ответ ещё готовится. Дождись его завершения.")
            db.execute("INSERT OR REPLACE INTO turn_locks VALUES(?,?,?)", (str(uid), owner, time.time() + 300))
        try:
            yield
        finally:
            with self.transaction() as db:
                db.execute("DELETE FROM turn_locks WHERE uid=? AND owner=?", (str(uid), owner))

    def metric(self, uid, data):
        with self.transaction() as db:
            db.execute("INSERT INTO request_metrics(uid,created,data) VALUES(?,?,?)", (str(uid), time.time(), encode(data)))

    def metrics(self, uid, limit=20):
        with closing(self.connect()) as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM request_metrics WHERE uid=? ORDER BY id DESC LIMIT ?", (str(uid), limit))]

    def owner_samples(self, uid, limit=1000):
        with closing(self.connect()) as db:
            return [json.loads(row[0]) for row in db.execute("""SELECT data FROM memory_items
                WHERE uid=? AND category='imported_dialogues' AND json_extract(data,'$.role')='me'
                ORDER BY seq DESC LIMIT ?""", (str(uid), limit))]

    def delete_chat(self, uid, chat_id):
        with self.transaction() as db:
            db.execute("""DELETE FROM memory_items WHERE uid=? AND
                (category IN ('imported_dialogues','examples','import_facts') AND json_extract(data,'$.chat_id')=?
                 OR category='imported_chats' AND item_key=?)""", (str(uid), chat_id, chat_id))
