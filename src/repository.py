from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES, should_escalate_recurrence

ITEM_EXTRA_COLUMNS = {
    "dam_section": "TEXT NOT NULL DEFAULT ''",
    "measure_point": "TEXT NOT NULL DEFAULT ''",
    "reading": "REAL NOT NULL DEFAULT 0",
    "previous_item_id": "INTEGER",
    "recurrence_count": "INTEGER NOT NULL DEFAULT 0",
    "report_count": "INTEGER NOT NULL DEFAULT 0",
    "escalation_pending": "INTEGER NOT NULL DEFAULT 0",
    "conclusion": "TEXT",
    "last_conclusion": "TEXT",
}


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    dam_section TEXT NOT NULL DEFAULT '',
                    measure_point TEXT NOT NULL DEFAULT '',
                    reading REAL NOT NULL DEFAULT 0,
                    previous_item_id INTEGER,
                    recurrence_count INTEGER NOT NULL DEFAULT 0,
                    report_count INTEGER NOT NULL DEFAULT 0,
                    escalation_pending INTEGER NOT NULL DEFAULT 0,
                    conclusion TEXT,
                    last_conclusion TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)
        self._migrate_items()

    def _migrate_items(self) -> None:
        with self._lock:
            columns = {row["name"] for row in
                       self.conn.execute("PRAGMA table_info(items)").fetchall()}
            missing = {name: ddl for name, ddl in ITEM_EXTRA_COLUMNS.items()
                       if name not in columns}
        if not missing:
            return
        with self._lock, self.conn:
            for name, ddl in missing.items():
                self.conn.execute(f"ALTER TABLE items ADD COLUMN {name} {ddl}")

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    dam_section: str, measure_point: str, reading: float,
                    actor: str) -> tuple:
        now = utc_now()
        with self._lock, self.conn:
            open_row = self.conn.execute(
                """SELECT * FROM items WHERE measure_point=? AND status<>'closed'
                   ORDER BY id LIMIT 1""",
                (measure_point,),
            ).fetchone()
            if open_row is not None:
                escalated = should_escalate_recurrence(
                    int(open_row["recurrence_count"]), reading, float(open_row["reading"]))
                self.conn.execute(
                    """UPDATE items SET report_count=report_count+1, reading=?,
                       severity=?, escalation_pending=?, version=version+1, updated_at=?
                       WHERE id=?""",
                    (reading,
                     "emergency" if escalated else open_row["severity"],
                     1 if (escalated or open_row["escalation_pending"]) else 0,
                     now, int(open_row["id"])),
                )
                item = self.get_item(int(open_row["id"]))
                return item, {"merged": True, "escalated": escalated}
            prev = self.conn.execute(
                """SELECT * FROM items WHERE measure_point=? AND status='closed'
                   ORDER BY id DESC LIMIT 1""",
                (measure_point,),
            ).fetchone()
            recurrence = int(prev["recurrence_count"]) + 1 if prev else 0
            escalated = should_escalate_recurrence(
                recurrence, reading, float(prev["reading"]) if prev else 0.0)
            try:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at,
                       dam_section, measure_point, reading, previous_item_id,
                       recurrence_count, report_count, escalation_pending, last_conclusion)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, "emergency" if escalated else severity, quantity,
                     threshold, STATES[0], 1, external_ref, actor, now, now, dam_section,
                     measure_point, reading,
                     int(prev["id"]) if prev else None, recurrence, 0,
                     1 if escalated else 0,
                     prev["conclusion"] if prev else None),
                )
                item_id = int(cur.lastrowid)
            except sqlite3.IntegrityError as exc:
                raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id), {"merged": False, "escalated": escalated}

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str, conclusion: Optional[str] = None) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?,
                   conclusion=COALESCE(?, conclusion)
                   WHERE id=? AND version=?""",
                (target, now, conclusion, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def confirm_escalation(self, item_id: int, expected_version: int,
                           actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET escalation_pending=0, version=version+1, updated_at=?
                   WHERE id=? AND version=? AND escalation_pending=1""",
                (now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                row = self.conn.execute(
                    "SELECT escalation_pending FROM items WHERE id=?", (item_id,)).fetchone()
                if row is None:
                    raise NotFoundError("项目不存在")
                if not row["escalation_pending"]:
                    raise ConflictError("无待确认的应急升级")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
