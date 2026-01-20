import sqlite3
from typing import Optional
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Task:
    id: int
    chat_id: int
    seq_num: int
    task_id: str
    url: str
    assigned_to: str
    created_by: str
    created_at: datetime


class Database:
    def __init__(self, db_path: str = "workqueue.db"):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    seq_num INTEGER NOT NULL,
                    task_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    assigned_to TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, task_id),
                    UNIQUE(chat_id, seq_num)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS seq_counters (
                    chat_id INTEGER PRIMARY KEY,
                    next_num INTEGER DEFAULT 1
                )
            """)
            conn.commit()

    def _get_next_seq_num(self, conn: sqlite3.Connection, chat_id: int) -> int:
        cursor = conn.execute(
            "SELECT next_num FROM seq_counters WHERE chat_id = ?",
            (chat_id,)
        )
        row = cursor.fetchone()
        
        if row is None:
            conn.execute(
                "INSERT INTO seq_counters (chat_id, next_num) VALUES (?, 2)",
                (chat_id,)
            )
            return 1
        else:
            next_num = row["next_num"]
            conn.execute(
                "UPDATE seq_counters SET next_num = ? WHERE chat_id = ?",
                (next_num + 1, chat_id)
            )
            return next_num

    def add_task(self, chat_id: int, task_id: str, url: str, assigned_to: str, created_by: str) -> Optional[int]:
        """Add a task. Returns sequence number if added, None if already exists."""
        try:
            with self._get_connection() as conn:
                seq_num = self._get_next_seq_num(conn, chat_id)
                conn.execute(
                    """
                    INSERT INTO tasks (chat_id, seq_num, task_id, url, assigned_to, created_by)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (chat_id, seq_num, task_id, url, assigned_to, created_by)
                )
                conn.commit()
                return seq_num
        except sqlite3.IntegrityError:
            return None

    def get_tasks(self, chat_id: int) -> list[Task]:
        """Get all tasks for a chat, ordered by sequence number."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, chat_id, seq_num, task_id, url, assigned_to, created_by, created_at
                FROM tasks
                WHERE chat_id = ?
                ORDER BY seq_num ASC
                """,
                (chat_id,)
            )
            return [
                Task(
                    id=row["id"],
                    chat_id=row["chat_id"],
                    seq_num=row["seq_num"],
                    task_id=row["task_id"],
                    url=row["url"],
                    assigned_to=row["assigned_to"],
                    created_by=row["created_by"],
                    created_at=row["created_at"]
                )
                for row in cursor.fetchall()
            ]

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            chat_id=row["chat_id"],
            seq_num=row["seq_num"],
            task_id=row["task_id"],
            url=row["url"],
            assigned_to=row["assigned_to"],
            created_by=row["created_by"],
            created_at=row["created_at"]
        )

    def remove_task_by_id(self, chat_id: int, task_id: str) -> Optional[Task]:
        """Remove a task by its task_id and return the removed task, or None if not found."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, chat_id, seq_num, task_id, url, assigned_to, created_by, created_at
                FROM tasks
                WHERE chat_id = ? AND task_id = ?
                """,
                (chat_id, task_id)
            )
            row = cursor.fetchone()
            
            if row is None:
                return None
            
            task = self._row_to_task(row)
            conn.execute(
                "DELETE FROM tasks WHERE chat_id = ? AND task_id = ?",
                (chat_id, task_id)
            )
            conn.commit()
            return task

    def remove_task_by_seq(self, chat_id: int, seq_num: int) -> Optional[Task]:
        """Remove a task by its sequence number and return the removed task, or None if not found."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, chat_id, seq_num, task_id, url, assigned_to, created_by, created_at
                FROM tasks
                WHERE chat_id = ? AND seq_num = ?
                """,
                (chat_id, seq_num)
            )
            row = cursor.fetchone()
            
            if row is None:
                return None
            
            task = self._row_to_task(row)
            conn.execute(
                "DELETE FROM tasks WHERE chat_id = ? AND seq_num = ?",
                (chat_id, seq_num)
            )
            conn.commit()
            return task
