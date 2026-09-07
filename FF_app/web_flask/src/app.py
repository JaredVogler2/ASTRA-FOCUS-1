# app.py - Flask Web Server for the FOCUS Production Scheduling Dashboard
# Ported from FOCUS-2026-FGI (the original non-modular Flask dashboard) and
# wired to the MAX (Fable) 4-stage optimization engine: it loads the
# max_v1_*.json.gz envelopes the engine writes to <MAX>/outputs/schedules/,
# normalizing them into the FGI envelope shape this dashboard was built
# against (src/max_adapter.py).  Legacy FGI-5 exports still load unchanged.

import sys
import os
from pathlib import Path

# Ensure UTF-8 output encoding for console (avoids cp1252 errors with Unicode chars)
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass  # Python < 3.7

# Add web_app_flask root to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, jsonify
# VENDOR CHANGE (web_flask/VENDOR_CHANGES.md): flask_cors / flask_compress
# made optional — FF_app's runtime is stdlib+flask only.  When the packages
# are installed the behavior is byte-identical to upstream; when absent the
# app serves uncompressed same-origin responses (no-op shims below).
try:
    from flask_cors import CORS
except ImportError:  # pragma: no cover — environment without flask-cors
    def CORS(app, *args, **kwargs):
        return app
try:
    from flask_compress import Compress
except ImportError:  # pragma: no cover — environment without flask-compress
    class Compress:
        def __init__(self, app=None):
            pass
import glob

from src.max_adapter import load_envelope
from src.paths import SCHEDULES_DIR

