import os
import sqlite3
from typing import Optional
from dataclasses import dataclass
from datetime import date, datetime, timezone

from review_urls import parse_review_url

# Use /app/data in Docker, current directory otherwise
DATA_DIR = os.environ.get("DATA_DIR", ".")
DB_PATH = os.path.join(DATA_DIR, "workqueue.db")


def utc_timestamp(now: Optional[datetime] = None) -> str:
    """Use one sortable, timezone-aware UTC representation for new records."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required")
    return now.astimezone(timezone.utc).isoformat(timespec="microseconds")


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
    created_by_id: Optional[int] = None


@dataclass
class ReviewActivity:
    chat_id: int
    review_identity: Optional[tuple[str, str, str]]
    outcome: str
    created_by: str
    created_by_id: Optional[int]
    completed_by: str
    completed_by_id: Optional[int]
    completed_at: datetime


@dataclass
class LeaderboardSettings:
    chat_id: int
    enabled: bool
    enabled_since: datetime


@dataclass
class Reminder:
    chat_id: int
    cron_expression: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class Database:
    def __init__(self, db_path: str = DB_PATH, now: Optional[datetime] = None):
        self.db_path = db_path
        # Ensure data directory exists
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._init_db(now)

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self, now: Optional[datetime] = None) -> None:
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
                    created_by_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, task_id),
                    UNIQUE(chat_id, seq_num)
                )
            """)
            # Existing queue entries keep their saved names and a NULL user ID.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
            if "created_by_id" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN created_by_id INTEGER")
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS review_activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    queue_task_id INTEGER NOT NULL UNIQUE,
                    chat_id INTEGER NOT NULL,
                    task_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    review_host TEXT,
                    review_project TEXT,
                    review_number TEXT,
                    outcome TEXT NOT NULL CHECK(outcome IN ('done', 'bounce')),
                    created_by TEXT NOT NULL,
                    created_by_id INTEGER,
                    completed_by TEXT NOT NULL,
                    completed_by_id INTEGER,
                    completed_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS review_activity_chat_time
                ON review_activity (chat_id, completed_at)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS leaderboard_users (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    seen_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, user_id, name)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS leaderboard_settings (
                    chat_id INTEGER PRIMARY KEY,
                    enabled BOOLEAN NOT NULL DEFAULT 1,
                    enabled_since TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS leaderboard_deliveries (
                    chat_id INTEGER NOT NULL,
                    week_start TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, week_start)
                )
            """)
            timestamp = utc_timestamp(now)
            conn.execute("""
                INSERT OR IGNORE INTO leaderboard_settings
                    (chat_id, enabled, enabled_since, created_at, updated_at)
                SELECT chat_id, 1, ?, ?, ? FROM (
                    SELECT chat_id FROM seq_counters
                    UNION SELECT chat_id FROM tasks
                    UNION SELECT chat_id FROM reminders
                )
            """, (timestamp, timestamp, timestamp))

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

    def _record_user(self, conn: sqlite3.Connection, chat_id: int, user_id: Optional[int], name: str, timestamp: str) -> None:
        if user_id is not None:
            conn.execute("""
                INSERT INTO leaderboard_users (chat_id, user_id, name, seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id, name) DO UPDATE SET seen_at = excluded.seen_at
            """, (chat_id, user_id, name, timestamp))

    def _enroll_leaderboard(self, conn: sqlite3.Connection, chat_id: int, timestamp: str) -> None:
        conn.execute("""
            INSERT OR IGNORE INTO leaderboard_settings
                (chat_id, enabled, enabled_since, created_at, updated_at)
            VALUES (?, 1, ?, ?, ?)
        """, (chat_id, timestamp, timestamp, timestamp))

    def add_task(self, chat_id: int, task_id: str, url: str, assignees: list[str], created_by: str,
                 created_by_id: Optional[int] = None, now: Optional[datetime] = None) -> Optional[int]:
        """Add a task. Returns sequence number if added, None if already exists."""
        try:
            with self._get_connection() as conn:
                seq_num = self._get_next_seq_num(conn, chat_id)
                # Keep assigned_to for backward compatibility (use first assignee or 'unassigned')
                assigned_to = assignees[0] if assignees else "unassigned"
                timestamp = utc_timestamp(now)
                
                cursor = conn.execute(
                    """
                    INSERT INTO tasks (chat_id, seq_num, task_id, url, assigned_to, created_by, created_by_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (chat_id, seq_num, task_id, url, assigned_to, created_by, created_by_id, timestamp)
                )
                
                # Get the inserted task id and add assignees
                task_db_id = cursor.lastrowid
                self._set_task_assignees(conn, task_db_id, assignees)
                self._record_user(conn, chat_id, created_by_id, created_by, timestamp)
                self._enroll_leaderboard(conn, chat_id, timestamp)
                
                conn.commit()
                return seq_num
        except sqlite3.IntegrityError:
            return None

    def get_tasks(self, chat_id: int) -> list[Task]:
        """Get all tasks for a chat, ordered by sequence number."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT *
                FROM tasks
                WHERE chat_id = ?
                ORDER BY seq_num ASC
                """,
                (chat_id,)
            )
            return [self._row_to_task(conn, row) for row in cursor.fetchall()]

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
            created_at=row["created_at"],
            created_by_id=row["created_by_id"]
        )

    def remove_task_by_id(self, chat_id: int, task_id: str) -> Optional[Task]:
        """Remove and archive a task without an identified reviewer."""
        return self._complete_task(chat_id, "task_id", task_id, "done", "Unknown")

    def remove_task_by_seq(self, chat_id: int, seq_num: int) -> Optional[Task]:
        """Remove and archive a task without an identified reviewer."""
        return self._complete_task(chat_id, "seq_num", seq_num, "done", "Unknown")

    def complete_task(self, chat_id: int, task_db_id: int, outcome: str, completed_by: str,
                      completed_by_id: Optional[int] = None, completed_at: Optional[datetime] = None) -> Optional[Task]:
        """Archive exactly this queue entry and remove it and its assignees atomically."""
        return self._complete_task(chat_id, "id", task_db_id, outcome, completed_by,
                                   completed_by_id, completed_at)

    def _complete_task(self, chat_id: int, column: str, value: int | str, outcome: str,
                       completed_by: str, completed_by_id: Optional[int] = None,
                       completed_at: Optional[datetime] = None) -> Optional[Task]:
        if outcome not in ("done", "bounce"):
            raise ValueError("Outcome must be done or bounce")
        with self._get_connection() as conn:
            # Serialize the read and deletion so repeated commands cannot create events.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT * FROM tasks WHERE chat_id = ? AND {column} = ?",
                (chat_id, value)
            ).fetchone()
            if row is None:
                return None

            task = self._row_to_task(conn, row)
            parsed = parse_review_url(task.url)
            identity = parsed[1] if parsed else (None, None, None)
            timestamp = utc_timestamp(completed_at)
            conn.execute("""
                INSERT INTO review_activity (
                    queue_task_id, chat_id, task_id, url, review_host, review_project, review_number,
                    outcome, created_by, created_by_id, completed_by, completed_by_id, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (task.id, chat_id, task.task_id, task.url, *identity, outcome,
                  task.created_by, task.created_by_id, completed_by, completed_by_id, timestamp))
            self._record_user(conn, chat_id, completed_by_id, completed_by, timestamp)
            conn.execute("DELETE FROM task_assignees WHERE task_id = ?", (task.id,))
            conn.execute("DELETE FROM tasks WHERE id = ?", (task.id,))
            return task

    def get_review_activity(self, chat_id: int, start: datetime, end: datetime) -> list[ReviewActivity]:
        """Fetch activity by UTC completion time, with an exclusive end boundary."""
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM review_activity
                WHERE chat_id = ? AND completed_at >= ? AND completed_at < ?
                ORDER BY completed_at, id
            """, (chat_id, utc_timestamp(start), utc_timestamp(end))).fetchall()
            return [ReviewActivity(
                chat_id=row["chat_id"],
                review_identity=(row["review_host"], row["review_project"], row["review_number"])
                    if row["review_host"] is not None else None,
                outcome=row["outcome"],
                created_by=row["created_by"], created_by_id=row["created_by_id"],
                completed_by=row["completed_by"], completed_by_id=row["completed_by_id"],
                completed_at=datetime.fromisoformat(row["completed_at"])
            ) for row in rows]

    def get_known_users(self, chat_id: int) -> list[tuple[int, str]]:
        """Return all observed names, oldest first, to retain rename and ambiguity evidence."""
        with self._get_connection() as conn:
            return [(row["user_id"], row["name"]) for row in conn.execute("""
                SELECT user_id, name FROM leaderboard_users
                WHERE chat_id = ? ORDER BY seen_at, rowid
            """, (chat_id,))]

    def set_leaderboard_enabled(self, chat_id: int, enabled: bool, now: Optional[datetime] = None) -> None:
        timestamp = utc_timestamp(now)
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO leaderboard_settings
                    (chat_id, enabled, enabled_since, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    enabled_since = CASE
                        WHEN leaderboard_settings.enabled = 0 AND excluded.enabled = 1
                        THEN excluded.enabled_since ELSE leaderboard_settings.enabled_since END,
                    updated_at = excluded.updated_at
            """, (chat_id, enabled, timestamp, timestamp, timestamp))

    def get_leaderboard_settings(self, chat_id: int) -> Optional[LeaderboardSettings]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM leaderboard_settings WHERE chat_id = ?", (chat_id,)).fetchone()
            return self._row_to_leaderboard_settings(row) if row else None

    def _row_to_leaderboard_settings(self, row: sqlite3.Row) -> LeaderboardSettings:
        return LeaderboardSettings(row["chat_id"], bool(row["enabled"]), datetime.fromisoformat(row["enabled_since"]))

    def get_active_leaderboard_settings(self) -> list[LeaderboardSettings]:
        with self._get_connection() as conn:
            return [self._row_to_leaderboard_settings(row) for row in conn.execute(
                "SELECT * FROM leaderboard_settings WHERE enabled = 1 ORDER BY chat_id"
            )]

    def has_leaderboard_delivery(self, chat_id: int, week_start: date) -> bool:
        with self._get_connection() as conn:
            return conn.execute("""
                SELECT 1 FROM leaderboard_deliveries WHERE chat_id = ? AND week_start = ?
            """, (chat_id, week_start.isoformat())).fetchone() is not None

    def record_leaderboard_delivery(self, chat_id: int, week_start: date, now: Optional[datetime] = None) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO leaderboard_deliveries (chat_id, week_start, sent_at)
                VALUES (?, ?, ?)
            """, (chat_id, week_start.isoformat(), utc_timestamp(now)))

    def update_task_assignees_by_seq(self, chat_id: int, seq_num: int, assignees: list[str]) -> Optional[Task]:
        """Update a task's assignees by sequence number and return the updated task, or None if not found."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT *
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
                SELECT *
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
                SELECT *
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
                SELECT *
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
