# src/ff_constants.py — local constants shim (VENDOR CHANGE, documented in
# web_flask/VENDOR_CHANGES.md).
#
# The upstream dashboard (MAX/web_app_flask) imports these values from the
# MAX engine package (max.core.config / max.core.shift_detection).  This
# vendored copy runs inside FF_app, which has NO dependency on the MAX
# engine tree, so the handful of consumed values live here VERBATIM.
#
# Values verified against MAX/max/core/config.py (and FOCU5/GROUNDING.md
# section 1) on 2026-07-11 — identical numbers, do not "tune" them here:
#   SHIFT_EFFECTIVE = {1: 460, 2: 460, 3: 370}
#   SHIFT_PAID      = {1: 480, 2: 480, 3: 390}
#   SHIFT_MAX       = {1: 520, 2: 520, 3: 430}
#   DAY_WORK_MINUTES = sum(SHIFT_EFFECTIVE.values()) = 1290
#
# detect_shift_and_work_day is copied VERBATIM (logic and boundaries) from
# MAX/max/core/shift_detection.py: ET boundaries 05:30 / 14:00 / 22:30 and
# the "3rd shift belongs to the work day it flows INTO" convention.

from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

# Effective plannable minutes per shift (engine truth; shift 3 is shorter).
SHIFT_EFFECTIVE = {1: 460, 2: 460, 3: 370}

# Paid minutes per shift (effective + paid buffer) — utilization denominators.
SHIFT_PAID = {1: 480, 2: 480, 3: 390}

# Hard per-shift maximum (effective + overtime headroom).
SHIFT_MAX = {1: 520, 2: 520, 3: 430}

# Full 3-shift working day in effective minutes (S1+S2+S3).
DAY_WORK_MINUTES = sum(SHIFT_EFFECTIVE.values())


# Eastern-time minutes-since-midnight shift boundaries (30 min before the
# actual shift start, matching MAX/max/core/shift_detection.py).
_S1_START = 330       # 5:30 AM
_S2_START = 840       # 2:00 PM
_S3_START = 1350      # 10:30 PM


def detect_shift_and_work_day(utc_now=None):
    """Return (shift_number, work_day) for the current ET wall clock.

    Verbatim behavior of max.core.shift_detection.detect_shift_and_work_day:
    a 3rd shift belongs to the work day it FLOWS into (a shift starting
    10:30 PM on 2/21 belongs to work day 2/22).
    """
    if utc_now is None:
        utc_now = datetime.now(timezone.utc)
    elif utc_now.tzinfo is None:
        utc_now = utc_now.replace(tzinfo=timezone.utc)

    eastern = ZoneInfo("America/New_York")
    et_now = utc_now.astimezone(eastern)
    et_minutes = et_now.hour * 60 + et_now.minute

    if _S1_START <= et_minutes < _S2_START:
        return 1, et_now.date()
    if _S2_START <= et_minutes < _S3_START:
        return 2, et_now.date()
    if et_minutes >= _S3_START:
        return 3, et_now.date() + timedelta(days=1)
    return 3, et_now.date()
