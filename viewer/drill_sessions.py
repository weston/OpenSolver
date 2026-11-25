import json
import os
import sqlite3
import time
import tempfile
from drill_grading import regrade_feedback


class Sessions:
    def __init__(self, path):
        self.temp = tempfile.TemporaryDirectory(prefix="wsolver-sessions-") if path == ":memory:" else None
        self.assets = self.temp.name if self.temp else path + ".d"
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
                CREATE TABLE IF NOT EXISTS choices (
                    session TEXT NOT NULL, simulation TEXT NOT NULL, seat TEXT NOT NULL,
                    node TEXT NOT NULL, combo TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(session, simulation, seat, node, combo));
            ''')
            changed = set()
            for row in self.db.execute('SELECT id, session, payload FROM reviews'):
                hand = json.loads(row['payload'])
                if all('grade' in h for h in hand['feedback']['hands']):
                    continue
                regrade_feedback(hand['feedback'])
                self.db.execute('UPDATE reviews SET payload=? WHERE id=?', (json.dumps(hand), row['id']))
                changed.add(row['session'])
            for session in changed:
                loss = sum(h['loss_pct'] for row in self.db.execute('SELECT payload FROM reviews WHERE session=?', (session,))
                           for h in json.loads(row['payload'])['feedback']['hands'])
                self.db.execute('UPDATE sessions SET loss=? WHERE id=?', (loss, session))

    def start(self, session, settings):
        with self.db:
            self.db.execute('INSERT INTO sessions(id, started, settings) VALUES (?, ?, ?)',
                            (session, time.time(), json.dumps(settings)))

    def snapshot_path(self, session):
        os.makedirs(self.assets, exist_ok=True)
        return os.path.join(self.assets, session + ".cfr")

    def get(self, session):
        row = self.db.execute('SELECT *, MAX(hands, COALESCE((SELECT MAX(hand) FROM reviews WHERE session=sessions.id), 0)) AS number FROM sessions WHERE id=?', (session,)).fetchone()
        return {**dict(row), 'settings': json.loads(row['settings'])} if row else None

    def resume(self, session):
        with self.db:
            self.db.execute('UPDATE sessions SET ended=NULL WHERE id=?', (session,))

    def record(self, session, number, hand, stats):
        with self.db:
            self.db.execute('INSERT INTO reviews(session, hand, payload) VALUES (?, ?, ?)',
                            (session, number, json.dumps(hand)))
            self.update(session, stats)
            if hand.get('simulation'):
                for result in hand['feedback']['hands']:
                    mix = {a['id']: f for a, f in zip(result['actions'], result['chosen'])}
                    self.db.execute('INSERT OR REPLACE INTO choices VALUES (?, ?, ?, ?, ?, ?)',
                                    (session, hand['simulation'], hand['seat'], result['node'], result['combo'], json.dumps(mix)))

    def update(self, session, stats):
        self.db.execute('UPDATE sessions SET hands=?, decisions=?, loss=? WHERE id=?', (*stats, session))

    def finish_hand(self, session, stats):
        with self.db:
            self.update(session, stats)

    def end(self, session):
        with self.db:
            self.db.execute('UPDATE sessions SET ended=? WHERE id=? AND ended IS NULL', (time.time(), session))
        self.remove_empty()

    def remove_empty(self, active=None):
        sessions = [row['id'] for row in self.db.execute('SELECT id FROM sessions WHERE hands=0 AND id!=?', (active or '',))]
        with self.db:
            for session in sessions:
                self.db.execute('DELETE FROM reviews WHERE session=?', (session,))
                self.db.execute('DELETE FROM choices WHERE session=?', (session,))
                self.db.execute('DELETE FROM sessions WHERE id=?', (session,))
        for session in sessions:
            path = os.path.join(self.assets, session + '.cfr')
            if os.path.isfile(path):
                os.remove(path)

    def list(self, offset, active=None):
        self.remove_empty(active)
        rows = self.db.execute('SELECT * FROM sessions WHERE hands>0 ORDER BY started DESC LIMIT 50 OFFSET ?', (offset,))
        return [{**dict(row), 'settings': json.loads(row['settings'])} for row in rows]

    def reviews(self, session, offset):
        rows = self.db.execute('SELECT hand, payload FROM reviews WHERE session=? ORDER BY id LIMIT 50 OFFSET ?',
                               (session, offset))
        return [{'number': row['hand'], **json.loads(row['payload'])} for row in rows]

    def simulations(self, session, offset):
        rows = self.db.execute('''SELECT simulation,
            SUM(seat='OOP') AS oop, SUM(seat='IP') AS ip FROM choices WHERE session=?
            GROUP BY simulation ORDER BY simulation LIMIT 50 OFFSET ?''', (session, offset))
        return [dict(row) for row in rows]

    def choices(self, session, simulation, seat):
        rows = self.db.execute('SELECT node, combo, payload FROM choices WHERE session=? AND simulation=? AND seat=? ORDER BY node, combo',
                               (session, simulation, seat))
        return [{'node': row['node'], 'combo': row['combo'], 'mix': json.loads(row['payload'])} for row in rows]
