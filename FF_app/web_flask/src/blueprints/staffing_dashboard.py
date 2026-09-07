"""
Staffing Dashboard API Blueprint

Handles API endpoints for staffing dashboard with peak demand and utilization metrics.
"""

from flask import Blueprint, jsonify, request, current_app
from datetime import datetime, date, timedelta
from pathlib import Path
from collections import defaultdict
import sys

staffing_dashboard_bp = Blueprint('staffing_dashboard', __name__, url_prefix='/api/staffing-dashboard')


def get_role_from_task(task):
    """
    Determine role (mechanic, quality, customer) from task data.
    Uses fields already present on the exported task JSON.
    """
    if task.get('isCustomerTask'):
        return 'customer'
    if task.get('isQualityTask') or task.get('is_inspection'):
        return 'quality'
    team = task.get('team') or task.get('resource_team') or ''
    if team.upper().startswith('QA-'):
        return 'quality'
    return 'mechanic'


def _get_schedule_dict():
    """Return the schedule as a task-id-keyed dict, preferring production mode."""
    current_schedule_data = getattr(current_app, 'current_schedule_data', None)
    if current_schedule_data:
        tasks = current_schedule_data.get('tasks', [])
        return {f"task_{i}": task for i, task in enumerate(tasks)}
    final_schedule = getattr(current_app, 'final_schedule', None)
    if final_schedule:
        return final_schedule
    return None


def _get_reference_date():
    """Extract reference_date string from the loaded schedule metadata."""
    csd = getattr(current_app, 'current_schedule_data', None)
    if csd:
        return (csd.get('metadata', {}).get('reference_date')
                or csd.get('summary', {}).get('reference_date'))
    return None


