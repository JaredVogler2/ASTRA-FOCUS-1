"""ff.sim — digital-week simulator + dispatch-policy behavior probe.

Adapts the MAX digital-week v2 doctrine (`MAX/tools/cadence_sim2.py`,
FOCU5 D5 "Simulation & Replay") to FF_app: execute the shift that just
ended, inject rework the production way (new Task rows that join the DAG),
roll the factory clock across the weekend seam, replan with the REAL
engine (compute_cpm + build_schedule — never a proxy), measure.

Honesty (OR-5): every simulator output carries ``mock_data`` propagated
from the fleet meta; all figures are synthetic; no optimality claims.

Modules:

- ``ff.sim.digital_week`` — ``run_digital_week`` + ``advance_clock`` and the
  doctrine constants (``SHIFT_CYCLE``, ``EXEC_RATE``, ``SLIDE_REMAIN``,
  ``REWORK_PER_ROUND``).
- ``ff.sim.policies`` — ``simulate_policy`` with the X4 behavior-probe pair
  ``flow`` vs ``chaser`` (imported separately; it pulls in the points layer).
"""

from ff.sim.digital_week import (  # noqa: F401
    EXEC_RATE,
    REWORK_PER_ROUND,
    SHIFT_CYCLE,
    SLIDE_REMAIN,
    advance_clock,
    run_digital_week,
)
