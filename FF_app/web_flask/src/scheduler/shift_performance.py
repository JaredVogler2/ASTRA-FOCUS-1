"""
Shift Performance Comparison Engine

Compares before/after schedules to calculate team performance scores.

Three-Dimensional "Transcript" Model:
  - Throughput (Quantity): What % of today's planned tasks were completed?
  - Alignment (Quality): Were completed tasks the ones the schedule valued most?
    Uses the optimizer's priority_score (composite of delivery urgency, critical path,
    successor count, control station deadlines) rather than an arbitrary rank-based GPA.
  - Impact (Outcome): Did the work improve, hold, or worsen aircraft delivery projections?

Combined Performance Index:
  PI = Throughput × Alignment × Impact Factor
  - Throughput capped at 100% (unscheduled work tracked separately)
  - Alignment = schedule_value_earned / schedule_value_planned (0-1)
  - Impact Factor from lateness deltas (0.5 to 1.5)

Resource-Type Separation:
  Performance is calculated separately per workGroup (mechanic, quality, customer, vendor).
  - Production (mechanic) is the primary KPI: each base SOI has exactly 1 mechanic task,
    so mechanic throughput == ALL SOI-level throughput. No rollup math needed.
  - Quality tasks are inspection segments (INPROC_INSP, FINAL_INSP) derived from base SOIs.
  - Customer tasks are customer acceptance inspections (CC_UAL, CC_DLH, etc.).
  - Vendor tasks are rare external vendor work.
  - "All" view shows combined performance across all workGroups (original behavior).

Rollups are weighted by planned workload, not averaged equally.
A team carrying 2 tasks cannot inflate the superintendent score the way
a team carrying 200 tasks can.
"""

import json
import gzip
import math
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from datetime import datetime, date, timedelta
from collections import defaultdict


WORK_GROUPS = ['mechanic', 'quality', 'customer', 'vendor']
WORK_GROUP_LABELS = {
    'mechanic': 'Production',
    'quality': 'Quality (QA)',
    'customer': 'Customer',
    'vendor': 'Vendor',
    'all': 'All Resources'
}


    # Shift wall-clock start times (hour, minute) in local time
SHIFT_WALL_START = {1: (6, 10), 2: (14, 40), 3: (23, 10)}

# Display reference date: day 0 in the model maps to this calendar date
DISPLAY_REFERENCE_DATE = datetime(2026, 1, 24)

# Model constants
_MINUTES_PER_SHIFT = 460
_MINUTES_PER_DAY = 920  # 2 shifts × 460


def abs_minutes_to_datetime(abs_min: int) -> Optional[datetime]:
    """Convert completionAbsMinutes to a wall-clock datetime.

    The model packs each day as 920 schedulable minutes (S1: 0-459, S2: 460-919).
    This converts back to a real datetime using shift start times.
    """
    if not abs_min:
        return None
    day = abs_min // _MINUTES_PER_DAY
    rem = abs_min - day * _MINUTES_PER_DAY
    shift = 1 if rem < _MINUTES_PER_SHIFT else 2
    min_in_shift = rem - (shift - 1) * _MINUTES_PER_SHIFT
    cal_date = DISPLAY_REFERENCE_DATE + timedelta(days=day)
    sh, sm = SHIFT_WALL_START[shift]
    return cal_date.replace(hour=sh, minute=sm) + timedelta(minutes=min_in_shift)


