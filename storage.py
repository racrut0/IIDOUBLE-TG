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

from retrieval import dialogue_examples, reaction_signals, search_text, terms


REACTION_INDEX_VERSION = "1"


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
                CREATE INDEX IF NOT EXISTS questions_by_text ON memory_items(uid, json_extract(data,'$.normalized'))
                    WHERE category='generated_questions';
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
                CREATE TABLE IF NOT EXISTS api_spending (
                    id TEXT PRIMARY KEY, uid TEXT NOT NULL, created REAL NOT NULL, model TEXT NOT NULL,
                    reserved INTEGER NOT NULL, charged INTEGER NOT NULL, status TEXT NOT NULL,
                    input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER, cache_write_tokens INTEGER
                );
                CREATE INDEX IF NOT EXISTS spending_time ON api_spending(created,uid);
                CREATE TABLE IF NOT EXISTS pending_imports (
                    uid TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, generation INTEGER NOT NULL,
                    created REAL NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schema_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
        self._upgrade_reaction_index()

    def _upgrade_reaction_index(self):
        # Existing archives need the new situation labels too; never reimport or call a model.
        # Updating the FTS text and its version in one transaction makes restart safe.
        with self.transaction() as db:
            row = db.execute("SELECT value FROM schema_metadata WHERE key='reaction_index'").fetchone()
            if row and row[0] == REACTION_INDEX_VERSION:
                return
            rows = db.execute("SELECT seq,data FROM memory_items WHERE category='examples'").fetchall()
            db.executemany("UPDATE memory_items SET search_text=? WHERE seq=?",
                ((search_text("examples", json.loads(row["data"])), row["seq"]) for row in rows))
            db.execute("INSERT OR REPLACE INTO schema_metadata(key,value) VALUES('reaction_index',?)", (REACTION_INDEX_VERSION,))

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
        searchable = search_text(category, value)
        if searchable is None:
            searchable = text.lower() + " " + " ".join(sorted(terms(text)))
        db.execute("""INSERT INTO memory_items(uid,category,item_key,data,search_text) VALUES(?,?,?,?,?)
            ON CONFLICT(uid,category,item_key) DO UPDATE SET data=excluded.data,search_text=excluded.search_text
            WHERE memory_items.data != excluded.data OR memory_items.search_text != excluded.search_text""", (uid, category, item_key(value), text, searchable))

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
                AND category NOT IN ('history','imported_dialogues','examples','generated_questions') ORDER BY seq""", (uid,)).fetchall()
            for item in rows:
                data.setdefault(item["category"], []).append(json.loads(item["data"]))
            history = db.execute("SELECT data FROM memory_items WHERE uid=? AND category='history' ORDER BY seq DESC LIMIT 20", (uid,)).fetchall()
            data["history"] = [json.loads(item["data"]) for item in reversed(history)]
            questions = db.execute("SELECT data FROM memory_items WHERE uid=? AND category='generated_questions' ORDER BY seq DESC LIMIT 80", (uid,)).fetchall()
            data["generated_questions"] = [json.loads(item["data"]) for item in reversed(questions)]
            data["imported_dialogues"], data["examples"] = [], []
            return MemorySnapshot(data, uid, row["generation"], self)

    def save(self, uid, memory, import_token=""):
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
            if import_token:
                pending = db.execute("SELECT generation,created FROM pending_imports WHERE uid=? AND token=?", (uid, import_token)).fetchone()
                if not pending or pending["generation"] != memory.generation or pending["created"] < time.time() - 86400:
                    raise StaleMemoryError("Предпросмотр импорта устарел. Загрузи файл заново.")
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
            if import_token:
                db.execute("DELETE FROM pending_imports WHERE uid=? AND token=?", (uid, import_token))
        memory.baseline = copy.deepcopy(dict(memory))

    def reset(self, uid):
        uid = str(uid)
        with self.transaction() as db:
            self._ensure_user(db, uid)
            db.execute("UPDATE users SET generation=generation+1,data='{}' WHERE uid=?", (uid,))
            db.execute("DELETE FROM memory_items WHERE uid=?", (uid,))
            db.execute("DELETE FROM request_metrics WHERE uid=?", (uid,))
            db.execute("DELETE FROM turn_locks WHERE uid=?", (uid,))
            db.execute("DELETE FROM pending_imports WHERE uid=?", (uid,))

    def stage_import(self, uid, generation, token, data):
        with self.transaction() as db:
            row = db.execute("SELECT generation FROM users WHERE uid=?", (str(uid),)).fetchone()
            if not row or row[0] != generation:
                raise StaleMemoryError("Память была сброшена. Загрузи файл заново.")
            db.execute("DELETE FROM pending_imports WHERE created<?", (time.time() - 86400,))
            db.execute("INSERT OR REPLACE INTO pending_imports VALUES(?,?,?,?,?)", (str(uid), token, generation, time.time(), encode(data)))

    def pending_import(self, uid, token=""):
        with closing(self.connect()) as db:
            clause = " AND token=?" if token else ""
            args = (str(uid), token) if token else (str(uid),)
            row = db.execute("SELECT * FROM pending_imports WHERE uid=?" + clause, args).fetchone()
            if not row:
                return None
            if row["created"] < time.time() - 86400:
                self.discard_import(uid, row["token"])
                return None
            return {**dict(row), "data": json.loads(row["data"])}

    def update_import(self, uid, token, generation, data):
        with self.transaction() as db:
            result = db.execute("UPDATE pending_imports SET data=? WHERE uid=? AND token=? AND generation=? AND created>=?",
                (encode(data), str(uid), token, generation, time.time() - 86400))
            if result.rowcount != 1:
                raise StaleMemoryError("Предпросмотр импорта устарел. Загрузи файл заново.")

    def discard_import(self, uid, token):
        with self.transaction() as db:
            db.execute("DELETE FROM pending_imports WHERE uid=? AND token=?", (str(uid), token))

    def search_examples(self, uid, query, limit=60):
        query_terms = sorted(terms(query))[:30]
        situations = ["rx" + label for label in sorted(reaction_signals(query))]
        with closing(self.connect()) as db:
            rows = []
            # Separate pools prevent a common subject word from crowding out a
            # differently worded example of the right reaction.
            for group in (query_terms, situations):
                if not group:
                    continue
                match = " OR ".join('"' + word + '"*' for word in group)
                rows.extend(db.execute("""SELECT m.data FROM item_search JOIN memory_items m ON m.seq=item_search.rowid
                    WHERE item_search MATCH ? AND m.uid=? AND m.category='examples'
                    ORDER BY bm25(item_search),m.seq DESC LIMIT ?""", (match, str(uid), limit)).fetchall())
            approved = db.execute("""SELECT data FROM memory_items WHERE uid=? AND category IN
                ('qa','good_examples','correction_examples','reaction_examples') ORDER BY seq DESC LIMIT 60""", (str(uid),)).fetchall()
            return [json.loads(row["data"]) for row in [*rows, *approved]]

    def recent_examples(self, uid, limit=20):
        with closing(self.connect()) as db:
            return [json.loads(row[0]) for row in db.execute("""SELECT data FROM memory_items
                WHERE uid=? AND category='examples' ORDER BY seq DESC LIMIT ?""", (str(uid), limit))]

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

    def find_question(self, uid, question_id="", normalized=""):
        if not question_id and not normalized:
            return None
        clause, value = ("item_key=?", question_id) if question_id else ("json_extract(data,'$.normalized')=?", normalized)
        with closing(self.connect()) as db:
            row = db.execute("SELECT data FROM memory_items WHERE uid=? AND category='generated_questions' AND "
                + clause + " ORDER BY seq DESC LIMIT 1", (str(uid), value)).fetchone()
            return json.loads(row[0]) if row else None

    def delete_chat(self, uid, chat_id):
        with self.transaction() as db:
            db.execute("""DELETE FROM memory_items WHERE uid=? AND
                (category IN ('imported_dialogues','examples','import_facts','generated_questions','reaction_examples','owner_facts') AND json_extract(data,'$.chat_id')=?
                 OR category='qa' AND json_extract(data,'$.question_chat_id')=?
                 OR category='imported_chats' AND item_key=?)""", (str(uid), chat_id, chat_id, chat_id))
