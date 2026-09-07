"""FF bridge (INCREMENT 6 Part C) — thin server-side proxy to the FF_app
backend, mounted at /api/ff.

The vendored FOCUS dashboard is envelope-driven and stateless; FF_app
(ff/web on FF_API_BASE, default http://127.0.0.1:8080) owns the live
state, the point engine, progression, excusals, the LLM explain surface
and — per OR-6 — the ONLY actuals write-path.  This blueprint forwards a
fixed allow-list of routes to that backend using a server-side service
session and returns the FF responses (status + JSON body) verbatim, so
the four new insight tabs and the My Day write-back never talk to a
second write-path and never invent data the FF backend did not serve.

Proxied surface (nothing else — the allow-list IS the contract):

- GET  /api/ff/points/shift          -> GET  /api/v1/points/shift
- GET  /api/ff/points/leaderboard    -> GET  /api/v1/points/leaderboard
- GET  /api/ff/scorecard             -> GET  /api/v1/scorecard
- GET  /api/ff/progression/team/<t>  -> GET  /api/v1/progression/team/<t>
- GET  /api/ff/recap                 -> GET  /api/v1/recap
- GET  /api/ff/excusals              -> GET  /api/v1/excusals
- GET  /api/ff/explain/candidates    -> GET  /api/v1/explain/candidates
- POST /api/ff/excusals              -> POST /api/v1/excusals   (write 1)
- POST /api/ff/actuals               -> POST /api/v1/actuals    (write 2,
      OR-6: THE single actuals path — this proxy adds no second one)
- POST /api/ff/replan                -> POST /api/v1/replan     (write 3;
      FF /replan re-exports the max_v1 envelope, then this bridge runs
      the local refresh — current_app.reload_schedules(), the exact body
      of POST /api/refresh — so the dashboard picks the new envelope up)

Write bodies are forwarded verbatim EXCEPT the documented identity
translation (Part-A BEMS mapping): the envelope speaks digit BEMS +
``taskId = f"{soi}_{line}"``; the FF backend speaks FF ``task_id``
(= soi) + FF mech ids.  ``_translate_actuals_body`` maps ``taskId`` ->
``task_id`` (via the per-task ``ff_task_id`` ride-along, fallback: strip
the ``_<line>`` suffix) and digit BEMS -> FF mech id (via the top-level
``ff_mechanic_map`` ride-along).

Auth: a SERVICE session (POST /login role=director scope=all on first
use, re-login once on 401).  Per-user pass-through auth is a future
hardening item — documented in web_flask/VENDOR_CHANGES.md.

Honesty: an unreachable/timed-out FF backend is an HTTP 502
``{"error", "code": "ff_backend_down"}`` — never a fake 200, never
cached stand-in data.  FF error statuses (400/401/403/404/409/...) pass
through untouched so e.g. the notes-required 400 and the done-reopen
409 reach the client exactly as the FF backend emitted them.

Stdlib + flask only (FF_app runtime rule): urllib.request +
http.cookiejar carry the service session; no third-party HTTP client.
"""

import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

from flask import Blueprint, current_app, jsonify, request

ff_bridge_bp = Blueprint('ff_bridge', __name__, url_prefix='/api/ff')

# Timeouts (seconds) — a hung FF backend must surface, not hang the tab.
GET_TIMEOUT = 30
WRITE_TIMEOUT = 60
REPLAN_TIMEOUT = 600   # fleet50 replan incl. envelope re-export is ~15-40s

_SERVICE_LOGIN = {'role': 'director', 'scope': 'all'}

# One opener (cookie jar = FF session) per FF_API_BASE value, so tests
# pointing the bridge at different backends never share sessions.
_openers = {}
_openers_lock = threading.Lock()


class FFBridgeError(Exception):
    """FF backend unreachable / service login failed -> honest 502."""

    def __init__(self, message, code='ff_backend_down'):
        super().__init__(message)
        self.code = code


def _base():
    """FF backend base URL — read per request so tests can repoint it."""
    return os.environ.get('FF_API_BASE', 'http://127.0.0.1:8080').rstrip('/')


def _opener(base):
    with _openers_lock:
        opener = _openers.get(base)
        if opener is None:
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(CookieJar()))
            _openers[base] = opener
        return opener


