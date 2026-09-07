"""Projection & economics API (Step 5b) — backs the NEW dashboard tabs.

Serves the committed-delivery-dates block and the dollar-exposure
rollup the engine embeds in every envelope (metadata.stats.projection
/ metadata.stats.economics).  Additive only: new endpoints for new
tabs; every existing view and endpoint is untouched so the owner can
compare old and new side by side.
"""

from flask import Blueprint, jsonify, current_app

projection_bp = Blueprint('projection', __name__)


def _stats():
    data = getattr(current_app, 'current_schedule_data', None) or {}
    return (data.get('metadata') or {}).get('stats') or {}


@projection_bp.route('/api/projection')
def projection():
    stats = _stats()
    proj = stats.get('projection')
    if not proj:
        return jsonify({
            'available': False,
            'reason': 'schedule was produced without the projection '
                      'layer (--no-projection or pre-Step-5b run)'})
    kept = stats.get('commitment_kept')
    in_hz = stats.get('commitment_in_horizon')
    return jsonify({
        'available': True,
        'runId': proj.get('runId'),
        'stationFeed': bool(proj.get('stationFeed')),
        'committed': proj.get('committed') or [],
        'earlyFlow': proj.get('earlyFlow') or [],
        'delivered': proj.get('delivered') or [],
        'changes': proj.get('changes') or [],
        'stability': {
            'keptIncumbentSlots': kept,
            'inHorizon': in_hz,
            'keptPct': (round(100.0 * kept / in_hz, 1)
                        if kept is not None and in_hz else None),
        },
        'finalLateness': stats.get('final_lateness'),
    })


@projection_bp.route('/api/projection/economics')
def economics():
    stats = _stats()
    econ = stats.get('economics')
    if not econ:
        return jsonify({'available': False,
                        'reason': 'no economics block in this schedule'})
    return jsonify({
        'available': True,
        'source': econ.get('source'),
        'fleet': econ.get('fleet') or {},
        'perAircraft': econ.get('per_aircraft') or [],
    })


@projection_bp.route('/api/capacity')
def capacity():
    """Pressure attribution (from the loaded envelope) + measured
    +1-head what-if results (from the last sweep artifact, if any)."""
    import json as _json
    from src.paths import MAX_ROOT
    stats = _stats()
    pressure = stats.get('capacity_pressure')
    artifact = None
    p = MAX_ROOT / 'outputs' / 'capacity_whatif.json'
    if p.exists():
        try:
            artifact = _json.loads(p.read_text())
        except (OSError, ValueError):
            artifact = None
    return jsonify({
        'available': bool(pressure),
        'pressure': pressure or {},
        'whatif': artifact,
    })
