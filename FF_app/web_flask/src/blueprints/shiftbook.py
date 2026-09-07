"""Shift Book (team lead) + My Day (mechanic) APIs — NEW tabs.

Both read straight off the loaded envelope.  The Shift Book is the
team lead's stable book for one (team, day, shift); My Day is one
mechanic's task card for a day, plus the next-day preview the
commitment layer makes safe to show.  Additive only.
"""

from collections import defaultdict

from flask import Blueprint, jsonify, current_app, request

shiftbook_bp = Blueprint('shiftbook', __name__)

_SYNTH_PREFIXES = ('QA-', 'FALLBACK', 'CC-', 'VENDOR')


def _env():
    return getattr(current_app, 'current_schedule_data', None) or {}


def _tasks():
    return _env().get('tasks') or []


def _today_and_shift():
    env = _env()
    today = int(env.get('todaySlot', 0)) // 3
    return today, int(env.get('shift_number', 1) or 1)


def _is_real_mech(mid):
    s = str(mid or '')
    return s.isdigit()


def _task_row(t):
    return {
        'taskId': t.get('taskId'),
        'line': t.get('line_number'),
        'team': t.get('team'),
        'skill': t.get('skill'),
        'day': t.get('day'),
        'shift': t.get('shift'),
        'startMinute': t.get('start_minute'),
        'endMinute': t.get('end_minute'),
        'startTime': t.get('startTime'),
        'endTime': t.get('endTime'),
        'durationMinutes': t.get('duration_minutes'),
        'mechanicIds': t.get('mechanicIds') or [],
        'mechanics': t.get('mechanics'),
        'state': t.get('state'),
        'isCritical': bool(t.get('isCritical')),
        'isInspection': bool(t.get('is_inspection')),
        'placedBy': t.get('placedBy'),
    }


@shiftbook_bp.route('/api/shiftbook/options')
def shiftbook_options():
    today, cur_shift = _today_and_shift()
    teams = sorted({t.get('team') for t in _tasks()
                    if t.get('team') and not str(t.get('team')).startswith(
                        _SYNTH_PREFIXES)})
    return jsonify({'teams': teams, 'todayDay': today,
                    'currentShift': cur_shift})


@shiftbook_bp.route('/api/shiftbook')
def shiftbook():
    today, cur_shift = _today_and_shift()
    team = request.args.get('team') or ''
    try:
        day = int(request.args.get('day', today))
        shift = int(request.args.get('shift', cur_shift))
    except (TypeError, ValueError):
        day, shift = today, cur_shift
    rows = [
        _task_row(t) for t in _tasks()
        if t.get('team') == team and int(t.get('day', -1)) == day
        and int(t.get('shift', 0)) == shift]
    rows.sort(key=lambda r: (r['startMinute'] or 0, r['taskId'] or ''))
    by_mech = defaultdict(list)
    for r in rows:
        for m in (r['mechanicIds'] or ['UNASSIGNED']):
            by_mech[str(m)].append(r['taskId'])
    return jsonify({
        'team': team, 'day': day, 'shift': shift,
        'tasks': rows,
        'mechanics': [{'bems': m, 'taskIds': ids, 'count': len(ids)}
                      for m, ids in sorted(by_mech.items())],
        'counts': {
            'total': len(rows),
            'critical': sum(1 for r in rows if r['isCritical']),
            'inProgress': sum(1 for r in rows
                              if r['state'] == 'in_progress'),
            'blocked': sum(1 for r in rows if r['state'] == 'blocked'),
        },
    })


@shiftbook_bp.route('/api/myday/options')
def myday_options():
    today, cur_shift = _today_and_shift()
    mechs = sorted({str(m) for t in _tasks()
                    for m in (t.get('mechanicIds') or [])
                    if _is_real_mech(m)})
    return jsonify({'mechanics': mechs, 'todayDay': today,
                    'currentShift': cur_shift})


@shiftbook_bp.route('/api/myday')
def myday():
    today, _ = _today_and_shift()
    bems = str(request.args.get('bems') or '')
    try:
        day = int(request.args.get('day', today))
    except (TypeError, ValueError):
        day = today

    def day_rows(d):
        rows = [_task_row(t) for t in _tasks()
                if int(t.get('day', -1)) == d
                and bems in [str(m) for m in (t.get('mechanicIds') or [])]]
        rows.sort(key=lambda r: (r['startMinute'] or 0, r['taskId'] or ''))
        return rows

    # next-day preview: first later day this mechanic has work
    preview_day, preview = None, []
    for d in range(day + 1, day + 8):
        rows = day_rows(d)
        if rows:
            preview_day, preview = d, rows
            break

    rows = day_rows(day)
    return jsonify({
        'bems': bems, 'day': day,
        'tasks': rows,
        'totalMinutes': sum(r['durationMinutes'] or 0 for r in rows),
        'previewDay': preview_day,
        'preview': preview,
    })