def _raw_request(opener, method, url, payload, timeout):
    """One HTTP round trip -> (status, parsed-JSON-or-{'error': text}).

    urllib raises HTTPError on 4xx/5xx; both paths funnel through the
    same body parse so FF's JSON error envelopes pass through verbatim.
    """
    data = None
    headers = {'Accept': 'application/json'}
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read()
            status = resp.status
    except urllib.error.HTTPError as err:      # 4xx/5xx WITH a response
        body = err.read()
        status = err.code
    except (urllib.error.URLError, OSError, TimeoutError) as err:
        raise FFBridgeError(
            f'FF backend unreachable at {url}: {err}') from err
    try:
        parsed = json.loads(body.decode('utf-8')) if body else {}
    except (ValueError, UnicodeDecodeError):
        parsed = {'error': body.decode('utf-8', 'replace')[:500]}
    if not isinstance(parsed, (dict, list)):
        parsed = {'value': parsed}
    return status, parsed


def _service_login(opener, base):
    """POST /login as the service persona; raise honestly on failure."""
    status, body = _raw_request(
        opener, 'POST', base + '/login', _SERVICE_LOGIN, GET_TIMEOUT)
    if status != 200:
        detail = body.get('error') if isinstance(body, dict) else body
        raise FFBridgeError(
            f'FF service login failed ({status}): {detail}',
            code='ff_login_failed')


def _ff_call(method, path, payload=None, timeout=GET_TIMEOUT,
             query_string=b''):
    """Proxy one call, holding the service session (re-login once on 401)."""
    base = _base()
    opener = _opener(base)
    url = base + path
    if query_string:
        url += '?' + query_string.decode('latin-1')
    status, body = _raw_request(opener, method, url, payload, timeout)
    if status == 401:  # no/expired service session — login and retry ONCE
        _service_login(opener, base)
        status, body = _raw_request(opener, method, url, payload, timeout)
    return status, body


@ff_bridge_bp.errorhandler(FFBridgeError)
def _handle_bridge_error(err):
    return jsonify({'error': str(err), 'code': err.code}), 502


def _proxy_get(path, timeout=GET_TIMEOUT):
    status, body = _ff_call('GET', path, timeout=timeout,
                            query_string=request.query_string)
    return jsonify(body), status


def _json_body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return None
    return body


# ---------------------------------------------------------------------------
# identity translation (Part-A ride-alongs: ff_task_id / ff_mechanic_map)
# ---------------------------------------------------------------------------


def _envelope():
    return getattr(current_app, 'current_schedule_data', None) or {}


_LINE_SUFFIX = re.compile(r'_\d+$')


def translate_task_id(value):
    """Envelope taskId (``{soi}_{line}``) -> FF task_id (= soi).

    Prefers the exported per-task ``ff_task_id`` ride-along; falls back
    to stripping the ``_<line>`` suffix (FF task ids contain no
    underscore, so the strip is unambiguous).  A value that is already
    an FF task_id passes through unchanged.
    """
    v = str(value or '')
    for t in _envelope().get('tasks') or []:
        if t.get('taskId') == v:
            ff = t.get('ff_task_id')
            return str(ff) if ff else _LINE_SUFFIX.sub('', v)
    return _LINE_SUFFIX.sub('', v) if _LINE_SUFFIX.search(v) else v


def translate_bems(value):
    """Digit BEMS -> FF mech id via the envelope's ff_mechanic_map, or None."""
    mapping = _envelope().get('ff_mechanic_map') or {}
    return mapping.get(str(value or ''))


def _translate_actuals_body(body):
    """Map dashboard identity (taskId / BEMS) to FF identity, verbatim rest."""
    out = dict(body)
    task_id = out.pop('taskId', None)
    if 'task_id' in out:
        out['task_id'] = translate_task_id(out['task_id'])
    elif task_id is not None:
        out['task_id'] = translate_task_id(task_id)
    bems = out.pop('bems', None)
    if bems is not None:
        ff_mech = translate_bems(bems)
        if ff_mech:
            # Ride-along context for the FF audit trail (FF /actuals
            # ignores unknown keys; state truth is task_id + state).
            out['mechanic_id'] = ff_mech
    return out