def create_app():
    """Create and configure an instance of the Flask application."""
    app = Flask(__name__, template_folder='../templates', static_folder='../static')
    CORS(app)

    # Enable gzip compression for all responses (reduces 25MB to ~2-3MB)
    Compress(app)
    app.config['COMPRESS_MIMETYPES'] = ['text/html', 'text/css', 'text/xml', 'application/json', 'application/javascript']
    app.config['COMPRESS_LEVEL'] = 6  # Compression level 1-9 (6 is good balance of speed/size)
    app.config['COMPRESS_MIN_SIZE'] = 500  # Only compress responses > 500 bytes

    app.config['JSON_AS_ASCII'] = False

    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
    app.jinja_env.auto_reload = True
    app.jinja_env.cache = {}

    # Use app context to store 3-stage scheduler data
    with app.app_context():
        # Legacy scheduler (for backward compatibility during migration)
        app.scheduler = None
        app.scenario_results = {}
        app.saved_scenarios = {}

        # Legacy in-process scheduler state the blueprints still probe
        # (always empty in this port — the MAX engine runs out of process)
        app.loader = None
        app.dags = {}
        app.segment_groups = {}
        app.focus_aircraft = []
        app.final_schedule = {}
        app.mechanic_timelines = {}
        app.calculated_pool_sizes = {}

        # Summary metrics
        app.summary_metrics = {}

        # Mechanic assignments tracking (used by assignments blueprint)
        app.mechanic_assignments = {}

        # Schedule data (populated by load_schedules_from_files in production mode)
        app.current_schedule_data = None
        app.current_schedule_file = None
        app.previous_schedule_data = None
        app.previous_schedule_file = None
        app.available_schedules = []
        app.active_schedule = 'current'


    from src.blueprints.main import main_bp
    from src.blueprints.scenarios import scenarios_bp
    from src.blueprints.assignments import assignments_bp
    from src.blueprints.supply_chain import supply_chain_bp
    from src.blueprints.industrial_engineering import ie_bp
    from src.blueprints.shift_performance import shift_performance_bp
    from src.blueprints.staffing_dashboard import staffing_dashboard_bp
    from src.blueprints.development import development_bp
    from src.blueprints.projection import projection_bp
    from src.blueprints.shiftbook import shiftbook_bp
    # VENDOR CHANGE (INCREMENT 6 Part C, web_flask/VENDOR_CHANGES.md):
    # FF bridge — server-side proxy to the FF_app backend (FF_API_BASE)
    # for the Arena/Progression/Excusals/Explain tabs + My Day write-back.
    from src.blueprints.ff_bridge import ff_bridge_bp

    def find_available_schedules():
        """
        Scan outputs/schedules/ for available schedule JSON files.

        Sorts by **file modification time** (newest first) so the most
        recently written file is always picked regardless of the timezone
        used when the filename timestamp was generated.

        Returns:
            List of (filepath, filename) tuples, sorted by modification time (newest first)
        """
        schedules_dir = SCHEDULES_DIR

        if not schedules_dir.exists():
            return []

        # Find all schedule JSON files — the MAX (Fable) engine's max_v1_*
        # exports first-class, plus every legacy FGI-family prefix.
        prefixes = ['max_v1_', 'max_', 'focus_group1_', 'fgi5_optimized_',
                    'fgi_optimized_', 'focus_v2_', 'all_soi_second_shift_',
                    'all_soi_after_shift_']
        files = []
        for prefix in prefixes:
            files += glob.glob(str(schedules_dir / f'{prefix}*.json'))
            files += glob.glob(str(schedules_dir / f'{prefix}*.json.gz'))

        # Also discover any new-format schedule files (with _S{N}_ shift tag)
        # produced by custom --soi arguments to run_fgi5_optimizer.py
        for pattern in ['*_S1_*_UTC.json', '*_S1_*_UTC.json.gz',
                        '*_S2_*_UTC.json', '*_S2_*_UTC.json.gz',
                        '*_S3_*_UTC.json', '*_S3_*_UTC.json.gz']:
            files += glob.glob(str(schedules_dir / pattern))
        files = list(set(files))  # deduplicate

        # Build list with file modification time for reliable sorting.
        # Previous approach parsed the timestamp from the filename, but
        # datetime.now() varies across timezone settings (UTC vs local),
        # causing a file generated on a UTC host to appear "newer" than
        # one generated later on an EST host.  File mtime is immune to
        # this because it records the actual wall-clock moment the file
        # was written.
        schedule_files = []
        for filepath in files:
            filename = Path(filepath).name
            try:
                mtime = os.path.getmtime(filepath)
                schedule_files.append((filepath, mtime, filename))
            except OSError as e:
                print(f"Warning: Could not stat {filename}: {e}")
                continue

        # Sort by modification time (newest first)
        schedule_files.sort(key=lambda x: x[1], reverse=True)

        return [(f[0], f[2]) for f in schedule_files]  # Return (filepath, filename)

    def load_schedule_from_json(filepath):
        """
        Load a pre-computed schedule from JSON file (supports both .json and .json.gz)

        MAX (Fable) engine envelopes are normalized to the FGI shape this
        dashboard consumes; FGI envelopes pass through untouched.

        Returns:
            Dict with schedule data, or None if load fails
        """
        return load_envelope(filepath)

    def load_schedules_from_files(app):
        """
        Load the 2 most recent schedules from outputs/schedules/

        Populates app context with:
        - app.current_schedule_data: Most recent schedule
        - app.previous_schedule_data: Second most recent schedule
        - app.available_schedules: List of all available schedules

        Returns:
            True if schedules loaded successfully, False otherwise
        """
        with app.app_context():
            print("=" * 80)
            print("LOADING PRE-COMPUTED SCHEDULES FROM FILES (Production Mode)")
            print("=" * 80)

            # Find available schedules
            available = find_available_schedules()
            app.available_schedules = [(f, fn) for f, fn in available]

            if not available:
                print("No pre-computed schedules found in outputs/schedules/")
                return False

            print(f"\nFound {len(available)} schedule(s):")
            for filepath, filename in available[:5]:  # Show first 5
                print(f"  - {filename}")
            if len(available) > 5:
                print(f"  ... and {len(available) - 5} more")

            # Load the newest schedule that actually parses — a truncated
            # or mid-write export must not take down the whole dashboard
            # when an older good plan sits in the same directory.
            loaded = []      # [(data, filename)]
            for filepath, filename in available:
                if len(loaded) >= 2:
                    break
                data = load_schedule_from_json(filepath)
                if data:
                    loaded.append((data, filename))
                    print(f"  ✓ {filename}: {len(data.get('tasks', [])):,} tasks")
                else:
                    print(f"  ⚠ Skipping unreadable schedule: {filename}")

            if not loaded:
                print("  ✗ No schedule file could be loaded")
                return False

            app.current_schedule_data, app.current_schedule_file = loaded[0]
            if len(loaded) > 1:
                app.previous_schedule_data, app.previous_schedule_file = loaded[1]
            else:
                print("\n[2/2] No previous schedule available")
                app.previous_schedule_data = None
                app.previous_schedule_file = None

            # Set active schedule (default to current)
            app.active_schedule = 'current'

            print("\n" + "=" * 80)
            print("SCHEDULES LOADED SUCCESSFULLY (Production Mode)")
            print("=" * 80)
            print(f"Current: {app.current_schedule_file}")
            if app.previous_schedule_data:
                print(f"Previous: {app.previous_schedule_file}")
            print(f"Dashboard ready - instant startup!")
            print("=" * 80)

            return True

    # Register blueprints + error handlers (shared by every startup path)
    app.register_blueprint(main_bp)
    app.register_blueprint(scenarios_bp, url_prefix='/api')
    app.register_blueprint(assignments_bp, url_prefix='/api')
    app.register_blueprint(supply_chain_bp)
    app.register_blueprint(ie_bp)
    app.register_blueprint(shift_performance_bp)
    app.register_blueprint(staffing_dashboard_bp)
    app.register_blueprint(development_bp)
    app.register_blueprint(projection_bp)
    app.register_blueprint(shiftbook_bp)
    app.register_blueprint(ff_bridge_bp)  # VENDOR CHANGE (Part C)

    @app.errorhandler(404)
    def not_found(error):
        return jsonify({'error': 'Not found'}), 404

    @app.errorhandler(500)
    def internal_error(error):
        return jsonify({'error': 'Internal server error'}), 500

    # Let blueprints trigger a re-scan of outputs/schedules (POST /api/refresh)
    app.reload_schedules = lambda: load_schedules_from_files(app)

    # Flask debug mode reloader handling:
    # When use_reloader=True (disabled by default now), Flask spawns a child process.
    # WERKZEUG_RUN_MAIN='true' only in the child worker process.
    # If reloader IS enabled via FLASK_USE_RELOADER=1, skip data loading in parent.
    werkzeug_main = os.environ.get('WERKZEUG_RUN_MAIN')
    use_reloader_enabled = os.environ.get('FLASK_USE_RELOADER') == '1'
    if werkzeug_main is None and use_reloader_enabled:
        print("\n⏳ Reloader parent process - skipping data loading...")
        print("   (Worker process will load data)")
        return app

    # Load pre-computed schedules. There is no in-process fallback
    # optimizer in this port (FORCE_RERUN is no longer honored) —
    # schedules come from the MAX (Fable) engine:
    #     cd <MAX> && python run.py run
    # then restart the dashboard or POST /api/refresh.
    if load_schedules_from_files(app):
        print("\n✓ Dashboard initialized (loaded from files)")
    else:
        print("\n" + "=" * 80)
        print("No loadable schedules in outputs/schedules/.")
        print("Generate one with the MAX (Fable) engine:  python run.py run")
        print("Then POST /api/refresh (or restart) to pick it up.")
        print("=" * 80)

    return app


if __name__ == '__main__':
    app = create_app()
    print("\n" + "="*80)
    print("FOCUS DASHBOARD SERVER STARTING")
    print("="*80)
    print(f"Access dashboard at: http://localhost:5000")
    print(f"Press CTRL+C to stop the server")
    print("="*80 + "\n")
    # Disable reloader to prevent double initialization (data loading is expensive)
    # Set FLASK_USE_RELOADER=1 to enable hot-reloading if needed
    use_reloader = os.environ.get('FLASK_USE_RELOADER', '0') == '1'
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=use_reloader)