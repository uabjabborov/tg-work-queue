import os
import sqlite3
from typing import Optional
from dataclasses import dataclass
from datetime import datetime

# Use /app/data in Docker, current directory otherwise
DATA_DIR = os.environ.get("DATA_DIR", ".")
DB_PATH = os.path.join(DATA_DIR, "workqueue.db")


@dataclass
class Task:
    id: int
    chat_id: int
    seq_num: int
    task_id: str
    url: str
    assignees: list[str]
    created_by: str
    created_at: datetime


@dataclass
class Reminder:
    chat_id: int
    cron_expression: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        # Ensure data directory exists
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS task_assignees (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    assignee TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                    UNIQUE(task_id, assignee)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS seq_counters (
                    chat_id INTEGER PRIMARY KEY,
                    next_num INTEGER DEFAULT 1
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    chat_id INTEGER PRIMARY KEY,
                    cron_expression TEXT NOT NULL,
                    enabled BOOLEAN NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            
            # Migrate existing assigned_to data to task_assignees table
            self._migrate_assignees(conn)

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

    def _migrate_assignees(self, conn: sqlite3.Connection) -> None:
        """Migrate existing assigned_to data to task_assignees table."""
        # Check if migration is needed (task_assignees is empty)
        cursor = conn.execute("SELECT COUNT(*) as count FROM task_assignees")
        if cursor.fetchone()["count"] > 0:
            return  # Already migrated
        
        # Get all tasks with assignees
        cursor = conn.execute("""
            SELECT id, assigned_to FROM tasks 
            WHERE assigned_to != 'unassigned' AND assigned_to != ''
        """)
        
        for row in cursor.fetchall():
            task_id = row["id"]
            assigned_to = row["assigned_to"]
            
            # Insert into task_assignees table
            try:
                conn.execute(
                    "INSERT INTO task_assignees (task_id, assignee) VALUES (?, ?)",
                    (task_id, assigned_to)
                )
            except sqlite3.IntegrityError:
                pass  # Skip duplicates
        
        conn.commit()

    def _get_task_assignees(self, conn: sqlite3.Connection, task_id: int) -> list[str]:
        """Get all assignees for a task."""
        cursor = conn.execute(
            "SELECT assignee FROM task_assignees WHERE task_id = ? ORDER BY assignee",
            (task_id,)
        )
        return [row["assignee"] for row in cursor.fetchall()]

    def _set_task_assignees(self, conn: sqlite3.Connection, task_id: int, assignees: list[str]) -> None:
        """Replace all assignees for a task."""
        # Delete existing assignees
        conn.execute("DELETE FROM task_assignees WHERE task_id = ?", (task_id,))
        
        # Insert new assignees
        for assignee in assignees:
            if assignee:  # Skip empty strings
                try:
                    conn.execute(
                        "INSERT INTO task_assignees (task_id, assignee) VALUES (?, ?)",
                        (task_id, assignee)
                    )
                except sqlite3.IntegrityError:
                    pass  # Skip duplicates

    def add_task(self, chat_id: int, task_id: str, url: str, assignees: list[str], created_by: str) -> Optional[int]:
        """Add a task. Returns sequence number if added, None if already exists."""
        try:
            with self._get_connection() as conn:
                seq_num = self._get_next_seq_num(conn, chat_id)
                # Keep assigned_to for backward compatibility (use first assignee or 'unassigned')
                assigned_to = assignees[0] if assignees else "unassigned"
                
                cursor = conn.execute(
                    """
                    INSERT INTO tasks (chat_id, seq_num, task_id, url, assigned_to, created_by)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (chat_id, seq_num, task_id, url, assigned_to, created_by)
                )
                
                # Get the inserted task id and add assignees
                task_db_id = cursor.lastrowid
                self._set_task_assignees(conn, task_db_id, assignees)
                
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
            tasks = []
            for row in cursor.fetchall():
                assignees = self._get_task_assignees(conn, row["id"])
                tasks.append(Task(
                    id=row["id"],
                    chat_id=row["chat_id"],
                    seq_num=row["seq_num"],
                    task_id=row["task_id"],
                    url=row["url"],
                    assignees=assignees,
                    created_by=row["created_by"],
                    created_at=row["created_at"]
                ))
            return tasks

    def _row_to_task(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Task:
        assignees = self._get_task_assignees(conn, row["id"])
        return Task(
            id=row["id"],
            chat_id=row["chat_id"],
            seq_num=row["seq_num"],
            task_id=row["task_id"],
            url=row["url"],
            assignees=assignees,
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
            
            task = self._row_to_task(conn, row)
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
            
            task = self._row_to_task(conn, row)
            conn.execute(
                "DELETE FROM tasks WHERE chat_id = ? AND seq_num = ?",
                (chat_id, seq_num)
            )
            conn.commit()
            return task

    def update_task_assignees_by_seq(self, chat_id: int, seq_num: int, assignees: list[str]) -> Optional[Task]:
        """Update a task's assignees by sequence number and return the updated task, or None if not found."""
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
            
            task_db_id = row["id"]
            
            # Update assignees in junction table
            self._set_task_assignees(conn, task_db_id, assignees)
            
            # Update assigned_to for backward compatibility
            assigned_to = assignees[0] if assignees else "unassigned"
            conn.execute(
                "UPDATE tasks SET assigned_to = ? WHERE chat_id = ? AND seq_num = ?",
                (assigned_to, chat_id, seq_num)
            )
            conn.commit()
            
            # Return updated task
            cursor = conn.execute(
                """
                SELECT id, chat_id, seq_num, task_id, url, assigned_to, created_by, created_at
                FROM tasks
                WHERE chat_id = ? AND seq_num = ?
                """,
                (chat_id, seq_num)
            )
            row = cursor.fetchone()
            return self._row_to_task(conn, row)

    def update_task_assignees_by_id(self, chat_id: int, task_id: str, assignees: list[str]) -> Optional[Task]:
        """Update a task's assignees by task_id and return the updated task, or None if not found."""
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
            
            task_db_id = row["id"]
            
            # Update assignees in junction table
            self._set_task_assignees(conn, task_db_id, assignees)
            
            # Update assigned_to for backward compatibility
            assigned_to = assignees[0] if assignees else "unassigned"
            conn.execute(
                "UPDATE tasks SET assigned_to = ? WHERE chat_id = ? AND task_id = ?",
                (assigned_to, chat_id, task_id)
            )
            conn.commit()
            
            # Return updated task
            cursor = conn.execute(
                """
                SELECT id, chat_id, seq_num, task_id, url, assigned_to, created_by, created_at
                FROM tasks
                WHERE chat_id = ? AND task_id = ?
                """,
                (chat_id, task_id)
            )
            row = cursor.fetchone()
            return self._row_to_task(conn, row)

    def set_reminder(self, chat_id: int, cron_expression: str, enabled: bool = True) -> None:
        """Set or update a reminder configuration for a chat."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO reminders (chat_id, cron_expression, enabled, created_at, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id) DO UPDATE SET
                    cron_expression = excluded.cron_expression,
                    enabled = excluded.enabled,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (chat_id, cron_expression, enabled)
            )
            conn.commit()

    def get_reminder(self, chat_id: int) -> Optional[Reminder]:
        """Get reminder configuration for a chat."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT chat_id, cron_expression, enabled, created_at, updated_at
                FROM reminders
                WHERE chat_id = ?
                """,
                (chat_id,)
            )
            row = cursor.fetchone()
            
            if row is None:
                return None
            
            return Reminder(
                chat_id=row["chat_id"],
                cron_expression=row["cron_expression"],
                enabled=bool(row["enabled"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"]
            )

    def get_all_active_reminders(self) -> list[Reminder]:
        """Get all enabled reminders."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT chat_id, cron_expression, enabled, created_at, updated_at
                FROM reminders
                WHERE enabled = 1
                """
            )
            return [
                Reminder(
                    chat_id=row["chat_id"],
                    cron_expression=row["cron_expression"],
                    enabled=bool(row["enabled"]),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"]
                )
                for row in cursor.fetchall()
            ]

    def disable_reminder(self, chat_id: int) -> bool:
        """Disable a reminder without deleting it. Returns True if reminder exists, False otherwise."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "UPDATE reminders SET enabled = 0, updated_at = CURRENT_TIMESTAMP WHERE chat_id = ?",
                (chat_id,)
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_reminder(self, chat_id: int) -> bool:
        """Delete a reminder configuration. Returns True if reminder existed, False otherwise."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM reminders WHERE chat_id = ?",
                (chat_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
