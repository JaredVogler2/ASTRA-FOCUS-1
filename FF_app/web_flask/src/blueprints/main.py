# src/blueprints/main.py

from flask import Blueprint, render_template, jsonify, current_app
from pathlib import Path
import glob

main_bp = Blueprint('main', __name__)

@main_bp.route('/')
def landing_page():
    return render_template('landing_page.html')

@main_bp.route('/dashboard')
def index():
    """Serve the main dashboard page (iOS-themed)"""
    return render_template('dashboard-ios.html')

@main_bp.route('/dashboard/classic')
def dashboard_classic():
    """Serve the classic dashboard page"""
    return render_template('dashboard2.html')

@main_bp.route('/api/available-schedules')
def get_available_schedules():
    """Return list of available schedule files"""
    try:
        # Get available schedules from app context
        available_schedules = getattr(current_app, 'available_schedules', None)

        # If not set (development mode), scan filesystem directly
        if available_schedules is None:
            import os
            from pathlib import Path
            import glob as glob_module

            print("available_schedules not in app context, scanning filesystem...")
            from src.paths import SCHEDULES_DIR
            schedules_dir = SCHEDULES_DIR

            if schedules_dir.exists():
                # Find all focus_group1_*.json and focus_group1_*.json.gz files
                pattern_json = str(schedules_dir / 'focus_group1_*.json')
                pattern_gz = str(schedules_dir / 'focus_group1_*.json.gz')
                files = glob_module.glob(pattern_json) + glob_module.glob(pattern_gz)

                # Parse timestamps from filenames
                schedule_files = []
                for filepath in files:
                    filename = Path(filepath).name
                    try:
                        # Remove .json.gz or .json extension
                        parts = filename.replace('.json.gz', '').replace('.json', '').split('_')
                        if len(parts) >= 4:
                            date_str = parts[2]  # 20251107
                            time_or_shift = parts[3]  # 143542 or shift1

                            # Parse datetime for sorting
                            if time_or_shift.startswith('shift'):
                                shift_times = {'shift1': '050000', 'shift2': '133000', 'shift3': '220000'}
                                time_str = shift_times.get(time_or_shift, '120000')
                            else:
                                time_str = time_or_shift

                            datetime_str = f"{date_str}_{time_str}"
                            schedule_files.append((filepath, datetime_str, filename))
                    except Exception as e:
                        print(f"Warning: Could not parse filename {filename}: {e}")
                        continue

                # Sort by datetime (newest first)
                schedule_files.sort(key=lambda x: x[1], reverse=True)
                available_schedules = [(f[0], f[2]) for f in schedule_files]
            else:
                available_schedules = []

        # Format for frontend (show all available schedules)
        schedules = []
        for i, (filepath, filename) in enumerate(available_schedules):
            # Extract run_date and shift_number from filename
            # Format: focus_group1_YYYYMMDD_HHMMSS.json.gz or focus_group1_YYYYMMDD_shiftN.json.gz
            run_date = None
            shift_number = None
            display_name = filename  # Fallback
            display_date = None
            identifier = None

            try:
                parts = filename.replace('.json.gz', '').replace('.json', '').split('_')
                if len(parts) >= 3:
                    date_str = parts[2]  # YYYYMMDD
                    # Convert to YYYY-MM-DD format for run_date
                    run_date = f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}"
                    # Convert to MM/DD/YYYY for display
                    display_date = f"{date_str[4:6]}/{date_str[6:8]}/{date_str[0:4]}"

                    if len(parts) >= 4:
                        time_or_shift = parts[3]
                        if time_or_shift.startswith('shift'):
                            # Extract shift number
                            shift_number = int(time_or_shift.replace('shift', ''))
                            identifier = f"Shift {shift_number}"
                            # Format: MM/DD/YYYY Shift N
                            display_name = f"{display_date} Shift {shift_number}"
                        else:
                            # Time-based schedule - format as HH:MM
                            shift_number = 1
                            time_str = time_or_shift  # HHMMSS
                            if len(time_str) >= 4:
                                formatted_time = f"{time_str[0:2]}:{time_str[2:4]}"
                                identifier = formatted_time
                                # Format: MM/DD/YYYY HH:MM
                                display_name = f"{display_date} {formatted_time}"
                            else:
                                identifier = time_str
                                display_name = f"{display_date} {time_str}"
            except Exception as e:
                print(f"Warning: Could not extract date/shift from {filename}: {e}")
                display_name = filename.replace('.json.gz', '').replace('.json', '').replace('focus_group1_', '').replace('_', ' ')

            schedules.append({
                'id': f'schedule_{i}',
                'filename': filename,
                'filepath': filepath,
                'display_name': display_name,
                'display_date': display_date,
                'identifier': identifier,
                'run_date': run_date,
                'shift_number': shift_number,
                'is_current': i == 0  # Most recent is current
            })

        return jsonify({
            'schedules': schedules,
            'total': len(available_schedules)
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e), 'schedules': []}), 500

@main_bp.app_errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not found'}), 404

@main_bp.app_errorhandler(500)
def internal_error(error):
    # It's good practice to log the error here
    # import traceback
    # traceback.print_exc()
    return jsonify({'error': 'Internal server error'}), 500
