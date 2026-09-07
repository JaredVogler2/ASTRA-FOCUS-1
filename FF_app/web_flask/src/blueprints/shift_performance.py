"""
Shift Performance API Blueprint

Handles API endpoints for "Do What's Prioritized" dashboard.
"""

from flask import Blueprint, jsonify, request, current_app
from datetime import datetime, date, timedelta
from pathlib import Path
import sys

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from database.db_manager import DatabaseManager
from scheduler.shift_performance import ShiftPerformanceEngine
from src.paths import SCHEDULES_DIR, MAX_ROOT


shift_performance_bp = Blueprint('shift_performance', __name__, url_prefix='/api/shift-performance')


def _parse_date(date_str):
    """Parse date string accepting both YYYYMMDD and YYYY-MM-DD formats."""
    cleaned = date_str.replace('-', '')
    if len(cleaned) == 8 and cleaned.isdigit():
        return datetime.strptime(cleaned, '%Y%m%d').date()
    return datetime.strptime(date_str, '%Y-%m-%d').date()

# Initialize database and performance engine
db_manager = DatabaseManager()
perf_engine = ShiftPerformanceEngine()


# ============================================================================
# DELAY REASON API ENDPOINTS
# ============================================================================

@shift_performance_bp.route('/delay-reasons/categories', methods=['GET'])
def get_delay_reason_categories():
    """Get list of delay reason categories"""
    try:
        categories = db_manager.get_delay_reason_categories(active_only=True)
        return jsonify({
            'categories': categories,
            'total': len(categories)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@shift_performance_bp.route('/delay-reasons', methods=['POST'])
def add_delay_reason():
    """
    Add or update a delay reason for a task

    IMPORTANT: Predecessors are ALWAYS on the same aircraft (line_number) as the delayed task.
    The line_number field applies to both the delayed task AND all its predecessors.

    Request body:
    {
        "task_soi": "string" (REQUIRED),
        "line_number": int (REQUIRED - aircraft line number, inherited by all predecessors),
        "shift_date": "YYYY-MM-DD" (REQUIRED),
        "shift_number": int (REQUIRED - 1, 2, or 3),
        "team": "string" (REQUIRED),
        "delay_reason": "string" (REQUIRED - delay category code),
        "entered_by": "string" (REQUIRED - who entered this delay),
        "notes": "string" (optional - general notes),
        "held_predecessors": [  // Required if delay_reason is HELD_PREDECESSOR
            {
                "predecessor_task_soi": "string" (optional - mechanic may not know task ID),
                "notes": "string" (REQUIRED - what are they waiting for)
            }
        ]
    }
    """
    try:
        data = request.get_json()

        # Validate required fields
        required_fields = ['task_soi', 'line_number', 'shift_date', 'shift_number', 'team', 'delay_reason', 'entered_by']
        for field in required_fields:
            if field not in data:
                return jsonify({'error': f'Missing required field: {field}'}), 400

        # Validate shift_number
        if data['shift_number'] not in [1, 2, 3]:
            return jsonify({'error': 'shift_number must be 1, 2, or 3'}), 400

        # If delay_reason is HELD_PREDECESSOR, validate held_predecessors
        held_predecessors = None
        if data['delay_reason'] == 'HELD_PREDECESSOR':
            held_predecessors = data.get('held_predecessors', [])
            if not held_predecessors:
                return jsonify({'error': 'held_predecessors required for HELD_PREDECESSOR delay reason'}), 400

            # Validate each predecessor entry has notes
            for pred in held_predecessors:
                if not pred.get('notes'):
                    return jsonify({'error': 'Each predecessor entry must have notes'}), 400

        # Parse date (accepts YYYYMMDD and YYYY-MM-DD)
        shift_date = _parse_date(data['shift_date'])

        # Add to database
        delay_id = db_manager.add_delay_reason(
            task_soi=data['task_soi'],
            line_number=data['line_number'],
            shift_date=shift_date,
            shift_number=data['shift_number'],
            team=data['team'],
            delay_reason=data['delay_reason'],
            notes=data.get('notes'),
            entered_by=data['entered_by'],  # Now required
            held_predecessors=held_predecessors
        )

        return jsonify({
            'success': True,
            'id': delay_id,
            'message': 'Delay reason recorded'
        })

    except ValueError as e:
        return jsonify({'error': f'Invalid date format: {str(e)}'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@shift_performance_bp.route('/delay-reasons/<shift_date>/<int:shift_number>', methods=['GET'])
def get_shift_delay_reasons(shift_date, shift_number):
    """
    Get all delay reasons for a shift

    Query params:
        team: Optional team filter
    """
    try:
        # Parse date
        shift_date_obj = _parse_date(shift_date)

        # Get team filter if provided
        team = request.args.get('team')

        # Fetch from database
        delay_reasons = db_manager.get_shift_delay_reasons(
            shift_date=shift_date_obj,
            shift_number=shift_number,
            team=team
        )

        return jsonify({
            'shift_date': shift_date,
            'shift_number': shift_number,
            'team': team,
            'delay_reasons': delay_reasons,
            'total': len(delay_reasons)
        })

    except ValueError as e:
        return jsonify({'error': f'Invalid date format: {str(e)}'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@shift_performance_bp.route('/delay-reasons/task/<task_soi>/<int:line_number>/<shift_date>/<int:shift_number>', methods=['GET'])
def get_task_delay_reason(task_soi, line_number, shift_date, shift_number):
    """Get delay reason for a specific task"""
    try:
        # Parse date
        shift_date_obj = _parse_date(shift_date)

        # Fetch from database
        delay_reason = db_manager.get_delay_reason(
            task_soi=task_soi,
            line_number=line_number,
            shift_date=shift_date_obj,
            shift_number=shift_number
        )

        if not delay_reason:
            return jsonify({'error': 'Delay reason not found'}), 404

        return jsonify(delay_reason)

    except ValueError as e:
        return jsonify({'error': f'Invalid date format: {str(e)}'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============================================================================
# SHIFT PERFORMANCE CALCULATION API ENDPOINTS
# ============================================================================

@shift_performance_bp.route('/calculate/<shift_date>/<int:shift_number>', methods=['POST'])
def calculate_shift_performance(shift_date, shift_number):
    """
    Calculate shift performance by comparing schedules

    Shift comparison logic:
    - Shift 1: Compare shift1 (before/plan) vs shift2 (after/actual) for SAME date
    - Shift 2: Compare shift2 (before/plan) vs shift3 (after/actual) for SAME date
    - Shift 3: Compare shift3 (before/plan PRIOR day) vs shift1 (after/actual CURRENT day)
               Shift 3 performance is calculated when shift 1 plan runs at start of new day

    Request body:
    {
        "before_schedule_file": "focus_group1_20251107_shift1.json",
        "after_schedule_file": "focus_group1_20251107_shift2.json"
    }
    """
    try:
        data = request.get_json()

        # Validate required fields
        if 'before_schedule_file' not in data or 'after_schedule_file' not in data:
            return jsonify({'error': 'Missing schedule file names'}), 400

        # Parse date
        shift_date_obj = _parse_date(shift_date)

        # Build file paths - Fixed: use correct directory
        import os
        schedules_dir = SCHEDULES_DIR
        before_path = schedules_dir / data['before_schedule_file']
        after_path = schedules_dir / data['after_schedule_file']

        # Validate files exist
        if not before_path.exists():
            return jsonify({'error': f'Before schedule not found: {data["before_schedule_file"]} (searched in {schedules_dir})'}), 404
        if not after_path.exists():
            return jsonify({'error': f'After schedule not found: {data["after_schedule_file"]} (searched in {schedules_dir})'}), 404

        # Get delay reasons from database
        delay_reasons_list = db_manager.get_shift_delay_reasons(
            shift_date=shift_date_obj,
            shift_number=shift_number
        )

        # Convert to dict keyed by (task_soi, line_number)
        delay_reasons = {
            (dr['task_soi'], dr['line_number']): dr['delay_reason']
            for dr in delay_reasons_list
        }

        # Calculate performance
        performance = perf_engine.calculate_shift_performance(
            before_schedule_path=str(before_path),
            after_schedule_path=str(after_path),
            shift_date=shift_date_obj,
            shift_number=shift_number,
            delay_reasons=delay_reasons
        )

        # Save to database - extract PI as the score_percentage, pi_grade as grade
        factory = performance['factory']
        db_manager.save_performance_score(
            shift_date=shift_date_obj,
            shift_number=shift_number,
            level='factory',
            entity_name='Factory',
            planned_points=factory.get('total_tasks_planned', 0),
            earned_points=factory.get('tasks_completed', 0),
            score_percentage=factory.get('performance_index', 0),
            grade=factory.get('pi_grade', 'F'),
            total_tasks_planned=factory.get('total_tasks_planned', 0),
            tasks_completed=factory.get('tasks_completed', 0),
            tasks_incomplete=factory.get('tasks_incomplete', 0),
            tasks_incomplete_with_reason=factory.get('tasks_incomplete_with_reason', 0),
            tasks_incomplete_no_reason=factory.get('tasks_incomplete_no_reason', 0),
            unscheduled_tasks_completed=factory.get('unscheduled_tasks_completed', 0),
            schedule_before_file=data['before_schedule_file'],
            schedule_after_file=data['after_schedule_file']
        )

        # Superintendent scores
        for super_perf in performance['superintendents']:
            db_manager.save_performance_score(
                shift_date=shift_date_obj,
                shift_number=shift_number,
                level='superintendent',
                entity_name=super_perf['superintendent'],
                planned_points=super_perf.get('total_tasks_planned', 0),
                earned_points=super_perf.get('tasks_completed', 0),
                score_percentage=super_perf.get('performance_index', 0),
                grade=super_perf.get('pi_grade', 'F'),
                total_tasks_planned=super_perf.get('total_tasks_planned', 0),
                tasks_completed=super_perf.get('tasks_completed', 0),
                tasks_incomplete=super_perf.get('tasks_incomplete', 0),
                tasks_incomplete_with_reason=super_perf.get('tasks_incomplete_with_reason', 0),
                tasks_incomplete_no_reason=super_perf.get('tasks_incomplete_no_reason', 0),
                unscheduled_tasks_completed=super_perf.get('unscheduled_tasks_completed', 0),
                schedule_before_file=data['before_schedule_file'],
                schedule_after_file=data['after_schedule_file']
            )

        # Team scores
        for team_perf in performance['teams']:
            if team_perf.get('no_tasks_planned'):
                continue

            db_manager.save_performance_score(
                shift_date=shift_date_obj,
                shift_number=shift_number,
                level='team',
                entity_name=team_perf['team'],
                planned_points=team_perf.get('total_tasks_planned', 0),
                earned_points=team_perf.get('tasks_completed', 0),
                score_percentage=team_perf.get('performance_index', 0),
                grade=team_perf.get('pi_grade', 'F'),
                total_tasks_planned=team_perf.get('total_tasks_planned', 0),
                tasks_completed=team_perf.get('tasks_completed', 0),
                tasks_incomplete=team_perf.get('tasks_incomplete', 0),
                tasks_incomplete_with_reason=team_perf.get('tasks_incomplete_with_reason', 0),
                tasks_incomplete_no_reason=team_perf.get('tasks_incomplete_no_reason', 0),
                unscheduled_tasks_completed=team_perf.get('unscheduled_tasks_completed', 0),
                schedule_before_file=data['before_schedule_file'],
                schedule_after_file=data['after_schedule_file']
            )

        # Persist per-aircraft completion projections from the after-schedule
        # for longitudinal slide tracking. The optimizer already computed
        # projectedCompletion per line_number — we just store it.
        after_products = performance.get('factory', {}).get('impact', {}).get('aircraft_deltas', [])
        # aircraft_deltas has the after-schedule data but not the raw products array.
        # Reload after-schedule products directly for clean storage.
        try:
            after_schedule = perf_engine.load_schedule(str(after_path))
            after_prods = after_schedule.get('products', [])
            if after_prods:
                db_manager.save_completion_history(
                    products=after_prods,
                    shift_date=shift_date_obj,
                    shift_number=shift_number,
                    schedule_file=data['after_schedule_file']
                )
        except Exception as hist_err:
            current_app.logger.warning(f"Could not save completion history: {hist_err}")

        # Enrich response with slide data if history exists
        try:
            slide_summary = db_manager.get_slide_summary()
            if slide_summary:
                performance['slide_summary'] = slide_summary
        except Exception:
            pass

        return jsonify({
            'success': True,
            'performance': performance,
            'message': 'Performance calculated and saved'
        })

    except ValueError as e:
        return jsonify({'error': f'Invalid date format: {str(e)}'}), 400
    except Exception as e:
        current_app.logger.error(f"Error calculating performance: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@shift_performance_bp.route('/scores/<shift_date>/<int:shift_number>', methods=['GET'])
def get_shift_performance_scores(shift_date, shift_number):
    """
    Get shift performance scores

    Query params:
        level: Optional filter by level (team, superintendent, factory)
        entity: Optional filter by entity name
    """
    try:
        # Parse date
        shift_date_obj = _parse_date(shift_date)

        # Get filters
        level = request.args.get('level')
        entity_name = request.args.get('entity')

        # Fetch from database
        scores = db_manager.get_shift_performance(
            shift_date=shift_date_obj,
            shift_number=shift_number,
            level=level,
            entity_name=entity_name
        )

        return jsonify({
            'shift_date': shift_date,
            'shift_number': shift_number,
            'level': level,
            'entity_name': entity_name,
            'scores': scores,
            'total': len(scores)
        })

    except ValueError as e:
        return jsonify({'error': f'Invalid date format: {str(e)}'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@shift_performance_bp.route('/team-history/<team_name>', methods=['GET'])
def get_team_performance_history(team_name):
    """
    Get performance history for a team

    Query params:
        days: Number of days of history (default 30)
    """
    try:
        days = int(request.args.get('days', 30))

        # Fetch from database
        history = db_manager.get_team_performance_history(
            team_name=team_name,
            days=days
        )

        return jsonify({
            'team': team_name,
            'days': days,
            'history': history,
            'total': len(history)
        })

    except ValueError as e:
        return jsonify({'error': f'Invalid parameter: {str(e)}'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@shift_performance_bp.route('/search-tasks', methods=['GET'])
def search_tasks():
    """Search tasks in a schedule file by partial SOI string.

    Query params:
        schedule: filename of the schedule to search (required)
        q: partial SOI string to search for (required, min 2 chars)
        line_number: optional — filter to a specific aircraft line

    Returns up to 20 matching tasks with soi, line_number, team, duration.
    """
    import gzip
    import json
    import os

    schedule_file = request.args.get('schedule', '').strip()
    query = request.args.get('q', '').strip().upper()
    line_filter = request.args.get('line_number', type=int)

    if not schedule_file or len(query) < 2:
        return jsonify({'results': []})

    try:
        schedules_dir = SCHEDULES_DIR
        filepath = schedules_dir / schedule_file

        if not filepath.exists():
            return jsonify({'results': [], 'error': 'Schedule file not found'})

        # Load schedule
        if str(filepath).endswith('.gz'):
            with gzip.open(filepath, 'rt', encoding='utf-8') as f:
                data = json.load(f)
        else:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)

        tasks = data.get('tasks', [])
        results = []
        seen = set()

        for t in tasks:
            soi = t.get('soi', '')
            ln = t.get('line_number')
            if not soi or ln is None:
                continue
            # Deduplicate by (soi, line_number)
            key = (soi, ln)
            if key in seen:
                continue
            # Apply line_number filter if specified
            if line_filter is not None and ln != line_filter:
                continue
            # Partial string match (case-insensitive)
            if query in soi.upper():
                seen.add(key)
                results.append({
                    'soi': soi,
                    'line_number': ln,
                    'team': t.get('team', ''),
                    'duration': t.get('duration_minutes', t.get('duration', 0)),
                    'cs': t.get('cs', ''),
                })
                if len(results) >= 20:
                    break

        # Sort by line_number then SOI for consistent display
        results.sort(key=lambda r: (r['line_number'], r['soi']))

        return jsonify({'results': results, 'total_matched': len(results)})

    except Exception as e:
        current_app.logger.error(f"Error searching tasks: {e}")
        return jsonify({'results': [], 'error': str(e)})


@shift_performance_bp.route('/available-schedules', methods=['GET'])
def get_available_schedules():
    """Get list of available schedule files for comparison.

    Supports two filename formats:
      NEW:    {prefix}_{WORKDAY}_S{N}_{HHMMSS}_UTC.json.gz
      LEGACY: {prefix}_{YYYYMMDD}_{HHMMSS}[_UTC].json.gz
              {prefix}_{YYYYMMDD}_shift{N}.json[.gz]

    Returns schedules sorted by file modification time (newest first),
    with work_day, shift_number, and aircraft_line_numbers metadata.
    """
    try:
        import os
        import re
        schedules_dir = SCHEDULES_DIR

        if not schedules_dir.exists():
            return jsonify({
                'schedules': [],
                'total': 0,
                'error': f'Schedules directory not found: {schedules_dir}'
            })

        # Find all schedule JSON files (both .json and .json.gz)
        import glob as glob_module
        prefixes = ['max_v1_', 'max_', 'focus_group1_', 'fgi5_optimized_',
                    'fgi_optimized_', 'focus_v2_', 'all_soi_second_shift_',
                    'all_soi_after_shift_']
        all_files = []
        for prefix in prefixes:
            all_files += glob_module.glob(str(schedules_dir / f'{prefix}*.json'))
            all_files += glob_module.glob(str(schedules_dir / f'{prefix}*.json.gz'))

        # Also discover any new-format schedule files (with _S{N}_ shift tag)
        # that may have been produced with a custom --soi argument
        for pattern in ['*_S1_*_UTC.json', '*_S1_*_UTC.json.gz',
                        '*_S2_*_UTC.json', '*_S2_*_UTC.json.gz',
                        '*_S3_*_UTC.json', '*_S3_*_UTC.json.gz']:
            all_files += glob_module.glob(str(schedules_dir / pattern))
        all_files = list(set(all_files))  # deduplicate

        # Sort by modification time (newest first)
        schedule_files = sorted(
            [Path(f) for f in all_files],
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )

        # Regex for NEW format: ..._{YYYYMMDD}_S{N}_{HHMMSS}_UTC
        # e.g. fgi5_optimized_20260222_S1_120000_UTC
        #      all_soi_second_shift_20260222_S2_193000_UTC
        new_format_re = re.compile(r'_(\d{8})_(S[123])_(\d{6})_UTC$')

        schedules = []
        for filepath in schedule_files:
            filename = filepath.name
            stem = filename.replace('.json.gz', '').replace('.json', '')

            date_str = None
            time_str = None
            shift_number = None
            work_day = None

            # --- Try NEW format first ---
            m = new_format_re.search(stem)
            if m:
                date_str = m.group(1)        # YYYYMMDD (work day)
                shift_tag = m.group(2)       # S1, S2, S3
                time_str = m.group(3)        # HHMMSS (UTC runtime)
                shift_number = int(shift_tag[1])
                work_day = date_str          # already the work day
            else:
                # --- LEGACY format parsing ---
                parts = stem.split('_')

                if stem.startswith('all_soi_second_shift_') and len(parts) >= 6:
                    # all_soi_second_shift_YYYYMMDD_HHMMSS[_UTC]
                    date_str = parts[4]
                    time_str = parts[5]
                    if time_str == 'UTC' and len(parts) >= 7:
                        time_str = parts[5]
                    elif len(parts) >= 7 and parts[6] == 'UTC':
                        time_str = parts[5]
                elif stem.startswith('fgi5_optimized_') and len(parts) >= 4:
                    date_str = parts[2]
                    time_str = parts[3]
                elif stem.startswith('fgi_optimized_') and len(parts) >= 4:
                    date_str = parts[2]
                    time_str = parts[3]
                elif stem.startswith('focus_v2_') and len(parts) >= 4:
                    date_str = parts[2]
                    time_str = parts[3]
                elif stem.startswith('focus_group1_') and len(parts) >= 4:
                    date_str = parts[2]
                    time_str = parts[3]

                # Legacy: try to extract shift from "shift1" style identifiers
                if time_str and time_str.startswith('shift') and len(time_str) > 5:
                    try:
                        shift_number = int(time_str[5:])
                    except ValueError:
                        pass

                # Infer shift_number from file prefix when filename doesn't encode it
                if shift_number is None:
                    if stem.startswith('all_soi_second_shift_'):
                        shift_number = 2  # Second shift SOI = shift 2

                # If still no shift_number, peek inside the JSON metadata
                if shift_number is None:
                    try:
                        import gzip as gzip_peek
                        with gzip_peek.open(filepath, 'rt', encoding='utf-8') as f:
                            import json as json_peek
                            # Read only first ~2KB to find shift_number field
                            raw = f.read(4096)
                            # Quick regex extract avoids full JSON parse
                            import re as re_peek
                            sn_match = re_peek.search(r'"shift_number"\s*:\s*(\d+)', raw)
                            if sn_match:
                                shift_number = int(sn_match.group(1))
                    except Exception:
                        pass  # Not critical — will fall to legacy pairing

                # Legacy: strip trailing _UTC from time_str
                if time_str == 'UTC':
                    time_str = None

                # Legacy files don't encode work_day; use date_str as fallback
                work_day = date_str

            if not date_str:
                continue

            # Build display strings
            try:
                display_date = f"{date_str[4:6]}/{date_str[6:8]}/{date_str[0:4]}"
            except Exception:
                display_date = date_str

            if time_str and time_str.startswith('shift'):
                display_time = f"shift {time_str[5:]}"
            elif time_str and len(time_str) >= 6:
                try:
                    display_time = f"{time_str[0:2]}:{time_str[2:4]}:{time_str[4:6]}"
                except Exception:
                    display_time = time_str or ''
            else:
                display_time = time_str or ''

            # Build run_timestamp for JS sorting
            run_ts = None
            try:
                if time_str and not time_str.startswith('shift') and len(time_str) >= 6:
                    run_ts = f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}T{time_str[0:2]}:{time_str[2:4]}:{time_str[4:6]}"
                else:
                    run_ts = f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}T00:00:00"
            except Exception:
                pass

            # Shift label for display
            shift_labels = {1: '1st', 2: '2nd', 3: '3rd'}
            shift_display = f"S{shift_number} ({shift_labels.get(shift_number, '?')})" if shift_number else None

            # Schedule type label
            if stem.startswith('all_soi_after_shift_'):
                schedule_type = 'After Shift'
            elif stem.startswith('all_soi_second_shift_'):
                schedule_type = 'Second Shift'
            elif stem.startswith('fgi5_optimized_'):
                schedule_type = 'FGI-5 Optimized'
            elif stem.startswith('fgi_optimized_'):
                schedule_type = 'FGI Optimized'
            elif stem.startswith('focus_v2_'):
                schedule_type = 'FOCUS v2'
            elif stem.startswith('focus_group1_'):
                schedule_type = 'FOCUS Group 1'
            else:
                schedule_type = 'Schedule'

            # Build display name with shift info if available
            if shift_display:
                display_name = f"{schedule_type} - {display_date} {shift_display} ({display_time} UTC)"
            else:
                display_name = f"{schedule_type} - {display_date} {display_time}"

            schedules.append({
                'filename': filename,
                'date_str': date_str,
                'identifier': time_str or '',
                'display_date': display_date,
                'display_time': display_time,
                'display_name': display_name,
                'schedule_type': schedule_type,
                'is_shift': bool(shift_number),
                'modified': filepath.stat().st_mtime,
                'run_timestamp': run_ts,
                'run_date': date_str,
                'shift_number': shift_number,
                'work_day': work_day,
                'shift_display': shift_display,
            })

        return jsonify({
            'schedules': schedules,
            'total': len(schedules),
            'schedules_dir': str(schedules_dir)
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e), 'schedules': []}), 500


@shift_performance_bp.route('/factory-trend', methods=['GET'])
def get_factory_trend():
    """
    Get factory-level performance trend over recent shifts.

    Query params:
        days: Number of days (default 30)
    """
    try:
        days = int(request.args.get('days', 30))

        with db_manager.get_connection() as conn:
            cursor = conn.execute("""
                SELECT shift_date, shift_number, score_percentage, grade,
                       total_tasks_planned, tasks_completed, tasks_incomplete,
                       tasks_incomplete_no_reason, earned_points, planned_points
                FROM shift_performance_scores
                WHERE level = 'factory'
                  AND shift_date >= date('now', '-' || ? || ' days')
                ORDER BY shift_date ASC, shift_number ASC
            """, (days,))
            rows = [dict(r) for r in cursor.fetchall()]

        return jsonify({
            'days': days,
            'trend': rows,
            'total': len(rows)
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@shift_performance_bp.route('/superintendent-trend/<super_name>', methods=['GET'])
def get_superintendent_trend(super_name):
    """
    Get superintendent performance trend.

    Query params:
        days: Number of days (default 30)
    """
    try:
        days = int(request.args.get('days', 30))

        with db_manager.get_connection() as conn:
            cursor = conn.execute("""
                SELECT shift_date, shift_number, score_percentage, grade,
                       total_tasks_planned, tasks_completed, tasks_incomplete,
                       tasks_incomplete_no_reason, earned_points, planned_points
                FROM shift_performance_scores
                WHERE level = 'superintendent' AND entity_name = ?
                  AND shift_date >= date('now', '-' || ? || ' days')
                ORDER BY shift_date ASC, shift_number ASC
            """, (super_name, days))
            rows = [dict(r) for r in cursor.fetchall()]

        return jsonify({
            'superintendent': super_name,
            'days': days,
            'trend': rows,
            'total': len(rows)
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============================================================================
# SCHEDULE SLIDE TRACKING
# ============================================================================

@shift_performance_bp.route('/slide-summary', methods=['GET'])
def get_slide_summary():
    """
    Get current slide status for all tracked aircraft.

    Returns per line_number:
    - current projected completion (from latest schedule)
    - high-water mark (best-ever projected completion)
    - cumulative slide in minutes and hours
    - shift-to-shift delta
    """
    try:
        rows = db_manager.get_slide_summary()

        # Convert cumulative slide minutes to hours for display
        for r in rows:
            slide_min = r.get('cumulative_slide_minutes') or 0
            r['cumulative_slide_hours'] = round(slide_min / 60.0, 1)
            delta_min = r.get('shift_delta_minutes') or 0
            r['shift_delta_hours'] = round(delta_min / 60.0, 1)

        return jsonify({
            'aircraft': rows,
            'total': len(rows)
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@shift_performance_bp.route('/slide-history/<int:line_number>', methods=['GET'])
def get_slide_history(line_number):
    """
    Get completion projection history for a single aircraft.

    Query params:
        shifts: Max records to return (default 90 = ~30 days × 3 shifts)

    Returns chronological list of projected completion per shift,
    plus computed high-water mark and cumulative slide.
    """
    try:
        shifts = int(request.args.get('shifts', 90))
        rows = db_manager.get_completion_history(line_number, shifts=shifts)

        if not rows:
            return jsonify({
                'line_number': line_number,
                'history': [],
                'high_water': None,
                'current': None,
                'cumulative_slide_hours': 0
            })

        # Compute high-water mark (minimum completion_abs_minutes)
        high_water_abs = min(r['completion_abs_minutes'] for r in rows)
        high_water_row = next(r for r in rows
                              if r['completion_abs_minutes'] == high_water_abs)

        current = rows[-1]
        cumulative_slide_min = current['completion_abs_minutes'] - high_water_abs

        # Add shift-to-shift delta to each row
        for i, r in enumerate(rows):
            if i == 0:
                r['shift_delta_minutes'] = 0
            else:
                r['shift_delta_minutes'] = (
                    r['completion_abs_minutes'] - rows[i-1]['completion_abs_minutes']
                )
            r['slide_from_high_water'] = r['completion_abs_minutes'] - high_water_abs

        return jsonify({
            'line_number': line_number,
            'history': rows,
            'high_water': {
                'completion_abs_minutes': high_water_abs,
                'projected_completion': high_water_row['projected_completion'],
                'shift_date': high_water_row['shift_date'],
                'shift_number': high_water_row['shift_number'],
            },
            'current': {
                'completion_abs_minutes': current['completion_abs_minutes'],
                'projected_completion': current['projected_completion'],
                'shift_date': current['shift_date'],
                'shift_number': current['shift_number'],
            },
            'cumulative_slide_minutes': cumulative_slide_min,
            'cumulative_slide_hours': round(cumulative_slide_min / 60.0, 1),
            'total_records': len(rows)
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============================================================================
# INTRA-SHIFT PROGRESS TRACKING
# ============================================================================

@shift_performance_bp.route('/shift-progress/<work_day>/<int:shift_number>', methods=['GET'])
def get_shift_progress(work_day, shift_number):
    """
    Get intra-shift progress timeline for a specific work day and shift.

    Finds the shift plan and all snapshots, then calculates performance
    at each snapshot point relative to the plan.

    Returns a timeline of progress checkpoints showing cumulative performance
    throughout the shift.
    """
    try:
        import os
        import re
        import gzip as gzip_module

        schedules_dir = SCHEDULES_DIR

        if not schedules_dir.exists():
            return jsonify({'error': 'Schedules directory not found', 'timeline': []}), 404

        # Find the shift plan (full optimizer run, not a snapshot)
        plan_pattern = re.compile(
            rf'(?!.*snapshot).*_{work_day}_S{shift_number}_\d{{6}}_UTC\.json\.gz$'
        )
        plan_candidates = [
            f for f in schedules_dir.glob('*.json.gz')
            if plan_pattern.match(f.name) and 'snapshot' not in f.name
        ]

        if not plan_candidates:
            return jsonify({
                'error': f'No plan found for S{shift_number} on {work_day}',
                'timeline': [],
                'work_day': work_day,
                'shift_number': shift_number
            })

        # Use most recent plan
        plan_candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        plan_path = plan_candidates[0]

        # Load plan
        with gzip_module.open(plan_path, 'rt', encoding='utf-8') as f:
            plan_data = json.load(f)

        plan_tasks = plan_data.get('tasks', [])
        plan_task_set = {(t['soi'], t['line_number']) for t in plan_tasks if t.get('soi')}

        # Find all snapshots for this shift
        snapshot_pattern = re.compile(
            rf'fgi5_snapshot_{work_day}_S{shift_number}_(\d{{6}})_UTC\.json\.gz$'
        )
        snapshot_files = []
        for f in schedules_dir.glob('fgi5_snapshot_*.json.gz'):
            m = snapshot_pattern.match(f.name)
            if m:
                snapshot_files.append((f, m.group(1)))  # (path, HHMMSS)

        # Sort snapshots by time
        snapshot_files.sort(key=lambda x: x[1])

        # Build timeline
        timeline = []

        # First point: the plan itself (0% complete at shift start)
        plan_timestamp = plan_data.get('metadata', {}).get('generated_at', '')
        shift_labels = {1: '1st', 2: '2nd', 3: '3rd'}
        timeline.append({
            'type': 'plan',
            'label': f'Shift Plan ({shift_labels.get(shift_number, "?")} Shift)',
            'timestamp': plan_timestamp,
            'time_utc': '',
            'filename': plan_path.name,
            'total_planned': len(plan_tasks),
            'remaining': len(plan_tasks),
            'completed': 0,
            'completion_pct': 0.0,
            'new_tasks': 0,
        })

        # Process each snapshot
        for snap_path, time_code in snapshot_files:
            try:
                with gzip_module.open(snap_path, 'rt', encoding='utf-8') as f:
                    snap_data = json.load(f)

                snap_tasks = snap_data.get('tasks', [])
                snap_task_set = {(t['soi'], t['line_number']) for t in snap_tasks if t.get('soi')}

                completed = plan_task_set - snap_task_set
                new_tasks = snap_task_set - plan_task_set
                remaining = plan_task_set & snap_task_set

                completed_count = len(completed)
                total_planned = len(plan_task_set)
                completion_pct = round(completed_count / total_planned * 100, 1) if total_planned > 0 else 0

                # Format time display
                display_time = f"{time_code[0:2]}:{time_code[2:4]}:{time_code[4:6]} UTC"

                timeline.append({
                    'type': 'snapshot',
                    'label': f'Check-in {display_time}',
                    'timestamp': snap_data.get('snapshot_timestamp', ''),
                    'time_utc': display_time,
                    'filename': snap_path.name,
                    'total_planned': total_planned,
                    'remaining': len(remaining),
                    'completed': completed_count,
                    'completion_pct': completion_pct,
                    'new_tasks': len(new_tasks),
                })

            except Exception as e:
                print(f"Warning: Could not process snapshot {snap_path.name}: {e}")
                continue

        # Calculate pace metrics
        if len(timeline) >= 2:
            last = timeline[-1]
            num_checkpoints = len(timeline) - 1
            # Simple pace: ~25% per 2hr in an 8hr shift
            expected_pct = min(100, num_checkpoints * 25)
            actual_pct = last['completion_pct']
            pace_delta = actual_pct - expected_pct
            pace_status = 'ahead' if pace_delta > 5 else ('behind' if pace_delta < -5 else 'on_pace')
        else:
            pace_delta = 0
            pace_status = 'no_data'

        return jsonify({
            'work_day': work_day,
            'shift_number': shift_number,
            'shift_label': shift_labels.get(shift_number, '?'),
            'plan_filename': plan_path.name,
            'timeline': timeline,
            'total_checkpoints': len(timeline) - 1,
            'pace': {
                'status': pace_status,
                'delta_pct': pace_delta,
            },
            'latest': timeline[-1] if timeline else None,
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e), 'timeline': []}), 500


@shift_performance_bp.route('/shift-progress/latest', methods=['GET'])
def get_latest_shift_progress():
    """
    Get shift progress for the most recent shift that has snapshots.
    Auto-detects the current shift and work day.
    """
    try:
        import os
        import re

        schedules_dir = SCHEDULES_DIR

        if not schedules_dir.exists():
            return jsonify({'error': 'No schedules directory', 'timeline': []}), 404

        # Find all snapshot files and extract work_day + shift
        snapshot_re = re.compile(r'fgi5_snapshot_(\d{8})_S([123])_\d{6}_UTC\.json\.gz$')
        seen = {}
        for f in sorted(schedules_dir.glob('fgi5_snapshot_*.json.gz'),
                        key=lambda p: p.stat().st_mtime, reverse=True):
            m = snapshot_re.match(f.name)
            if m:
                wd = m.group(1)
                sn = int(m.group(2))
                key = (wd, sn)
                if key not in seen:
                    seen[key] = f

        if not seen:
            # No snapshots yet — detect current shift for plan-only view
            try:
                # VENDOR CHANGE (web_flask/VENDOR_CHANGES.md):
                # max.core.shift_detection -> src.ff_constants local shim
                # (verbatim ET-boundary logic; no MAX engine tree here).
                from src.ff_constants import detect_shift_and_work_day
                shift_number, work_day = detect_shift_and_work_day()
                work_day_str = work_day.strftime('%Y%m%d')
                return get_shift_progress(work_day_str, shift_number)
            except Exception:
                return jsonify({'error': 'No snapshots found', 'timeline': []})

        # Use the most recent snapshot's work_day/shift
        latest_key = next(iter(seen))
        work_day_str, shift_number = latest_key

        return get_shift_progress(work_day_str, shift_number)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e), 'timeline': []}), 500
