"""Durable, append-only operational records; task writes stay in FF actuals."""
import json
import sqlite3
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone

def now():
    return datetime.now(timezone.utc).isoformat()

class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS plans (
              id INTEGER PRIMARY KEY, snapshot_id TEXT NOT NULL, created_at TEXT NOT NULL,
              parent_id INTEGER REFERENCES plans(id), payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS baselines (
              team TEXT NOT NULL, day INTEGER NOT NULL, shift INTEGER NOT NULL,
              plan_id INTEGER NOT NULL REFERENCES plans(id), actor TEXT NOT NULL,
              created_at TEXT NOT NULL, PRIMARY KEY(team, day, shift));
            CREATE TABLE IF NOT EXISTS audit (
              id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, actor TEXT NOT NULL,
              action TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS notes (
              id INTEGER PRIMARY KEY, team TEXT NOT NULL, day INTEGER NOT NULL,
              shift INTEGER NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL,
              body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (
              request_key TEXT PRIMARY KEY, actor TEXT NOT NULL, payload_hash TEXT NOT NULL,
              response TEXT NOT NULL, status INTEGER NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def audit(self, actor, action, payload):
        with self.connect() as db:
            return db.execute('INSERT INTO audit(created_at,actor,action,payload) VALUES(?,?,?,?)',
                              (now(), actor, action, json.dumps(payload, sort_keys=True))).lastrowid

    def setting(self, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, payload, actor):
        with self.connect() as db:
            encoded = json.dumps(payload, sort_keys=True)
            db.execute('INSERT INTO settings(key,payload) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload', (key, encoded))
            db.execute('INSERT INTO audit(created_at,actor,action,payload) VALUES(?,?,?,?)',
                       (now(), actor, 'settings.'+key, encoded))

    def save_plan(self, state):
        from ff.services import points
        payload = {'schedule': state['schedule'].to_dict(), 'cpm': state['cpm'],
                   'mock_data': state['mock_data'], 'states': {t.task_id:t.state for t in state['fleet'].tasks}}
        # Freeze original scorer values, retaining one scoring implementation.
        payload['points'] = {tid: points.score_task(tid, state['snapshot'])['total'] for tid in sorted(state['schedule'].assignments)}
        payload['fleet'] = state['fleet'].to_dict()
        with self.connect() as db:
            parent = db.execute('SELECT id FROM plans ORDER BY id DESC LIMIT 1').fetchone()
            return db.execute('INSERT INTO plans(snapshot_id,created_at,parent_id,payload) VALUES(?,?,?,?)',
                              (state['snapshot_id'],now(),parent[0] if parent else None,zlib.compress(json.dumps(payload, sort_keys=True).encode()))).lastrowid

    def plan(self, plan_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM plans WHERE id=?', (plan_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result['payload'] = json.loads(zlib.decompress(result['payload']))
        return result

    def latest(self):
        with self.connect() as db:
            row = db.execute('SELECT id FROM plans ORDER BY id DESC LIMIT 1').fetchone()
        return self.plan(row[0]) if row else None
