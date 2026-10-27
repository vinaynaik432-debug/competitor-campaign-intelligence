"""Local persistence. Credentials never enter this store."""
import json
import math
import sqlite3
from contextlib import contextmanager
from pathlib import Path

CAP_USD = 5.0


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS snapshots (
                    id TEXT PRIMARY KEY, collected_at TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS feedback (
                    snapshot_id TEXT NOT NULL, event_id TEXT NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY(snapshot_id, event_id));
                CREATE TABLE IF NOT EXISTS reservations (
                    id TEXT PRIMARY KEY, cap REAL NOT NULL, reported REAL NOT NULL DEFAULT 0,
                    run_id TEXT, status TEXT NOT NULL DEFAULT 'start_pending');
            ''')
            columns = {r[1] for r in db.execute('PRAGMA table_info(reservations)')}
            if 'cost_known' not in columns:
                db.execute('ALTER TABLE reservations ADD COLUMN cost_known INTEGER NOT NULL DEFAULT 0')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save_snapshot(self, snapshot):
        with self.connect() as db:
            db.execute('INSERT INTO snapshots VALUES (?, ?, ?)',
                       (snapshot['id'], snapshot['collected_at'], json.dumps(snapshot, ensure_ascii=False)))

    def snapshots(self):
        with self.connect() as db:
            return [json.loads(r['body']) for r in db.execute('SELECT body FROM snapshots ORDER BY collected_at, id')]

    def budget(self):
        with self.connect() as db:
            rows = list(db.execute('SELECT cap, reported, cost_known FROM reservations'))
        allocated = sum(max(r['cap'], r['reported']) for r in rows)
        spent = sum(r['reported'] for r in rows)
        return {'cap_usd': CAP_USD, 'spent_usd': round(spent, 6),
                'reserved_usd': round(max(0, allocated - spent), 6),
                'remaining_usd': round(max(0, CAP_USD - allocated), 6),
                'unknown_cost_runs': sum(not r['cost_known'] for r in rows)}

    def reserve(self, reservation_id, amount):
        if isinstance(amount, bool) or not math.isfinite(amount) or not 0 < amount <= CAP_USD:
            raise ValueError('Invalid run allowance.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            used = db.execute('SELECT COALESCE(SUM(MAX(cap, reported)), 0) FROM reservations').fetchone()[0]
            if used + amount > CAP_USD + 1e-9:
                raise ValueError('The US$5 project allowance is fully allocated. Saved data remains available.')
            db.execute('INSERT INTO reservations(id, cap) VALUES (?, ?)', (reservation_id, amount))

    def run_status(self, reservation_id, run_id=None, status='unknown', reported=None):
        with self.connect() as db:
            db.execute('UPDATE reservations SET run_id=COALESCE(?,run_id), status=? WHERE id=?',
                       (run_id, status, reservation_id))
            if isinstance(reported, (int, float)) and not isinstance(reported, bool) and math.isfinite(reported) and reported >= 0:
                db.execute('UPDATE reservations SET reported=?, cost_known=1 WHERE id=?', (reported, reservation_id))

    def feedback(self, snapshot_id):
        with self.connect() as db:
            return {r['event_id']: r['value'] for r in db.execute(
                'SELECT event_id,value FROM feedback WHERE snapshot_id=?', (snapshot_id,))}

    def set_feedback(self, snapshot_id, event_id, value):
        if value not in {'useful', 'not_useful', 'acted_on', ''}:
            raise ValueError('Choose useful, not useful, or acted on.')
        with self.connect() as db:
            if value:
                db.execute('INSERT OR REPLACE INTO feedback VALUES (?, ?, ?)', (snapshot_id, event_id, value))
            else:
                db.execute('DELETE FROM feedback WHERE snapshot_id=? AND event_id=?', (snapshot_id, event_id))
