"""Server-owned identity and CSRF; demo personas never become production auth."""
import json
import os
import secrets
import time
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit
from flask import g, jsonify, redirect, request, session
from werkzeug.security import check_password_hash

def install_security(app, demo, store):
    public_origin = (os.environ.get('ASTRA_PUBLIC_ORIGIN') or
                     os.environ.get('RENDER_EXTERNAL_URL', '')).rstrip('/')
    if public_origin:
        origin_parts = urlsplit(public_origin)
        if (origin_parts.scheme != 'https' or not origin_parts.netloc or
                origin_parts.path or origin_parts.query or origin_parts.fragment or
                origin_parts.username or origin_parts.password):
            raise RuntimeError('ASTRA_PUBLIC_ORIGIN must be an HTTPS origin without a path or credentials.')
    app.config.update(SESSION_COOKIE_NAME='astra_session', SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=not demo,
                      PERMANENT_SESSION_LIFETIME=28800, MAX_CONTENT_LENGTH=2*1024*1024)
    users_path = os.environ.get('ASTRA_USERS_FILE')
    users = json.loads(Path(users_path).read_text()) if users_path else {}
    if not demo and not users:
        raise RuntimeError('ASTRA_USERS_FILE with server-assigned roles and password hashes is required.')
    for username, user in users.items():
        if not isinstance(user, dict) or user.get('role') not in {'mechanic','lead','flm','super','director','vp'} or not isinstance(user.get('scope'),str) or not str(user.get('password_hash','')).startswith(('scrypt:', 'pbkdf2:')):
            raise RuntimeError('Invalid user configuration for '+username)
    app.extensions['astra_users'] = users
    app.extensions['astra_attempts'] = {}

    @app.before_request
    def boundary():
        path = request.path
        public = path in {'/sign-in', '/healthz', '/readyz'} or path.startswith('/astra-static/')
        if path == '/login':
            return redirect('/sign-in', code=303)
        if path == '/logout':
            return jsonify(error='Use the sign-out button.', code='post_required'),405
        if not public:
            if not session.get('username'):
                return (jsonify(error='Sign in to continue.',code='unauthenticated'),401) if path.startswith('/api/') else redirect('/sign-in')
            if not demo:
                user = users.get(session['username'])
                if not user:
                    session.clear()
                    return jsonify(error='Account no longer available.',code='unauthenticated'),401
                # Scope comes from the server on every request, never a browser role selector.
                session['role'], session['scope'] = user['role'], user['scope']
        if request.method not in {'GET','HEAD','OPTIONS'}:
            supplied = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token','')
            expected = session.get('csrf_token','')
            if not expected or not secrets.compare_digest(str(supplied),str(expected)):
                return jsonify(error='Session expired. Refresh and try again.',code='csrf_invalid'),403
            origin = request.headers.get('Origin')
            if origin and origin != (public_origin or request.host_url.rstrip('/')):
                return jsonify(error='Cross-origin writes are rejected.',code='origin_invalid'),403
        g.actor = session.get('username','anonymous')

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['Cache-Control'] = 'no-store' if not request.path.startswith('/astra-static/') else 'public, max-age=3600'
        if request.path in {'/', '/sign-in'}:
            response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'self'"
        return response

def authenticate(app, username, password):
    attempts = app.extensions['astra_attempts']
    key = request.remote_addr or 'unknown'
    stamp = time.monotonic()
    attempts[key] = [t for t in attempts.get(key,[]) if stamp-t < 300]
    if len(attempts[key]) >= 10:
        return None, 'Too many attempts. Try again in five minutes.'
    user = app.extensions['astra_users'].get(username)
    dummy = 'scrypt:32768:8:1$notARealSalt$'+'0'*128
    if not check_password_hash(user['password_hash'] if user else dummy, password):
        attempts[key].append(stamp)
        return None, 'Username or password is incorrect.'
    attempts.pop(key,None)
    return user, None
