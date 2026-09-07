"""Central path configuration for the Flask dashboard.

VENDOR CHANGE (web_flask/VENDOR_CHANGES.md): this vendored copy lives at
FF_app/web_flask/, so ``MAX_ROOT`` (the variable name is kept for the
importers' sake) resolves to the FF_app root and the default schedule
directory becomes FF_app/outputs/schedules — where FF_app's envelope
exporter (ff/export/envelope.py) writes max_v1_*.json.gz.  The
``MAX_SCHEDULES_DIR`` env override still wins, exactly as upstream.
Runtime artifacts (SQLite shift-performance history, IE review queue)
stay inside web_flask/ so the app remains self-contained.
"""

import os
import sys
from pathlib import Path

WEBAPP_ROOT = Path(__file__).resolve().parent.parent    # FF_app/web_flask/
MAX_ROOT = WEBAPP_ROOT.parent                           # FF_app/ (name kept)

# Upstream appended this to sys.path so max.* resolved; here it makes the
# FF_app root importable (ff.* for the Part-C integration). Appended, not
# prepended, so it can never shadow app modules.
if str(MAX_ROOT) not in sys.path:
    sys.path.append(str(MAX_ROOT))

# Where the FF_app engine exports schedules (python3 run.py run-schedule /
# export-envelope) — override for tests or alternate layouts.
SCHEDULES_DIR = Path(os.environ.get(
    "MAX_SCHEDULES_DIR", str(MAX_ROOT / "outputs" / "schedules")))

# Dashboard-local persistence.
DATA_DIR = WEBAPP_ROOT / "data"                         # shift_performance.db
IE_QUEUE_FILE = WEBAPP_ROOT / "ie_review_queue.json"
