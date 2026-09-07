"""
Database Manager for FOCUS Shift Performance Tracking

Handles SQLite database initialization, delay reason tracking, and performance score storage.
"""

import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from datetime import datetime, date
from contextlib import contextmanager


class DatabaseManager:
    """Manages SQLite database for shift performance tracking"""

    def __init__(self, db_path: str = None):
        """
        Initialize database manager

        Args:
            db_path: Path to SQLite database file. If None, uses default location.
        """
        if db_path is None:
            # Default to data/shift_performance.db
            project_root = Path(__file__).parent.parent.parent
            db_path = project_root / 'data' / 'shift_performance.db'

        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize database schema
        self._initialize_database()

    @contextmanager
    def get_connection(self):
        """Context manager for database connections"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row  # Enable dict-like access
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize_database(self):
        """Initialize database schema from schema.sql"""
        schema_path = Path(__file__).parent / 'schema.sql'

        if not schema_path.exists():
            raise FileNotFoundError(f"Database schema file not found: {schema_path}")

        with open(schema_path, 'r') as f:
            schema_sql = f.read()

        with self.get_connection() as conn:
            conn.executescript(schema_sql)

    # ========================================================================
    # DELAY REASON MANAGEMENT
    # ========================================================================

    def add_delay_reason(self, task_soi: str, line_number: int,
                        shift_date: date, shift_number: int,
                        team: str, delay_reason: str,
                        notes: str = None, entered_by: str = None,
                        held_predecessors: List[Dict] = None) -> int:
        """
        Add or update a delay reason for a task

        Args:
            task_soi: Task SOI identifier
            line_number: Aircraft line number (predecessors inherit this - always same aircraft)
            shift_date: Date of the shift
            shift_number: Shift number (1, 2, or 3)
            team: Team name
            delay_reason: Delay reason category code
            notes: Optional additional notes
            entered_by: User who entered the reason (REQUIRED in practice)
            held_predecessors: List of predecessor dicts if delay_reason is HELD_PREDECESSOR
                              Each dict: {'predecessor_task_soi': str (optional),
                                         'notes': str (required)}
                              NOTE: Predecessors are always on same line_number (same aircraft)

        Returns:
            ID of the inserted/updated record
        """
        with self.get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO task_delay_reasons
                (task_soi, line_number, shift_date, shift_number, team,
                 delay_reason, notes, entered_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_soi, line_number, shift_date, shift_number)
                DO UPDATE SET
                    delay_reason = excluded.delay_reason,
                    notes = excluded.notes,
                    entered_by = excluded.entered_by,
                    entered_at = CURRENT_TIMESTAMP
            """, (task_soi, line_number, shift_date, shift_number, team,
                  delay_reason, notes, entered_by))

            delay_reason_id = cursor.lastrowid

            # If delay reason is HELD_PREDECESSOR, handle predecessor entries
            if delay_reason == 'HELD_PREDECESSOR' and held_predecessors:
                # First, delete existing predecessor entries for this delay reason
                conn.execute("""
                    DELETE FROM held_predecessor_details
                    WHERE delay_reason_id = ?
                """, (delay_reason_id,))

                # Insert new predecessor entries
                # NOTE: Predecessors are always on same line_number as delayed task
                # (line_number is in parent task_delay_reasons, not stored here)
                for pred in held_predecessors:
                    conn.execute("""
                        INSERT INTO held_predecessor_details
                        (delay_reason_id, predecessor_task_soi, notes)
                        VALUES (?, ?, ?)
                    """, (delay_reason_id,
                          pred.get('predecessor_task_soi'),
                          pred.get('notes', '')))

            return delay_reason_id

    def get_delay_reason(self, task_soi: str, line_number: int,
                        shift_date: date, shift_number: int) -> Optional[Dict]:
        """
        Get delay reason for a specific task

        Returns:
            Dict with delay reason details (includes held_predecessors if applicable), or None if not found
        """
        with self.get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM task_delay_reasons
                WHERE task_soi = ? AND line_number = ?
                  AND shift_date = ? AND shift_number = ?
            """, (task_soi, line_number, shift_date, shift_number))

            row = cursor.fetchone()
            if not row:
                return None

            result = dict(row)

            # If delay reason is HELD_PREDECESSOR, fetch predecessor details
            if result.get('delay_reason') == 'HELD_PREDECESSOR':
                pred_cursor = conn.execute("""
                    SELECT predecessor_task_soi, notes
                    FROM held_predecessor_details
                    WHERE delay_reason_id = ?
                    ORDER BY id
                """, (result['id'],))

                result['held_predecessors'] = [dict(pred_row) for pred_row in pred_cursor.fetchall()]
                # Note: line_number is inherited from parent task_delay_reasons record

            return result

    def get_shift_delay_reasons(self, shift_date: date, shift_number: int,
                               team: str = None) -> List[Dict]:
        """
        Get all delay reasons for a shift

        Args:
            shift_date: Date of the shift
            shift_number: Shift number (1, 2, or 3)
            team: Optional team filter

        Returns:
            List of delay reason dicts
        """
        with self.get_connection() as conn:
            if team:
                cursor = conn.execute("""
                    SELECT * FROM task_delay_reasons
                    WHERE shift_date = ? AND shift_number = ? AND team = ?
                    ORDER BY entered_at DESC
                """, (shift_date, shift_number, team))
            else:
                cursor = conn.execute("""
                    SELECT * FROM task_delay_reasons
                    WHERE shift_date = ? AND shift_number = ?
                    ORDER BY team, entered_at DESC
                """, (shift_date, shift_number))

            return [dict(row) for row in cursor.fetchall()]

    def get_delay_reason_categories(self, active_only: bool = True) -> List[Dict]:
        """
        Get list of delay reason categories

        Args:
            active_only: If True, only return active categories

        Returns:
            List of category dicts
        """
        with self.get_connection() as conn:
            if active_only:
                cursor = conn.execute("""
                    SELECT * FROM delay_reason_categories
                    WHERE is_active = 1
                    ORDER BY display_order
                """)
            else:
                cursor = conn.execute("""
                    SELECT * FROM delay_reason_categories
                    ORDER BY display_order
                """)

            return [dict(row) for row in cursor.fetchall()]

    # ========================================================================
    # SHIFT PERFORMANCE SCORE MANAGEMENT
    # ========================================================================

    def save_performance_score(self, shift_date: date, shift_number: int,
                              level: str, entity_name: str,
                              planned_points: int, earned_points: int,
                              score_percentage: float, grade: str,
                              total_tasks_planned: int, tasks_completed: int,
                              tasks_incomplete: int,
                              tasks_incomplete_with_reason: int,
                              tasks_incomplete_no_reason: int,
                              unscheduled_tasks_completed: int,
                              schedule_before_file: str = None,
                              schedule_after_file: str = None) -> int:
        """
        Save shift performance score

        Args:
            shift_date: Date of the shift
            shift_number: Shift number (1, 2, or 3)
            level: 'team', 'superintendent', or 'factory'
            entity_name: Name of the entity (team/superintendent/factory)
            planned_points: Total points that could be earned
            earned_points: Actual points earned (can be negative)
            score_percentage: (earned_points / planned_points) * 100
            grade: Letter grade (A+, A, B, C, D, F)
            total_tasks_planned: Total tasks on plan
            tasks_completed: Tasks completed
            tasks_incomplete: Tasks not completed
            tasks_incomplete_with_reason: Incomplete tasks with delay reason
            tasks_incomplete_no_reason: Incomplete tasks without delay reason
            unscheduled_tasks_completed: Tasks completed but not on plan
            schedule_before_file: Filename of before schedule
            schedule_after_file: Filename of after schedule

        Returns:
            ID of the inserted/updated record
        """
        with self.get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO shift_performance_scores
                (shift_date, shift_number, level, entity_name,
                 planned_points, earned_points, score_percentage, grade,
                 total_tasks_planned, tasks_completed, tasks_incomplete,
                 tasks_incomplete_with_reason, tasks_incomplete_no_reason,
                 unscheduled_tasks_completed,
                 schedule_before_file, schedule_after_file)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(shift_date, shift_number, level, entity_name)
                DO UPDATE SET
                    planned_points = excluded.planned_points,
                    earned_points = excluded.earned_points,
                    score_percentage = excluded.score_percentage,
                    grade = excluded.grade,
                    total_tasks_planned = excluded.total_tasks_planned,
                    tasks_completed = excluded.tasks_completed,
                    tasks_incomplete = excluded.tasks_incomplete,
                    tasks_incomplete_with_reason = excluded.tasks_incomplete_with_reason,
                    tasks_incomplete_no_reason = excluded.tasks_incomplete_no_reason,
                    unscheduled_tasks_completed = excluded.unscheduled_tasks_completed,
                    schedule_before_file = excluded.schedule_before_file,
                    schedule_after_file = excluded.schedule_after_file,
                    calculated_at = CURRENT_TIMESTAMP
            """, (shift_date, shift_number, level, entity_name,
                  planned_points, earned_points, score_percentage, grade,
                  total_tasks_planned, tasks_completed, tasks_incomplete,
                  tasks_incomplete_with_reason, tasks_incomplete_no_reason,
                  unscheduled_tasks_completed,
                  schedule_before_file, schedule_after_file))

            return cursor.lastrowid

    def get_shift_performance(self, shift_date: date, shift_number: int,
                             level: str = None, entity_name: str = None) -> List[Dict]:
        """
        Get shift performance scores

        Args:
            shift_date: Date of the shift
            shift_number: Shift number (1, 2, or 3)
            level: Optional filter by level ('team', 'superintendent', 'factory')
            entity_name: Optional filter by entity name

        Returns:
            List of performance score dicts
        """
        with self.get_connection() as conn:
            query = """
                SELECT * FROM shift_performance_scores
                WHERE shift_date = ? AND shift_number = ?
            """
            params = [shift_date, shift_number]

            if level:
                query += " AND level = ?"
                params.append(level)

            if entity_name:
                query += " AND entity_name = ?"
                params.append(entity_name)

            query += " ORDER BY level, score_percentage DESC"

            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    # ========================================================================
    # SCHEDULE COMPLETION HISTORY (per-line slide tracking)
    # ========================================================================

    def save_completion_history(self, products: List[Dict],
                                shift_date: date, shift_number: int,
                                schedule_file: str = None) -> int:
        """
        Persist projected completion per line number from the optimizer's products array.

        Simply reads projectedCompletion and completionAbsMinutes from each product
        and stores them. No model-minute math — the optimizer already computed these.

        Args:
            products: The 'products' array from a schedule JSON
            shift_date: Work day date
            shift_number: Shift (1, 2, or 3)
            schedule_file: Source schedule filename

        Returns:
            Number of rows inserted/updated
        """
        count = 0
        with self.get_connection() as conn:
            for p in products:
                line_number = p.get('line_number')
                completion_abs = p.get('completionAbsMinutes')
                projected = p.get('projectedCompletion', '')
                if line_number is None or not completion_abs:
                    continue

                conn.execute("""
                    INSERT INTO schedule_completion_history
                    (line_number, shift_date, shift_number,
                     projected_completion, completion_abs_minutes,
                     delivery_date, lateness_days, total_tasks_remaining,
                     schedule_file)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(line_number, shift_date, shift_number)
                    DO UPDATE SET
                        projected_completion = excluded.projected_completion,
                        completion_abs_minutes = excluded.completion_abs_minutes,
                        delivery_date = excluded.delivery_date,
                        lateness_days = excluded.lateness_days,
                        total_tasks_remaining = excluded.total_tasks_remaining,
                        schedule_file = excluded.schedule_file,
                        recorded_at = CURRENT_TIMESTAMP
                """, (line_number, shift_date, shift_number,
                      projected, completion_abs,
                      p.get('deliveryDate', ''),
                      p.get('latenessDays', 0),
                      p.get('totalTasks', 0),
                      schedule_file))
                count += 1
        return count

    def get_completion_history(self, line_number: int,
                               shifts: int = 30) -> List[Dict]:
        """
        Get completion projection history for a single aircraft.

        Returns rows ordered chronologically so you can see the projected
        completion datetime shift-over-shift.

        Args:
            line_number: Aircraft line number
            shifts: Max number of recent shift records to return

        Returns:
            List of dicts with projected_completion, completion_abs_minutes, etc.
        """
        with self.get_connection() as conn:
            cursor = conn.execute("""
                SELECT line_number, shift_date, shift_number,
                       projected_completion, completion_abs_minutes,
                       delivery_date, lateness_days, total_tasks_remaining,
                       schedule_file, recorded_at
                FROM schedule_completion_history
                WHERE line_number = ?
                ORDER BY shift_date ASC, shift_number ASC
                LIMIT ?
            """, (line_number, shifts))
            return [dict(r) for r in cursor.fetchall()]

    def get_slide_summary(self) -> List[Dict]:
        """
        Get current slide status for all tracked aircraft.

        For each line_number returns:
        - latest projected_completion (current position)
        - best-ever projected_completion (high-water mark)
        - cumulative slide = current completion_abs_minutes - min(completion_abs_minutes)

        All derived from stored optimizer output — no model-minute math.
        """
        with self.get_connection() as conn:
            cursor = conn.execute("""
                SELECT
                    cur.line_number,
                    cur.projected_completion AS current_projected,
                    cur.completion_abs_minutes AS current_abs,
                    cur.delivery_date,
                    cur.lateness_days,
                    cur.total_tasks_remaining,
                    cur.shift_date AS current_shift_date,
                    cur.shift_number AS current_shift_number,
                    hw.min_abs AS high_water_abs,
                    hw.hw_shift_date AS high_water_shift_date,
                    hw.hw_shift_number AS high_water_shift_number,
                    hw.hw_projected AS high_water_projected,
                    (cur.completion_abs_minutes - hw.min_abs) AS cumulative_slide_minutes,
                    prev.completion_abs_minutes AS prev_abs,
                    CASE WHEN prev.completion_abs_minutes IS NOT NULL
                         THEN cur.completion_abs_minutes - prev.completion_abs_minutes
                         ELSE 0
                    END AS shift_delta_minutes
                FROM (
                    -- Latest record per line_number
                    SELECT h1.*
                    FROM schedule_completion_history h1
                    INNER JOIN (
                        SELECT line_number,
                               MAX(shift_date || '_' || shift_number) AS max_key
                        FROM schedule_completion_history
                        GROUP BY line_number
                    ) latest ON h1.line_number = latest.line_number
                        AND (h1.shift_date || '_' || h1.shift_number) = latest.max_key
                ) cur
                LEFT JOIN (
                    -- High-water mark (minimum completion_abs_minutes ever seen)
                    SELECT h2.line_number,
                           MIN(h2.completion_abs_minutes) AS min_abs,
                           h2.shift_date AS hw_shift_date,
                           h2.shift_number AS hw_shift_number,
                           h2.projected_completion AS hw_projected
                    FROM schedule_completion_history h2
                    INNER JOIN (
                        SELECT line_number, MIN(completion_abs_minutes) AS min_abs
                        FROM schedule_completion_history
                        GROUP BY line_number
                    ) hw_min ON h2.line_number = hw_min.line_number
                        AND h2.completion_abs_minutes = hw_min.min_abs
                    GROUP BY h2.line_number
                ) hw ON cur.line_number = hw.line_number
                LEFT JOIN (
                    -- Previous shift record (second-most-recent)
                    SELECT h3.*
                    FROM schedule_completion_history h3
                    INNER JOIN (
                        SELECT line_number,
                               MAX(shift_date || '_' || shift_number) AS max_key
                        FROM schedule_completion_history
                        WHERE (shift_date || '_' || shift_number) < (
                            SELECT MAX(shift_date || '_' || shift_number)
                            FROM schedule_completion_history s2
                            WHERE s2.line_number = schedule_completion_history.line_number
                        )
                        GROUP BY line_number
                    ) prev_latest ON h3.line_number = prev_latest.line_number
                        AND (h3.shift_date || '_' || h3.shift_number) = prev_latest.max_key
                ) prev ON cur.line_number = prev.line_number
                ORDER BY cumulative_slide_minutes DESC
            """)
            return [dict(r) for r in cursor.fetchall()]

    def get_team_performance_history(self, team_name: str, days: int = 30) -> List[Dict]:
        """
        Get performance history for a team

        Args:
            team_name: Team name
            days: Number of days of history to retrieve

        Returns:
            List of performance score dicts, ordered by date/shift
        """
        with self.get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM shift_performance_scores
                WHERE level = 'team' AND entity_name = ?
                  AND shift_date >= date('now', '-' || ? || ' days')
                ORDER BY shift_date DESC, shift_number DESC
            """, (team_name, days))

            return [dict(row) for row in cursor.fetchall()]