# ---------------------------------------------------------------------------
# read proxies (GG-1: the game tabs are read-only — GET all the way down)
# ---------------------------------------------------------------------------


@ff_bridge_bp.route('/points/shift')
def points_shift():
    return _proxy_get('/api/v1/points/shift')


@ff_bridge_bp.route('/points/leaderboard')
def points_leaderboard():
    return _proxy_get('/api/v1/points/leaderboard')


@ff_bridge_bp.route('/scorecard')
def scorecard():
    # Fortnight increment: graded execution history (slice x period +
    # trends). Read-only aggregate data; query string passes through.
    return _proxy_get('/api/v1/scorecard')


@ff_bridge_bp.route('/progression/team/<team>')
def progression_team(team):
    return _proxy_get('/api/v1/progression/team/'
                      + urllib.parse.quote(team, safe=''))


@ff_bridge_bp.route('/recap')
def recap():
    return _proxy_get('/api/v1/recap')


@ff_bridge_bp.route('/excusals', methods=['GET'])
def excusals_get():
    return _proxy_get('/api/v1/excusals')


@ff_bridge_bp.route('/explain/candidates')
def explain_candidates():
    # LB-8 lives in the FF backend: the payload ALWAYS carries the
    # deterministic explanation; 'llm-validated' only when a narrative
    # passed the full validator pipeline. The bridge adds nothing.
    return _proxy_get('/api/v1/explain/candidates')


# ---------------------------------------------------------------------------
# write proxies — the ONLY three, forwarded to the FF single write-paths
# ---------------------------------------------------------------------------


@ff_bridge_bp.route('/excusals', methods=['POST'])
def excusals_post():
    body = _json_body()
    if body is None:
        return jsonify({'error': 'request body must be a JSON object',
                        'code': 'invalid_json'}), 400
    if 'task_id' in body or 'taskId' in body:
        body = dict(body)
        raw = body.pop('taskId', None)
        body['task_id'] = translate_task_id(body.get('task_id', raw))
    status, resp = _ff_call('POST', '/api/v1/excusals', payload=body,
                            timeout=WRITE_TIMEOUT)
    return jsonify(resp), status


@ff_bridge_bp.route('/actuals', methods=['POST'])
def actuals_post():
    """OR-6: forwarded to FF POST /api/v1/actuals — the single write-path.

    The 409 done-reopen guard passes through untouched; the client
    re-posts with force=true only after the user confirms.
    """
    body = _json_body()
    if body is None:
        return jsonify({'error': 'request body must be a JSON object',
                        'code': 'invalid_json'}), 400
    status, resp = _ff_call('POST', '/api/v1/actuals',
                            payload=_translate_actuals_body(body),
                            timeout=WRITE_TIMEOUT)
    return jsonify(resp), status


@ff_bridge_bp.route('/replan', methods=['POST'])
def replan_post():
    """FF replan (re-exports the envelope) + the LOCAL dashboard refresh.

    FF's /replan rebuilds schedule/snapshot and writes a fresh
    max_v1_*.json.gz into its envelope dir (this dashboard's schedules
    dir).  On FF success the bridge immediately runs
    ``current_app.reload_schedules()`` — the exact body of
    ``POST /api/refresh`` — so the new envelope is live before the
    client's next read.  The response is FF's replan payload plus two
    honest bridge fields: ``dashboard_refreshed`` and
    ``current_schedule``.
    """
    status, resp = _ff_call('POST', '/api/v1/replan', payload={},
                            timeout=REPLAN_TIMEOUT)
    refreshed = False
    refresh_error = None
    if status == 200:
        reload_schedules = getattr(current_app, 'reload_schedules', None)
        if reload_schedules is None:
            refresh_error = 'reload hook unavailable'
        else:
            try:
                refreshed = bool(reload_schedules())
            except Exception as exc:  # honest, never a fake success
                refresh_error = f'{type(exc).__name__}: {exc}'
    if isinstance(resp, dict):
        resp = dict(resp)
        resp['dashboard_refreshed'] = refreshed
        if refresh_error:
            resp['dashboard_refresh_error'] = refresh_error
        resp['current_schedule'] = getattr(
            current_app, 'current_schedule_file', None)
    return jsonify(resp), status