def calculate_peak_demand_and_utilization(schedule_dict, filters):
    """
    Calculate peak demand headcount and utilization per day with filtering.

    Works directly on the exported task JSON fields — no loader required.
    """
    filter_supers = set(filters.get('superintendents', []))
    filter_shifts = set(int(s) for s in filters.get('shifts', []))  # normalise to int
    filter_teams = set(filters.get('teams', []))
    filter_roles = set(filters.get('roles', []))

    shift_capacity_map = {1: 480, 2: 480, 3: 390}  # wall clock − lunch (utilization denominator)

    def _is_qa_team(team_name):
        return team_name.upper().startswith('QA-')

    # Single pass: collect per-day stats
    mechanics_by_day = defaultdict(set)          # day -> {(team, mechanic_id, shift)}
    work_minutes_by_day = defaultdict(float)     # day -> total work (mechanic only)
    mechanic_workers_by_day = defaultdict(set)   # day -> {(team, mechanic_id, shift)} mechanic only
    team_mechanics_by_day = defaultdict(lambda: defaultdict(set))  # day -> team -> {(mid, shift)}

    for _tid, task in schedule_dict.items():
        team = task.get('team') or task.get('resource_team') or task.get('mechanic_team')
        mechanic_id = task.get('mechanic_id')
        shift = task.get('shift')

        if not team or mechanic_id is None or not shift:
            continue

        superintendent = task.get('superintendent', 'UNKNOWN')
        role = get_role_from_task(task)
        duration = task.get('duration') or task.get('duration_minutes') or 60

        # Apply filters
        if filter_supers and superintendent not in filter_supers:
            continue
        if filter_shifts and int(shift) not in filter_shifts:
            continue
        if filter_teams and team not in filter_teams:
            continue
        if filter_roles and role not in filter_roles:
            continue

        day = task.get('day')
        if day is None:
            start_time = task.get('start_time') or task.get('start_minute')
            if start_time is not None:
                day = int(start_time // 1440)
            else:
                continue

        # All teams contribute to peak demand
        mechanics_by_day[day].add((team, mechanic_id, shift))
        team_mechanics_by_day[day][team].add((mechanic_id, shift))

        # Only mechanic teams contribute to utilization
        if not _is_qa_team(team):
            work_minutes_by_day[day] += duration
            mechanic_workers_by_day[day].add((team, mechanic_id, shift))

    # Build per-day results
    peak_demand_by_day = {}
    utilization_by_day = {}

    for day in sorted(mechanics_by_day.keys()):
        peak_demand = len(mechanics_by_day[day])

        team_counts = {
            team: len(mechs) for team, mechs in team_mechanics_by_day[day].items()
        }

        peak_demand_by_day[day] = {
            'total': peak_demand,
            'by_team': team_counts
        }

        # Utilization based on mechanic teams only (QA has unlimited capacity)
        day_capacity = sum(
            shift_capacity_map.get(shift, 480)
            for _team, _mid, shift in mechanic_workers_by_day[day]
        )

        work_minutes = work_minutes_by_day[day]
        utilization_pct = (work_minutes / day_capacity * 100) if day_capacity > 0 else 0

        utilization_by_day[day] = {
            'utilization_pct': round(utilization_pct, 2),
            'work_minutes': round(work_minutes, 2),
            'capacity_minutes': round(day_capacity, 2),
            'peak_demand': peak_demand
        }

    # Summary
    summary = {
        'total_days': len(peak_demand_by_day),
        'avg_peak_demand': round(
            sum(d['total'] for d in peak_demand_by_day.values()) / len(peak_demand_by_day), 2
        ) if peak_demand_by_day else 0,
        'max_peak_demand': max(
            (d['total'] for d in peak_demand_by_day.values()), default=0
        ),
        'avg_utilization': round(
            sum(d['utilization_pct'] for d in utilization_by_day.values()) / len(utilization_by_day), 2
        ) if utilization_by_day else 0,
        'total_work_hours': round(sum(work_minutes_by_day.values()) / 60, 2)
    }

    # Per-team utilization
    utilization_by_team = {}
    team_work = defaultdict(float)
    team_cap = defaultdict(float)

    for day in peak_demand_by_day:
        day_data = peak_demand_by_day[day]
        day_util = utilization_by_day.get(day, {})
        for team, count in day_data.get('by_team', {}).items():
            if day_data['total'] > 0:
                proportion = count / day_data['total']
                team_work[team] += day_util.get('work_minutes', 0) * proportion
                team_cap[team] += day_util.get('capacity_minutes', 0) * proportion

    for team in team_work:
        utilization_by_team[team] = round(
            (team_work[team] / team_cap[team]) * 100, 1
        ) if team_cap[team] > 0 else 0

    return {
        'peak_demand_by_day': {str(k): v for k, v in peak_demand_by_day.items()},
        'utilization_by_day': {str(k): v for k, v in utilization_by_day.items()},
        'utilization_by_team': utilization_by_team,
        'summary': summary
    }


# ============================================================================
# STAFFING DASHBOARD API ENDPOINTS
# ============================================================================

@staffing_dashboard_bp.route('/filter-options', methods=['GET'])
def get_filter_options():
    """Get all available filter options from the loaded schedule — no loader needed."""
    try:
        schedule_dict = _get_schedule_dict()
        if not schedule_dict:
            return jsonify({'error': 'No schedule data available'}), 500

        superintendents = set()
        teams = set()
        shifts = set()
        roles = set()

        for task in schedule_dict.values():
            team = task.get('team') or task.get('resource_team') or task.get('mechanic_team')
            if team:
                teams.add(team)

            sup = task.get('superintendent')
            if sup and sup != 'UNKNOWN':
                superintendents.add(sup)

            if task.get('shift'):
                shifts.add(task['shift'])

            roles.add(get_role_from_task(task))

        return jsonify({
            'superintendents': sorted(superintendents),
            'teams': sorted(teams),
            'shifts': sorted(shifts),
            'roles': sorted(roles)
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@staffing_dashboard_bp.route('/peak-demand-utilization', methods=['POST'])
def get_peak_demand_utilization():
    """Get peak demand and utilization data with filtering."""
    try:
        data = request.get_json() or {}
        filters = {
            'superintendents': data.get('superintendents', []),
            'shifts': data.get('shifts', []),
            'teams': data.get('teams', []),
            'roles': data.get('roles', [])
        }

        schedule_dict = _get_schedule_dict()
        if not schedule_dict:
            return jsonify({'error': 'No schedule data available'}), 500

        result = calculate_peak_demand_and_utilization(schedule_dict, filters)

        ref = _get_reference_date()
        if ref:
            result['reference_date'] = ref

        return jsonify(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@staffing_dashboard_bp.route('/all-soi-metadata', methods=['GET'])
def get_all_soi_metadata():
    """
    Get all superintendents, teams, and skills from the loaded schedule.
    Falls back to ALL SOI CSV only if a loader is already available.
    """
    try:
        # Try schedule-based metadata first (no loader needed)
        schedule_dict = _get_schedule_dict()
        if schedule_dict:
            superintendents = set()
            teams = set()
            skills = set()

            for task in schedule_dict.values():
                team = task.get('team') or task.get('resource_team') or ''
                if team:
                    teams.add(team)
                sup = task.get('superintendent')
                if sup and sup != 'UNKNOWN':
                    superintendents.add(sup)
                skill = task.get('skill') or task.get('assigned_skill')
                if skill:
                    skills.add(skill)

            return jsonify({
                'superintendents': sorted(superintendents),
                'teams': sorted(teams),
                'skills': sorted(skills)
            })

        # Fallback: use loader if available (dev mode)
        loader = getattr(current_app, 'loader', None)
        if loader and hasattr(loader, 'all_soi'):
            all_superintendents = sorted(
                loader.all_soi['SUPERINTENDENT'].dropna().unique().tolist()
            )
            all_teams = sorted(
                loader.all_soi['TEAM'].dropna().unique().tolist()
            )
            all_skills = set()
            if hasattr(loader, 'skill_map') and loader.skill_map:
                all_skills = set(loader.skill_map.values())
            if hasattr(loader, 'staffing') and loader.staffing is not None:
                if 'Skill' in loader.staffing.columns:
                    all_skills.update(loader.staffing['Skill'].dropna().unique())

            return jsonify({
                'superintendents': all_superintendents,
                'teams': all_teams,
                'skills': sorted(all_skills)
            })

        return jsonify({'error': 'No schedule or loader available'}), 500
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
