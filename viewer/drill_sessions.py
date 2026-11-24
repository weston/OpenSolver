import json
import os
import sqlite3
import time


class Sessions:
    def __init__(self, path):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, started REAL NOT NULL, ended REAL,
                    settings TEXT NOT NULL, hands INTEGER NOT NULL DEFAULT 0,
                    decisions INTEGER NOT NULL DEFAULT 0, loss REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY, session TEXT NOT NULL, hand INTEGER NOT NULL,
                    payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS reviews_session ON reviews(session, id);
            ''')

    def start(self, session, settings):
        with self.db:
            self.db.execute('INSERT INTO sessions(id, started, settings) VALUES (?, ?, ?)',
                            (session, time.time(), json.dumps(settings)))

    def record(self, session, number, hand, stats):
        with self.db:
            self.db.execute('INSERT INTO reviews(session, hand, payload) VALUES (?, ?, ?)',
                            (session, number, json.dumps(hand)))
            self.update(session, stats)

    def update(self, session, stats):
        self.db.execute('UPDATE sessions SET hands=?, decisions=?, loss=? WHERE id=?', (*stats, session))

    def finish_hand(self, session, stats):
        with self.db:
            self.update(session, stats)

    def end(self, session):
        with self.db:
            self.db.execute('UPDATE sessions SET ended=? WHERE id=? AND ended IS NULL', (time.time(), session))

    def list(self, offset):
        rows = self.db.execute('SELECT * FROM sessions ORDER BY started DESC LIMIT 50 OFFSET ?', (offset,))
        return [{**dict(row), 'settings': json.loads(row['settings'])} for row in rows]

    def reviews(self, session, offset):
        rows = self.db.execute('SELECT hand, payload FROM reviews WHERE session=? ORDER BY id LIMIT 50 OFFSET ?',
                               (session, offset))
        return [{'number': row['hand'], **json.loads(row['payload'])} for row in rows]
