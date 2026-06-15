"""SQLite persistence layer — sole source of truth for sessions, orders, and steps."""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

_DB_PATH = Path("data/bot.db")
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """Return a thread-local connection with WAL mode and foreign keys enabled."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            excel_filename  TEXT NOT NULL,
            total_orders    INTEGER NOT NULL DEFAULT 0,
            success_count   INTEGER NOT NULL DEFAULT 0,
            failed_count    INTEGER NOT NULL DEFAULT 0,
            status          TEXT NOT NULL DEFAULT 'created',
            config_json     TEXT DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS orders (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            row_index       INTEGER NOT NULL,
            product_name    TEXT DEFAULT '',
            product_url     TEXT DEFAULT '',
            product_id      TEXT DEFAULT '',
            receiver_name   TEXT DEFAULT '',
            phone_number    TEXT DEFAULT '',
            address         TEXT DEFAULT '',
            quantity        INTEGER DEFAULT 1,
            status          TEXT DEFAULT 'pending',
            order_id        TEXT DEFAULT '',
            note            TEXT DEFAULT '',
            retry_count     INTEGER DEFAULT 0,
            failure_category TEXT DEFAULT '',
            debug_log_dir   TEXT DEFAULT '',
            started_at      TEXT,
            completed_at    TEXT,
            total_steps     INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS order_steps (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id        INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            step_number     INTEGER NOT NULL,
            timestamp       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            url             TEXT DEFAULT '',
            action          TEXT DEFAULT '',
            element_index   INTEGER DEFAULT 0,
            target_x        INTEGER DEFAULT 0,
            target_y        INTEGER DEFAULT 0,
            text_to_type    TEXT DEFAULT '',
            scroll_direction TEXT DEFAULT '',
            reasoning       TEXT DEFAULT '',
            observation     TEXT DEFAULT '',
            screenshot_filename  TEXT DEFAULT '',
            annotated_filename   TEXT DEFAULT ''
        );
    """)
    conn.commit()


# ── Sessions ─────────────────────────────────────────────────────

def create_session(excel_filename: str, total_orders: int = 0, config: dict | None = None) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO sessions (excel_filename, total_orders, config_json) VALUES (?, ?, ?)",
        (excel_filename, total_orders, json.dumps(config or {})),
    )
    conn.commit()
    return cur.lastrowid


def update_session(session_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values())
    vals.append(session_id)
    conn = _get_conn()
    conn.execute(f"UPDATE sessions SET {cols} WHERE id = ?", vals)
    conn.commit()


def list_sessions() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM sessions ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def get_session(session_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def delete_session(session_id: int) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()


# ── Orders ───────────────────────────────────────────────────────

def create_order(
    session_id: int,
    row_index: int,
    product_name: str = "",
    product_url: str = "",
    product_id: str = "",
    receiver_name: str = "",
    phone_number: str = "",
    address: str = "",
    quantity: int = 1,
    status: str = "pending",
    order_id: str = "",
    note: str = "",
) -> int:
    conn = _get_conn()
    cur = conn.execute(
        """INSERT INTO orders
           (session_id, row_index, product_name, product_url, product_id,
            receiver_name, phone_number, address, quantity, status, order_id, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, row_index, product_name, product_url, product_id,
         receiver_name, phone_number, address, quantity, status, order_id, note),
    )
    conn.commit()
    return cur.lastrowid


def update_order(order_db_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values())
    vals.append(order_db_id)
    conn = _get_conn()
    conn.execute(f"UPDATE orders SET {cols} WHERE id = ?", vals)
    conn.commit()


def get_orders_for_session(session_id: int) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM orders WHERE session_id = ? ORDER BY row_index", (session_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_order(order_db_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_db_id,)).fetchone()
    return dict(row) if row else None


# ── Steps ────────────────────────────────────────────────────────

def insert_step(
    order_db_id: int,
    step_number: int,
    url: str = "",
    action: str = "",
    element_index: int = 0,
    target_x: int = 0,
    target_y: int = 0,
    text_to_type: str = "",
    scroll_direction: str = "",
    reasoning: str = "",
    observation: str = "",
    screenshot_filename: str = "",
    annotated_filename: str = "",
) -> int:
    conn = _get_conn()
    cur = conn.execute(
        """INSERT INTO order_steps
           (order_id, step_number, url, action, element_index, target_x, target_y,
            text_to_type, scroll_direction, reasoning, observation,
            screenshot_filename, annotated_filename)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (order_db_id, step_number, url, action, element_index, target_x, target_y,
         text_to_type, scroll_direction, reasoning, observation,
         screenshot_filename, annotated_filename),
    )
    conn.commit()
    return cur.lastrowid


def get_steps_for_order(order_db_id: int) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM order_steps WHERE order_id = ? ORDER BY step_number",
        (order_db_id,),
    ).fetchall()
    return [dict(r) for r in rows]