class ShiftPerformanceEngine:
    """Engine for comparing schedules and calculating shift performance scores"""

    def __init__(self, dags: Dict = None):
        self.dags = dags or {}

    # ========================================================================
    # GRADE HELPERS
    # ========================================================================

    def calculate_grade(self, score: float) -> str:
        """Convert a 0-100 score to letter grade"""
        if score >= 95:
            return 'A+'
        elif score >= 90:
            return 'A'
        elif score >= 85:
            return 'A-'
        elif score >= 80:
            return 'B+'
        elif score >= 75:
            return 'B'
        elif score >= 70:
            return 'B-'
        elif score >= 65:
            return 'C+'
        elif score >= 60:
            return 'C'
        elif score >= 55:
            return 'D+'
        elif score >= 50:
            return 'D'
        else:
            return 'F'

    # ========================================================================
    # SCHEDULE LOADING
    # ========================================================================

    def load_schedule(self, schedule_path: str) -> Dict:
        """Load a schedule envelope (supports .json and .json.gz).

        Routed through the shared adapter loader so MAX (Fable) engine
        envelopes are normalized to the FGI shape before grading —
        otherwise priority_score/priority are absent and every shift
        grades as 0% alignment."""
        from src.max_adapter import load_envelope
        env = load_envelope(schedule_path)
        if env is None:
            raise FileNotFoundError(f"Could not load schedule: {schedule_path}")
        return env

    # ========================================================================
    # CRITICAL PATH DURATION (DAG-based, optimizer-independent)
    # ========================================================================

    @staticmethod
    def _compute_cp_durations(tasks: List[Dict], successors_map: Dict,
                              aircraft_list: List[int]) -> Dict[int, int]:
        """Compute the DAG critical path duration (minutes) per aircraft.

        The critical path is the longest dependency chain through each
        aircraft's remaining tasks — purely a property of the DAG, not the
        optimizer's scheduling decisions.  This is the theoretical minimum
        completion time with unlimited resources.

        Returns:
            {line_number: critical_path_duration_minutes}
        """
        # Build task_id -> duration map.  task_id = "{soi}_{line_number}"
        task_dur: Dict[str, int] = {}
        ac_tasks: Dict[int, set] = defaultdict(set)
        for t in tasks:
            soi = t.get('soi', '')
            ln = t.get('line_number', 0)
            tid = f'{soi}_{ln}'
            dur = t.get('duration', 0) or t.get('duration_minutes', 0) or 0
            task_dur[tid] = dur
            ac_tasks[ln].add(tid)

        results: Dict[int, int] = {}
        for ln in aircraft_list:
            tids = ac_tasks.get(ln, set())
            if not tids:
                results[ln] = 0
                continue

            # DP longest-path from each node (inclusive of own duration)
            memo: Dict[str, int] = {}

            def longest_from(tid: str) -> int:
                if tid in memo:
                    return memo[tid]
                dur = task_dur.get(tid, 0)
                succs = [s for s in successors_map.get(tid, []) if s in tids]
                if not succs:
                    memo[tid] = dur
                    return dur
                best = max(longest_from(s) for s in succs)
                memo[tid] = dur + best
                return dur + best

            results[ln] = max(longest_from(tid) for tid in tids)
        return results

    @staticmethod
    def _enrich_products_with_cp(products: List[Dict], tasks: List[Dict],
                                 successors_map: Dict) -> None:
        """Add ``criticalPathDuration`` (minutes) to each product dict in-place."""
        aircraft_list = [p['line_number'] for p in products]
        cp_map = ShiftPerformanceEngine._compute_cp_durations(
            tasks, successors_map, aircraft_list)
        for p in products:
            p['criticalPathDuration'] = cp_map.get(p['line_number'], 0)

    # ========================================================================
    # SCHEDULE COMPARISON
    # ========================================================================

    def compare_schedules(self, before_tasks: List[Dict], after_tasks: List[Dict],
                         delay_reasons: Dict[Tuple, str] = None) -> Dict:
        """
        Compare task lists to determine completion status.
        Task in before but not after = completed.
        Task in both = incomplete.
        Task in after but not before = unscheduled (new work).
        """
        before_map = {(t['soi'], t['line_number']): t for t in before_tasks}
        after_map = {(t['soi'], t['line_number']): t for t in after_tasks}

        completed = []
        incomplete = []
        unscheduled = []

        for key, task in before_map.items():
            if key not in after_map:
                completed.append(task)
            else:
                has_reason = bool(delay_reasons and key in delay_reasons)
                incomplete.append({
                    **task,
                    'has_delay_reason': has_reason,
                    'delay_reason': delay_reasons.get(key) if delay_reasons else None
                })

        for key, task in after_map.items():
            if key not in before_map:
                unscheduled.append(task)

        return {
            'completed': completed,
            'incomplete': incomplete,
            'unscheduled': unscheduled
        }

    # ========================================================================
    # DIMENSION 1: THROUGHPUT (Did you do enough?)
    # ========================================================================

    def calculate_throughput(self, tasks_completed: int, tasks_planned: int,
                            unscheduled_count: int) -> Dict:
        """
        Throughput = tasks completed / tasks planned for today, capped at 100%.
        Unscheduled tasks tracked separately as bonus productivity.
        """
        if tasks_planned == 0:
            return {
                'tasks_completed': tasks_completed,
                'tasks_planned': tasks_planned,
                'completion_rate': 0.0,
                'unscheduled_completed': unscheduled_count,
                'throughput_score': 0.0,
                'throughput_grade': 'N/A'
            }

        raw_rate = (tasks_completed / tasks_planned) * 100
        capped_rate = min(100.0, raw_rate)

        return {
            'tasks_completed': tasks_completed,
            'tasks_planned': tasks_planned,
            'completion_rate': round(capped_rate, 1),
            'completion_rate_raw': round(raw_rate, 1),
            'unscheduled_completed': unscheduled_count,
            'throughput_score': round(capped_rate, 1),
            'throughput_grade': self.calculate_grade(capped_rate)
        }

    # ========================================================================
    # DIMENSION 2: ALIGNMENT (Did you do the right things?)
    # ========================================================================

    def calculate_alignment(self, completed_tasks: List[Dict],
                           all_planned_tasks: List[Dict],
                           unscheduled_tasks: List[Dict]) -> Dict:
        """
        Alignment measures whether completed tasks were the ones the schedule
        valued most. Uses the optimizer's priority_score field, which is a
        composite of delivery urgency, critical path status, successor count,
        and control station deadlines.

        Alignment Score = schedule_value_earned / schedule_value_planned
        Scaled to 0-100 for display.

        Unscheduled tasks contribute at 25% of average schedule value (bonus
        work, but not what was planned).

        If no tasks were completed AND no unscheduled work was done, alignment
        is 0 — the metric is undefined when there's no work to evaluate.
        """
        if not completed_tasks and not unscheduled_tasks:
            return {
                'alignment_score': 0.0,
                'alignment_grade': 'N/A',
                'schedule_value_earned': 0,
                'schedule_value_planned': sum(t.get('priority_score', 0) for t in all_planned_tasks),
                'avg_completed_value': 0,
                'avg_planned_value': (sum(t.get('priority_score', 0) for t in all_planned_tasks) /
                                      len(all_planned_tasks)) if all_planned_tasks else 0,
                'critical_path_completed': 0,
                'critical_path_total': sum(1 for t in all_planned_tasks if t.get('isCritical')),
                'critical_path_pct': 0.0,
                'high_value_completed': 0,
                'high_value_total': 0
            }

        if not all_planned_tasks:
            return {
                'alignment_score': 0.0,
                'alignment_grade': 'N/A',
                'schedule_value_earned': 0,
                'schedule_value_planned': 0,
                'avg_completed_value': 0,
                'avg_planned_value': 0,
                'critical_path_completed': 0,
                'critical_path_total': 0,
                'critical_path_pct': 0.0,
                'high_value_completed': 0,
                'high_value_total': 0
            }

        # Calculate total planned schedule value
        value_planned = sum(t.get('priority_score', 0) for t in all_planned_tasks)
        avg_planned = value_planned / len(all_planned_tasks) if all_planned_tasks else 0

        # Calculate earned schedule value from completed planned tasks
        value_earned = sum(t.get('priority_score', 0) for t in completed_tasks)

        # Unscheduled tasks contribute at 25% of average planned value (bonus credit)
        unscheduled_bonus = len(unscheduled_tasks) * (avg_planned * 0.25) if unscheduled_tasks else 0

        total_earned = value_earned + unscheduled_bonus

        # Alignment as percentage (capped at 100)
        if value_planned > 0:
            alignment_pct = min(100.0, (total_earned / value_planned) * 100)
        else:
            alignment_pct = 0.0

        # Critical path analysis
        cp_total = sum(1 for t in all_planned_tasks if t.get('isCritical'))
        cp_completed = sum(1 for t in completed_tasks if t.get('isCritical'))

        # High-value tasks (top quartile by priority_score)
        if all_planned_tasks:
            sorted_by_value = sorted(all_planned_tasks, key=lambda t: t.get('priority_score', 0), reverse=True)
            top_quartile_threshold = len(sorted_by_value) // 4 or 1
            high_value_sois = {(t['soi'], t['line_number']) for t in sorted_by_value[:top_quartile_threshold]}
            hv_total = len(high_value_sois)
            hv_completed = sum(1 for t in completed_tasks
                             if (t['soi'], t['line_number']) in high_value_sois)
        else:
            hv_total = 0
            hv_completed = 0

        avg_completed = value_earned / len(completed_tasks) if completed_tasks else 0

        return {
            'alignment_score': round(alignment_pct, 1),
            'alignment_grade': self.calculate_grade(alignment_pct),
            'schedule_value_earned': round(total_earned, 0),
            'schedule_value_planned': round(value_planned, 0),
            'avg_completed_value': round(avg_completed, 0),
            'avg_planned_value': round(avg_planned, 0),
            'critical_path_completed': cp_completed,
            'critical_path_total': cp_total,
            'critical_path_pct': round((cp_completed / cp_total * 100) if cp_total > 0 else 0, 1),
            'high_value_completed': hv_completed,
            'high_value_total': hv_total
        }

    # ========================================================================
    # DIMENSION 3: IMPACT (Did it actually matter for delivery?)
    # ========================================================================

    SHIFT_DURATION = 460  # minutes per shift

    def calculate_impact(self, before_products: List[Dict],
                        after_products: List[Dict]) -> Dict:
        """
        Impact measures schedule movement by comparing the projected
        completion datetime of each aircraft between two optimizer runs.

        For each aircraft the delta is simply:
          schedule_delta = after_completion - before_completion (absolute minutes)
          negative = schedule pulled earlier (improved)
          positive = schedule pushed later (worsened)
          zero = held

        The optimizer output already accounts for team capacity, shift
        constraints, and resource contention.  A simple datetime comparison
        of each aircraft's last scheduled job is the complete answer.

        ``criticalPathDuration`` (DAG longest-path) is included per aircraft
        as diagnostic data — it shows whether the dependency chain shortened,
        independent of the optimizer's resource packing.

        NOTE: currently both optimizer runs use the same starting shift
        (day 0 / S1), so the delta is valid as a relative comparison even
        though absolute dates include phantom past-shift capacity.  When the
        optimizer gains a ``start_shift`` parameter the delta will also
        naturally account for elapsed time.
        """
        empty = {
            'impact_factor': 1.0,
            'impact_score': 50.0,
            'impact_grade': 'C',
            'aircraft_deltas': [],
            'aircraft_improved': 0,
            'aircraft_held': 0,
            'aircraft_worsened': 0,
            'net_hours': 0.0,
            'avg_hours_per_aircraft': 0.0,
            'net_minutes': 0,
            'total_cp_reduction': 0,
            'total_remaining_delta': 0
        }
        if not before_products or not after_products:
            return empty

        after_by_line = {p['line_number']: p for p in after_products}
        deltas = []
        total_cp_reduction = 0
        improved = 0
        held = 0
        worsened = 0
        total_remaining_delta = 0

        for bp in before_products:
            ln = bp['line_number']
            ap = after_by_line.get(ln)
            if not ap:
                continue

            # --- Primary: compare last-job datetime (absolute minutes) ---
            # Use `or 0` to handle None values from older schedules that
            # predate the completionAbsMinutes field (key exists but is None).
            before_abs = bp.get('completionAbsMinutes') or 0
            after_abs = ap.get('completionAbsMinutes') or 0
            # Negative = pulled earlier (good), positive = pushed later (bad)
            schedule_delta = after_abs - before_abs

            # --- DAG critical path (diagnostic) ---
            before_cp_dur = bp.get('criticalPathDuration', 0)
            after_cp_dur = ap.get('criticalPathDuration', 0)
            cp_reduction = before_cp_dur - after_cp_dur  # positive = shortened
            total_cp_reduction += cp_reduction

            # --- Lateness (precise: hours if <24h, decimal days otherwise) ---
            before_late = bp.get('latenessDays', 0)
            after_late = ap.get('latenessDays', 0)
            lateness_delta = round(before_late - after_late, 1)
            before_late_display = bp.get('latenessDisplay', f"{before_late}d")
            after_late_display = ap.get('latenessDisplay', f"{after_late}d")

            before_remaining = bp.get('totalTasks', 0)
            after_remaining = ap.get('totalTasks', 0)
            tasks_removed = before_remaining - after_remaining

            before_cp_count = bp.get('criticalPath', 0)
            after_cp_count = ap.get('criticalPath', 0)

            before_proj = bp.get('projectedCompletion', '')
            after_proj = ap.get('projectedCompletion', '')
            before_remaining_days = bp.get('daysRemaining', 0)
            after_remaining_days = ap.get('daysRemaining', 0)
            remaining_delta = before_remaining_days - after_remaining_days
            total_remaining_delta += remaining_delta

            # Classify: 5-min threshold to filter optimizer tie-breaking noise
            if schedule_delta < -5:
                improved += 1
            elif schedule_delta > 5:
                worsened += 1
            else:
                held += 1

            # Wall-clock hours delta (accounts for overnight gaps, not model minutes)
            before_dt = abs_minutes_to_datetime(before_abs)
            after_dt = abs_minutes_to_datetime(after_abs)
            if before_dt and after_dt:
                wall_clock_delta = (after_dt - before_dt).total_seconds() / 3600.0
            else:
                wall_clock_delta = schedule_delta / 60.0  # fallback to model hours

            # Format lateness delta: hours if <24h, else decimal days
            delta_minutes = after_abs - before_abs  # positive = worsened
            delta_hours = round(abs(delta_minutes) / 60.0, 1)
            delta_days_dec = round(abs(delta_minutes) / _MINUTES_PER_DAY, 1) if delta_minutes != 0 else 0.0
            sign = '+' if delta_minutes > 0 else '-' if delta_minutes < 0 else ''
            if delta_minutes == 0:
                lateness_delta_display = "0h"
            elif delta_hours < 24:
                lateness_delta_display = f"{sign}{delta_hours}h"
            else:
                lateness_delta_display = f"{sign}{delta_days_dec}d"

            deltas.append({
                'line_number': ln,
                'name': bp.get('name', f'Line {ln}'),
                'delivery_date': bp.get('deliveryDate', ''),
                'before_lateness': before_late,
                'after_lateness': after_late,
                'before_lateness_display': before_late_display,
                'after_lateness_display': after_late_display,
                'lateness_delta': lateness_delta,
                'lateness_delta_display': lateness_delta_display,
                'schedule_delta': schedule_delta,
                'hours_delta': round(wall_clock_delta, 1),
                'before_completion_abs': before_abs,
                'after_completion_abs': after_abs,
                'before_completion_dt': before_dt.strftime('%a %b %d, %Y %I:%M %p') if before_dt else None,
                'after_completion_dt': after_dt.strftime('%a %b %d, %Y %I:%M %p') if after_dt else None,
                'before_cp_duration': before_cp_dur,
                'after_cp_duration': after_cp_dur,
                'cp_reduction': cp_reduction,
                'tasks_removed': tasks_removed,
                'before_projected': before_proj,
                'after_projected': after_proj,
                'before_cp': before_cp_count,
                'after_cp': after_cp_count,
                'before_days_remaining': before_remaining_days,
                'after_days_remaining': after_remaining_days
            })

        # ---- Impact: wall-clock hours sum across all aircraft ----
        # hours_delta is real wall-clock hours (after_datetime - before_datetime).
        # Positive = schedule pushed later (delayed), negative = pulled earlier (improved).
        net_hours = sum(d['hours_delta'] for d in deltas)
        net_minutes = round(net_hours * 60)
        aircraft_count = len(deltas)
        avg_hours_per_aircraft = round(net_hours / aircraft_count, 1) if aircraft_count > 0 else 0.0

        # Impact factor/score use inverted sign: positive net_hours_benefit = schedule improved
        net_hours_benefit = -net_hours  # positive = improved, negative = worsened
        if net_hours_benefit > 0:
            impact_factor = min(1.5, 1.0 + (net_hours_benefit * 0.003))
        elif net_hours_benefit < 0:
            impact_factor = max(0.5, 1.0 - (abs(net_hours_benefit) * 0.007))
        else:
            impact_factor = 1.0

        # Score: 50 = neutral, each hour of net improvement ≈ +0.65 pts
        impact_score = min(100.0, max(0.0, 50.0 + (net_hours_benefit * 0.65)))

        return {
            'impact_factor': round(impact_factor, 2),
            'impact_score': round(impact_score, 1),
            'impact_grade': self.calculate_grade(impact_score),
            'aircraft_deltas': sorted(deltas, key=lambda d: d['schedule_delta'],
                                      reverse=True),
            'aircraft_improved': improved,
            'aircraft_held': held,
            'aircraft_worsened': worsened,
            'net_hours': round(net_hours, 1),
            'avg_hours_per_aircraft': avg_hours_per_aircraft,
            'net_minutes': net_minutes,
            'total_cp_reduction': total_cp_reduction,
            'total_remaining_delta': total_remaining_delta
        }

    # ========================================================================
    # SCHEDULE IMPACT ANALYSIS (downstream blocking)
    # ========================================================================

    def analyze_schedule_impact(self, incomplete_tasks: List[Dict],
                                successors_map: Dict) -> Dict:
        """Analyze downstream impact of incomplete tasks."""
        if not successors_map:
            successors_map = {}

        blocked_by_task = []
        all_blocked_tasks = set()
        aircraft_blocked = defaultdict(lambda: {'blocked_count': 0, 'tasks': []})

        for task in incomplete_tasks:
            soi = task.get('soi', '')
            line_number = task.get('line_number', 0)
            task_id = f"{soi}_{line_number}"

            direct_successors = successors_map.get(task_id, [])

            # BFS cascade (max depth 5)
            cascade_count = 0
            visited = set()
            queue = list(direct_successors)
            depth = 0
            while queue and depth < 5:
                next_queue = []
                for succ_id in queue:
                    if succ_id not in visited:
                        visited.add(succ_id)
                        cascade_count += 1
                        all_blocked_tasks.add(succ_id)
                        next_queue.extend(successors_map.get(succ_id, []))
                queue = next_queue
                depth += 1

            blocked_by_task.append({
                'soi': soi,
                'line_number': line_number,
                'task_id': task_id,
                'team': task.get('team', ''),
                'priority': task.get('priority', 0),
                'priority_score': task.get('priority_score', 0),
                'duration': task.get('duration_minutes') or task.get('duration') or 0,
                'direct_successors': len(direct_successors),
                'cascade_blocked': cascade_count,
                'isCritical': task.get('isCritical', False),
                'has_delay_reason': task.get('has_delay_reason', False),
                'delay_reason': task.get('delay_reason')
            })

            if line_number:
                aircraft_blocked[line_number]['blocked_count'] += cascade_count
                aircraft_blocked[line_number]['tasks'].append(soi)

        most_impactful = sorted(blocked_by_task, key=lambda x: x['cascade_blocked'], reverse=True)

        return {
            'total_blocked_successors': len(all_blocked_tasks),
            'total_incomplete_tasks': len(incomplete_tasks),
            'most_impactful_misses': most_impactful[:10],
            'all_incomplete_impact': blocked_by_task,
            'aircraft_impact': {
                str(ln): info for ln, info in sorted(
                    aircraft_blocked.items(),
                    key=lambda x: x[1]['blocked_count'],
                    reverse=True
                )
            },
            'cascade_depth_analyzed': 5
        }

    # ========================================================================
    # COMBINED PERFORMANCE INDEX
    # ========================================================================

    def calculate_performance_index(self, throughput: Dict, alignment: Dict,
                                    impact: Dict) -> Dict:
        """
        Performance Index = Throughput Score × (Alignment Score / 100) × Impact Factor

        This means:
        - 100% throughput + 100% alignment + neutral impact = 100 PI
        - 50% throughput + 100% alignment + neutral impact = 50 PI
        - 100% throughput + 50% alignment + neutral impact = 50 PI
        - 100% throughput + 100% alignment + improved impact = up to 150 PI (capped at 100)
        - 100% throughput + 100% alignment + worsened impact = down to 50 PI
        """
        t_score = throughput.get('throughput_score', 0)
        a_score = alignment.get('alignment_score', 0) / 100.0 if alignment.get('alignment_score', 0) > 0 else 0
        i_factor = impact.get('impact_factor', 1.0)

        pi = t_score * a_score * i_factor
        pi = min(100.0, max(0.0, round(pi, 1)))

        return {
            'performance_index': pi,
            'pi_grade': self.calculate_grade(pi),
            'components': {
                'throughput': round(t_score, 1),
                'alignment': round(a_score * 100, 1),
                'impact_factor': round(i_factor, 2)
            }
        }

    # ========================================================================
    # TEAM PERFORMANCE CALCULATION
    # ========================================================================

    def calculate_team_performance(self, team_name: str, shift_number: int,
                                   before_schedule: Dict, after_schedule: Dict,
                                   today_day: int = None,
                                   delay_reasons: Dict[Tuple, str] = None,
                                   successors_map: Dict = None,
                                   before_products: List[Dict] = None,
                                   after_products: List[Dict] = None,
                                   work_group: str = None) -> Dict:
        """
        Calculate performance for a single team.

        Key improvement: filters to today's planned tasks only (by day number),
        not all remaining tasks across the entire schedule horizon.

        work_group: Optional filter by workGroup (mechanic, quality, customer, vendor).
                    If None, all tasks for this team are included.
        """
        # Get all tasks for this team and shift
        all_team_before = [
            t for t in before_schedule['tasks']
            if t.get('team') == team_name and t.get('shift') == shift_number
        ]

        # Apply workGroup filter if specified
        if work_group:
            all_team_before = [t for t in all_team_before if t.get('workGroup') == work_group]

        # Filter to today's work only if day is specified
        if today_day is not None:
            team_tasks_today = [t for t in all_team_before if t.get('day') == today_day]
        else:
            team_tasks_today = all_team_before

        total_planned = len(team_tasks_today)

        if total_planned == 0:
            return {
                'team': team_name,
                'shift': shift_number,
                'work_group': work_group or 'all',
                'no_tasks_planned': True,
                'total_tasks_planned': 0,
                'tasks_completed': 0,
                'tasks_incomplete': 0,
                'tasks_incomplete_with_reason': 0,
                'tasks_incomplete_no_reason': 0,
                'unscheduled_tasks_completed': 0,
                'throughput': self.calculate_throughput(0, 0, 0),
                'alignment': self.calculate_alignment([], [], []),
                'impact': {'impact_factor': 1.0, 'impact_score': 50.0, 'impact_grade': 'C',
                          'aircraft_deltas': [], 'aircraft_improved': 0,
                          'aircraft_held': 0, 'aircraft_worsened': 0,
                          'net_hours': 0.0, 'avg_hours_per_aircraft': 0.0, 'net_minutes': 0},
                'performance_index': 0.0,
                'pi_grade': 'N/A',
                'pi_components': {'throughput': 0, 'alignment': 0, 'impact_factor': 1.0},
                'schedule_impact': {
                    'total_blocked_successors': 0, 'total_incomplete_tasks': 0,
                    'most_impactful_misses': [], 'all_incomplete_impact': [],
                    'aircraft_impact': {}, 'cascade_depth_analyzed': 5
                }
            }

        # Get after-schedule tasks for comparison.
        # NOTE: Do NOT filter by shift_number here.  The after schedule may be
        # from a different shift (e.g. before=S1, after=S2).  We compare by
        # (soi, line_number) key in compare_schedules, so including all after
        # tasks for this team is correct — tasks still present = incomplete,
        # tasks removed = completed.
        team_tasks_after = [
            t for t in after_schedule['tasks']
            if t.get('team') == team_name
        ]

        # Apply same workGroup filter to after tasks
        if work_group:
            team_tasks_after = [t for t in team_tasks_after if t.get('workGroup') == work_group]

        # Compare: what was completed vs incomplete
        comparison = self.compare_schedules(team_tasks_today, team_tasks_after, delay_reasons)

        tasks_completed = len(comparison['completed'])
        tasks_incomplete = len(comparison['incomplete'])

        # Unscheduled completed: tasks NOT on today's plan that were nonetheless
        # completed this shift (present in before schedule, gone from after).
        # We must compare the FULL before team tasks against the FULL after set,
        # then subtract the planned completions we already counted above.
        # Without this, the entire future backlog gets miscounted as "extra work."
        all_before_for_team = [
            t for t in before_schedule['tasks']
            if t.get('team') == team_name
        ]
        if work_group:
            all_before_for_team = [t for t in all_before_for_team
                                   if t.get('workGroup') == work_group]

        all_before_keys = {(t['soi'], t['line_number']) for t in all_before_for_team}
        all_after_keys = {(t['soi'], t['line_number']) for t in team_tasks_after}
        planned_keys = {(t['soi'], t['line_number']) for t in team_tasks_today}

        # All tasks removed from schedule = all completed this shift
        all_removed = all_before_keys - all_after_keys
        # Unscheduled = removed tasks that were NOT on the plan
        unscheduled_keys = all_removed - planned_keys
        tasks_unscheduled = len(unscheduled_keys)
        # Build the unscheduled task list for alignment scoring
        all_before_map = {(t['soi'], t['line_number']): t for t in all_before_for_team}
        comparison['unscheduled'] = [all_before_map[k] for k in unscheduled_keys
                                     if k in all_before_map]

        incomplete_with_reason = sum(1 for t in comparison['incomplete'] if t.get('has_delay_reason'))
        incomplete_no_reason = sum(1 for t in comparison['incomplete'] if not t.get('has_delay_reason'))

        # Dimension 1: Throughput
        throughput = self.calculate_throughput(tasks_completed, total_planned, tasks_unscheduled)

        # Dimension 2: Alignment
        alignment = self.calculate_alignment(
            comparison['completed'], team_tasks_today, comparison['unscheduled'])

        # Dimension 3: Impact (team's aircraft subset)
        team_lines = set(t.get('line_number') for t in team_tasks_today if t.get('line_number'))
        if before_products and after_products and team_lines:
            team_before_products = [p for p in before_products if p['line_number'] in team_lines]
            team_after_products = [p for p in after_products if p['line_number'] in team_lines]
            impact = self.calculate_impact(team_before_products, team_after_products)
        else:
            impact = {
                'impact_factor': 1.0, 'impact_score': 50.0, 'impact_grade': 'C',
                'aircraft_deltas': [], 'aircraft_improved': 0,
                'aircraft_held': 0, 'aircraft_worsened': 0,
                'net_hours': 0.0, 'avg_hours_per_aircraft': 0.0, 'net_minutes': 0
            }

        # Combined PI
        pi_result = self.calculate_performance_index(throughput, alignment, impact)

        # Schedule impact (downstream blocking from incompletes)
        schedule_impact = self.analyze_schedule_impact(
            comparison['incomplete'], successors_map or {})

        return {
            'team': team_name,
            'shift': shift_number,
            'work_group': work_group or 'all',
            'no_tasks_planned': False,
            'total_tasks_planned': total_planned,
            'total_tasks_all_days': len(all_team_before),
            'tasks_completed': tasks_completed,
            'tasks_incomplete': tasks_incomplete,
            'tasks_incomplete_with_reason': incomplete_with_reason,
            'tasks_incomplete_no_reason': incomplete_no_reason,
            'unscheduled_tasks_completed': tasks_unscheduled,
            'throughput': throughput,
            'alignment': alignment,
            'impact': impact,
            'performance_index': pi_result['performance_index'],
            'pi_grade': pi_result['pi_grade'],
            'pi_components': pi_result['components'],
            'schedule_impact': schedule_impact,
            'task_details': {
                'completed': comparison['completed'],
                'incomplete': comparison['incomplete'],
                'unscheduled': comparison['unscheduled']
            }
        }

    # ========================================================================
    # HIERARCHICAL ROLLUP (weighted by planned workload)
    # ========================================================================

    def _rollup_scores(self, team_scores: List[Dict]) -> Dict:
        """
        Aggregate team scores into a rollup.
        Weighted by planned task count so teams carrying more work
        have proportionally more influence on the rollup score.
        """
        active = [t for t in team_scores if not t.get('no_tasks_planned')]
        if not active:
            return {
                'throughput': self.calculate_throughput(0, 0, 0),
                'alignment': self.calculate_alignment([], [], []),
                'impact': {'impact_factor': 1.0, 'impact_score': 50.0, 'impact_grade': 'C',
                          'aircraft_deltas': [], 'aircraft_improved': 0,
                          'aircraft_held': 0, 'aircraft_worsened': 0,
                          'net_hours': 0.0, 'avg_hours_per_aircraft': 0.0, 'net_minutes': 0},
                'performance_index': 0.0,
                'pi_grade': 'N/A',
                'pi_components': {'throughput': 0, 'alignment': 0, 'impact_factor': 1.0}
            }

        total_planned = sum(t['total_tasks_planned'] for t in active)
        total_completed = sum(t['tasks_completed'] for t in active)
        total_unscheduled = sum(t['unscheduled_tasks_completed'] for t in active)

        # Weighted throughput
        throughput = self.calculate_throughput(total_completed, total_planned, total_unscheduled)

        # Weighted alignment: use planned task count as weight.
        # If nothing was completed, alignment is undefined — force to 0.
        if total_completed == 0 and total_unscheduled == 0:
            weighted_alignment = 0.0
        else:
            weighted_alignment = 0.0
            for t in active:
                weight = t['total_tasks_planned'] / total_planned if total_planned > 0 else 0
                weighted_alignment += t['alignment']['alignment_score'] * weight

        alignment = {
            'alignment_score': round(weighted_alignment, 1),
            'alignment_grade': self.calculate_grade(weighted_alignment) if weighted_alignment > 0 else 'N/A',
            'schedule_value_earned': sum(t['alignment'].get('schedule_value_earned', 0) for t in active),
            'schedule_value_planned': sum(t['alignment'].get('schedule_value_planned', 0) for t in active),
            'critical_path_completed': sum(t['alignment'].get('critical_path_completed', 0) for t in active),
            'critical_path_total': sum(t['alignment'].get('critical_path_total', 0) for t in active),
        }
        cp_total = alignment['critical_path_total']
        alignment['critical_path_pct'] = round(
            (alignment['critical_path_completed'] / cp_total * 100) if cp_total > 0 else 0, 1)

        # Combined impact from all teams' aircraft
        all_deltas = []
        for t in active:
            all_deltas.extend(t.get('impact', {}).get('aircraft_deltas', []))

        # Deduplicate aircraft deltas (same aircraft from different teams)
        seen_lines = set()
        unique_deltas = []
        for d in all_deltas:
            ln = d['line_number']
            if ln not in seen_lines:
                seen_lines.add(ln)
                unique_deltas.append(d)

        # Straight sum of (after - before) per aircraft.
        # Positive = schedule pushed later (delayed), negative = pulled earlier (improved).
        net_minutes = sum(d.get('schedule_delta', 0) for d in unique_deltas)
        net_hours = net_minutes / 60.0

        improved = sum(1 for d in unique_deltas if d.get('schedule_delta', 0) < -5)
        held_count = sum(1 for d in unique_deltas if -5 <= d.get('schedule_delta', 0) <= 5)
        worsened_count = sum(1 for d in unique_deltas if d.get('schedule_delta', 0) > 5)
        aircraft_count = len(unique_deltas)
        avg_hours_per_aircraft = round(net_hours / aircraft_count, 1) if aircraft_count > 0 else 0.0

        net_hours_benefit = -net_hours  # positive = improved, negative = worsened
        if net_hours_benefit > 0:
            impact_factor = min(1.5, 1.0 + (net_hours_benefit * 0.003))
        elif net_hours_benefit < 0:
            impact_factor = max(0.5, 1.0 - (abs(net_hours_benefit) * 0.007))
        else:
            impact_factor = 1.0

        impact_score = min(100.0, max(0.0, 50.0 + (net_hours_benefit * 0.65)))

        impact = {
            'impact_factor': round(impact_factor, 2),
            'impact_score': round(impact_score, 1),
            'impact_grade': self.calculate_grade(impact_score),
            'aircraft_deltas': sorted(unique_deltas, key=lambda d: d.get('schedule_delta', 0),
                                      reverse=True),
            'aircraft_improved': improved,
            'aircraft_held': held_count,
            'aircraft_worsened': worsened_count,
            'net_hours': round(net_hours, 1),
            'avg_hours_per_aircraft': avg_hours_per_aircraft,
            'net_minutes': net_minutes,
            'total_cp_reduction': sum(d.get('cp_reduction', 0) for d in unique_deltas)
        }

        # Combined PI
        pi_result = self.calculate_performance_index(throughput, alignment, impact)

        return {
            'throughput': throughput,
            'alignment': alignment,
            'impact': impact,
            'performance_index': pi_result['performance_index'],
            'pi_grade': pi_result['pi_grade'],
            'pi_components': pi_result['components']
        }

    def _rollup_impact_analysis(self, team_scores: List[Dict]) -> Dict:
        """Aggregate schedule impact analysis across teams."""
        total_blocked = 0
        total_incomplete = 0
        all_impact = []
        aircraft_impact = defaultdict(lambda: {'blocked_count': 0, 'tasks': []})

        for ts in team_scores:
            if ts.get('no_tasks_planned'):
                continue
            si = ts.get('schedule_impact', {})
            total_blocked += si.get('total_blocked_successors', 0)
            total_incomplete += si.get('total_incomplete_tasks', 0)
            all_impact.extend(si.get('all_incomplete_impact', []))

            for ln, info in si.get('aircraft_impact', {}).items():
                aircraft_impact[ln]['blocked_count'] += info['blocked_count']
                aircraft_impact[ln]['tasks'].extend(info['tasks'])

        most_impactful = sorted(all_impact, key=lambda x: x.get('cascade_blocked', 0), reverse=True)

        return {
            'total_blocked_successors': total_blocked,
            'total_incomplete_tasks': total_incomplete,
            'most_impactful_misses': most_impactful[:10],
            'all_incomplete_impact': all_impact,
            'aircraft_impact': dict(aircraft_impact),
            'cascade_depth_analyzed': 5
        }

    # ========================================================================
    # MAIN ENTRY POINT
    # ========================================================================

    def get_team_to_superintendent_mapping(self, before_schedule: Dict) -> Dict[str, str]:
        """Extract team to superintendent mapping from schedule data"""
        team_to_super = {}
        for task in before_schedule['tasks']:
            team = task.get('team')
            superintendent = task.get('superintendent')
            if team and superintendent and team not in team_to_super:
                team_to_super[team] = superintendent
        return team_to_super

    def _detect_aircraft_changes(self, before_schedule: Dict, after_schedule: Dict) -> Dict:
        """
        Detect which aircraft line numbers were added or removed between schedules.

        An aircraft removed from the dataset typically means all remaining tasks
        are complete ("Line 1265 complete!").  A new aircraft means the next
        soonest delivery was pulled into the focus set ("NEW! Line 1266").

        Returns:
            {
                'completed_aircraft': [{'line_number': 1265, 'task_count': 42}],
                'new_aircraft': [{'line_number': 1266, 'task_count': 38}],
                'common_aircraft': [1263, 1264, ...],
                'has_changes': True/False
            }
        """
        # Get line numbers from embedded metadata first, fall back to task scan
        before_lines = set(before_schedule.get('aircraft_line_numbers', []))
        after_lines = set(after_schedule.get('aircraft_line_numbers', []))

        # If metadata not available, scan tasks
        if not before_lines:
            before_lines = set(t.get('line_number') for t in before_schedule.get('tasks', [])
                               if t.get('line_number') is not None)
        if not after_lines:
            after_lines = set(t.get('line_number') for t in after_schedule.get('tasks', [])
                              if t.get('line_number') is not None)

        completed = before_lines - after_lines  # In before but not after = completed
        new = after_lines - before_lines        # In after but not before = newly added
        common = before_lines & after_lines

        # Count tasks per removed/added aircraft for context
        completed_aircraft = []
        for ln in sorted(completed):
            task_count = sum(1 for t in before_schedule.get('tasks', [])
                            if t.get('line_number') == ln)
            completed_aircraft.append({'line_number': ln, 'task_count': task_count})

        new_aircraft = []
        for ln in sorted(new):
            task_count = sum(1 for t in after_schedule.get('tasks', [])
                            if t.get('line_number') == ln)
            new_aircraft.append({'line_number': ln, 'task_count': task_count})

        return {
            'completed_aircraft': completed_aircraft,
            'new_aircraft': new_aircraft,
            'common_aircraft': sorted(common),
            'has_changes': bool(completed or new)
        }

    def _build_schedule_overview(self, before_schedule: Dict, after_schedule: Dict,
                                evaluated_shift: int, today_day: int) -> Dict:
        """
        Build a simple task-count comparison between before and after schedules.

        Returns total tasks, per-shift breakdown, and deltas so the dashboard can
        show at a glance how many tasks were removed (completed) and how the
        workload shifted between shifts.
        """
        before_tasks = before_schedule.get('tasks', [])
        after_tasks = after_schedule.get('tasks', [])

        # Count by shift number
        def count_by_shift(tasks):
            counts = defaultdict(int)
            for t in tasks:
                s = t.get('shift')
                if s is not None:
                    counts[s] += 1
            return dict(counts)

        before_by_shift = count_by_shift(before_tasks)
        after_by_shift = count_by_shift(after_tasks)

        # All shift numbers present in either schedule
        all_shifts = sorted(set(list(before_by_shift.keys()) + list(after_by_shift.keys())))

        shift_breakdown = []
        for s in all_shifts:
            b = before_by_shift.get(s, 0)
            a = after_by_shift.get(s, 0)
            shift_breakdown.append({
                'shift': s,
                'before': b,
                'after': a,
                'delta': a - b,
                'is_evaluated': s == evaluated_shift
            })

        before_total = len(before_tasks)
        after_total = len(after_tasks)
        tasks_removed = before_total - after_total

        # Also count tasks for today_day on the evaluated shift specifically
        before_today = 0
        after_today = 0
        if today_day is not None:
            before_today = sum(1 for t in before_tasks
                               if t.get('shift') == evaluated_shift
                               and t.get('day') == today_day)
            after_today = sum(1 for t in after_tasks
                              if t.get('shift') == evaluated_shift
                              and t.get('day') == today_day)

        # Unique (soi, line_number) comparison for true completed count
        before_keys = {(t['soi'], t['line_number']) for t in before_tasks}
        after_keys = {(t['soi'], t['line_number']) for t in after_tasks}
        truly_completed = len(before_keys - after_keys)
        newly_added = len(after_keys - before_keys)

        return {
            'before_total': before_total,
            'after_total': after_total,
            'tasks_removed': tasks_removed,
            'truly_completed': truly_completed,
            'newly_added': newly_added,
            'evaluated_shift': evaluated_shift,
            'today_day': today_day,
            'before_today_shift': before_today,
            'after_today_shift': after_today,
            'today_shift_delta': after_today - before_today,
            'shift_breakdown': shift_breakdown,
        }

    def _build_hierarchy(self, team_scores: List[Dict], shift_number: int,
                         shift_date: date, before_schedule: Dict,
                         before_products: List[Dict], after_products: List[Dict],
                         work_group_label: str = 'all') -> Dict:
        """
        Build superintendent rollups, factory rollup, grade distribution, and delay breakdown
        from a list of team scores. Shared logic used for both 'all' and per-workGroup views.
        """
        team_to_super = self.get_team_to_superintendent_mapping(before_schedule)
        for ts in team_scores:
            ts['superintendent'] = team_to_super.get(ts['team'], 'Unknown')

        # Roll up to superintendent level
        super_teams = defaultdict(list)
        for ts in team_scores:
            if not ts.get('no_tasks_planned'):
                super_teams[ts['superintendent']].append(ts)

        superintendent_scores = []
        for super_name, super_team_list in sorted(super_teams.items()):
            rollup = self._rollup_scores(super_team_list)
            schedule_impact = self._rollup_impact_analysis(super_team_list)

            superintendent_scores.append({
                'superintendent': super_name,
                'shift': shift_number,
                'work_group': work_group_label,
                'total_tasks_planned': sum(t['total_tasks_planned'] for t in super_team_list),
                'tasks_completed': sum(t['tasks_completed'] for t in super_team_list),
                'tasks_incomplete': sum(t['tasks_incomplete'] for t in super_team_list),
                'tasks_incomplete_with_reason': sum(t['tasks_incomplete_with_reason'] for t in super_team_list),
                'tasks_incomplete_no_reason': sum(t['tasks_incomplete_no_reason'] for t in super_team_list),
                'unscheduled_tasks_completed': sum(t['unscheduled_tasks_completed'] for t in super_team_list),
                'team_count': len(super_team_list),
                **rollup,
                'schedule_impact': schedule_impact,
                'teams': [t['team'] for t in super_team_list]
            })

        # Roll up to factory level
        active_teams = [t for t in team_scores if not t.get('no_tasks_planned')]
        factory_rollup = self._rollup_scores(active_teams)
        factory_schedule_impact = self._rollup_impact_analysis(active_teams)

        # Factory-level impact uses ALL products (not filtered by team)
        factory_impact = self.calculate_impact(before_products, after_products)

        # Recalculate PI with factory-level impact
        factory_pi = self.calculate_performance_index(
            factory_rollup['throughput'], factory_rollup['alignment'], factory_impact)

        today_day = None
        all_days = sorted(set(t.get('day', 0) for t in before_schedule['tasks']
                             if t.get('shift') == shift_number))
        if all_days:
            today_day = all_days[0]

        factory_score = {
            'shift': shift_number,
            'shift_date': shift_date.isoformat(),
            'today_day': today_day,
            'work_group': work_group_label,
            'total_tasks_planned': sum(t['total_tasks_planned'] for t in active_teams),
            'tasks_completed': sum(t['tasks_completed'] for t in active_teams),
            'tasks_incomplete': sum(t['tasks_incomplete'] for t in active_teams),
            'tasks_incomplete_with_reason': sum(t['tasks_incomplete_with_reason'] for t in active_teams),
            'tasks_incomplete_no_reason': sum(t['tasks_incomplete_no_reason'] for t in active_teams),
            'unscheduled_tasks_completed': sum(t['unscheduled_tasks_completed'] for t in active_teams),
            'superintendent_count': len(superintendent_scores),
            'team_count': len(active_teams),
            'throughput': factory_rollup['throughput'],
            'alignment': factory_rollup['alignment'],
            'impact': factory_impact,
            'performance_index': factory_pi['performance_index'],
            'pi_grade': factory_pi['pi_grade'],
            'pi_components': factory_pi['components'],
            'schedule_impact': factory_schedule_impact
        }

        # Team grade distribution
        team_grades = defaultdict(int)
        for ts in active_teams:
            grade = ts.get('pi_grade', 'F')
            if grade.startswith('A'):
                team_grades['A'] += 1
            elif grade.startswith('B'):
                team_grades['B'] += 1
            elif grade.startswith('C'):
                team_grades['C'] += 1
            elif grade.startswith('D'):
                team_grades['D'] += 1
            else:
                team_grades['F'] += 1

        # Delay reason breakdown
        delay_breakdown = defaultdict(int)
        for ts in active_teams:
            for task in ts.get('task_details', {}).get('incomplete', []):
                reason = task.get('delay_reason')
                if reason:
                    delay_breakdown[reason] += 1

        return {
            'teams': team_scores,
            'superintendents': superintendent_scores,
            'factory': factory_score,
            'team_grade_distribution': dict(team_grades),
            'delay_reason_breakdown': dict(delay_breakdown)
        }

    def calculate_shift_performance(self, before_schedule_path: str,
                                   after_schedule_path: str,
                                   shift_date: date, shift_number: int,
                                   delay_reasons: Dict[Tuple, str] = None) -> Dict:
        """
        Calculate complete shift performance for all teams, superintendents, and factory.

        Returns performance data with per-workGroup breakdowns:
        - 'by_resource_type': { 'mechanic': {...}, 'quality': {...}, ... }
        - Top-level 'teams', 'superintendents', 'factory' default to Production (mechanic) view
        - 'all' view is also available under by_resource_type['all']

        Key improvements:
        1. Filters to today's tasks (earliest day in schedule), not entire horizon
        2. Uses priority_score for alignment instead of broken rank-based GPA
        3. Tracks lateness delta as impact metric
        4. Weights rollups by planned workload
        5. Caps completion rate at 100%
        6. Separates performance by resource type (workGroup)
        """
        before_schedule = self.load_schedule(before_schedule_path)
        after_schedule = self.load_schedule(after_schedule_path)

        # ====================================================================
        # Detect aircraft set changes between before and after schedules
        # ====================================================================
        aircraft_changes = self._detect_aircraft_changes(before_schedule, after_schedule)

        # Extract products (aircraft delivery projections) for impact analysis
        before_products = before_schedule.get('products', [])
        after_products = after_schedule.get('products', [])

        # Extract successors maps for blocking + critical-path analysis
        successors_map = before_schedule.get('successors_map', {})
        after_successors_map = after_schedule.get('successors_map', {})

        # Enrich products with DAG critical-path duration (optimizer-independent)
        self._enrich_products_with_cp(
            before_products, before_schedule['tasks'], successors_map)
        self._enrich_products_with_cp(
            after_products, after_schedule['tasks'], after_successors_map)

        # Determine "today" - the earliest day in the before schedule
        all_days = sorted(set(t.get('day', 0) for t in before_schedule['tasks']
                             if t.get('shift') == shift_number))
        today_day = all_days[0] if all_days else None

        # ====================================================================
        # Calculate per-workGroup performance
        # ====================================================================
        by_resource_type = {}

        for wg in WORK_GROUPS + ['all']:
            wg_filter = wg if wg != 'all' else None

            # Get unique teams for this shift, day, and workGroup
            if today_day is not None:
                teams = set(
                    t.get('team') for t in before_schedule['tasks']
                    if t.get('shift') == shift_number and t.get('team')
                    and t.get('day') == today_day
                    and (wg_filter is None or t.get('workGroup') == wg_filter)
                )
            else:
                teams = set(
                    t.get('team') for t in before_schedule['tasks']
                    if t.get('shift') == shift_number and t.get('team')
                    and (wg_filter is None or t.get('workGroup') == wg_filter)
                )

            # Calculate team performance for this workGroup
            team_scores = []
            for team_name in sorted(teams):
                team_perf = self.calculate_team_performance(
                    team_name, shift_number, before_schedule, after_schedule,
                    today_day=today_day,
                    delay_reasons=delay_reasons,
                    successors_map=successors_map,
                    before_products=before_products,
                    after_products=after_products,
                    work_group=wg_filter
                )
                team_scores.append(team_perf)

            hierarchy = self._build_hierarchy(
                team_scores, shift_number, shift_date, before_schedule,
                before_products, after_products, work_group_label=wg
            )
            by_resource_type[wg] = hierarchy

        # ====================================================================
        # Primary view defaults to Production (mechanic)
        # ====================================================================
        primary = by_resource_type.get('mechanic', by_resource_type.get('all', {}))

        # Count tasks per workGroup for the summary
        wg_summary = {}
        for wg in WORK_GROUPS:
            wg_data = by_resource_type.get(wg, {})
            wg_factory = wg_data.get('factory', {})
            wg_summary[wg] = {
                'label': WORK_GROUP_LABELS.get(wg, wg),
                'total_tasks_planned': wg_factory.get('total_tasks_planned', 0),
                'tasks_completed': wg_factory.get('tasks_completed', 0),
                'performance_index': wg_factory.get('performance_index', 0),
                'pi_grade': wg_factory.get('pi_grade', 'N/A')
            }

        # ====================================================================
        # Schedule Overview: total task counts by shift for before vs after
        # ====================================================================
        schedule_overview = self._build_schedule_overview(
            before_schedule, after_schedule, shift_number, today_day)

        return {
            'shift_date': shift_date.isoformat(),
            'shift_number': shift_number,
            'today_day': today_day,
            'before_schedule_file': Path(before_schedule_path).name,
            'after_schedule_file': Path(after_schedule_path).name,
            'active_work_group': 'mechanic',
            'work_group_labels': WORK_GROUP_LABELS,
            'work_group_summary': wg_summary,
            # Primary view = mechanic (Production)
            'teams': primary.get('teams', []),
            'superintendents': primary.get('superintendents', []),
            'factory': primary.get('factory', {}),
            'team_grade_distribution': primary.get('team_grade_distribution', {}),
            'delay_reason_breakdown': primary.get('delay_reason_breakdown', {}),
            # Full per-resource-type breakdown
            'by_resource_type': by_resource_type,
            # Aircraft set changes between schedules
            'aircraft_changes': aircraft_changes,
            # Schedule overview: task counts by shift, before vs after
            'schedule_overview': schedule_overview,
        }
