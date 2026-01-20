import sqlite3
from typing import Optional
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Task:
    id: int
    chat_id: int
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
                    task_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    assigned_to TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, task_id)
                )
            """)
            conn.commit()

    def add_task(self, chat_id: int, task_id: str, url: str, assigned_to: str, created_by: str) -> bool:
        """Add a task. Returns True if added, False if already exists."""
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO tasks (chat_id, task_id, url, assigned_to, created_by)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (chat_id, task_id, url, assigned_to, created_by)
                )
                conn.commit()
                return True
        except sqlite3.IntegrityError:
            return False

    def get_tasks(self, chat_id: int) -> list[Task]:
        """Get all tasks for a chat, ordered by creation time."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, chat_id, task_id, url, assigned_to, created_by, created_at
                FROM tasks
                WHERE chat_id = ?
                ORDER BY created_at ASC
                """,
                (chat_id,)
            )
            return [
                Task(
                    id=row["id"],
                    chat_id=row["chat_id"],
                    task_id=row["task_id"],
                    url=row["url"],
                    assigned_to=row["assigned_to"],
                    created_by=row["created_by"],
                    created_at=row["created_at"]
                )
                for row in cursor.fetchall()
            ]

    def remove_task(self, chat_id: int, task_id: str) -> Optional[Task]:
        """Remove a task by its ID and return the removed task, or None if not found."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, chat_id, task_id, url, assigned_to, created_by, created_at
                FROM tasks
                WHERE chat_id = ? AND task_id = ?
                """,
                (chat_id, task_id)
            )
            row = cursor.fetchone()
            
            if row is None:
                return None
            
            task = Task(
                id=row["id"],
                chat_id=row["chat_id"],
                task_id=row["task_id"],
                url=row["url"],
                assigned_to=row["assigned_to"],
                created_by=row["created_by"],
                created_at=row["created_at"]
            )
            
            conn.execute(
                "DELETE FROM tasks WHERE chat_id = ? AND task_id = ?",
                (chat_id, task_id)
            )
            conn.commit()
            return task
