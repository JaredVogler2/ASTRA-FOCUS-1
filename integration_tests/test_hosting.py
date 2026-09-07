"""Deployment gates: standalone source, offline assets, and real hosted login."""
import json
import re
from pathlib import Path
import pytest
from conftest import login
from werkzeug.security import check_password_hash


def test_hosted_account_login_and_restart(app, monkeypatch, tmp_path):
    monkeypatch.setenv('ASTRA_DEMO', '0')
    monkeypatch.setenv('ASTRA_ADMIN_USERNAME', 'owner')
    monkeypatch.setenv('ASTRA_ADMIN_PASSWORD', 'fixture-secret-12345')
    monkeypatch.setenv('RENDER_EXTERNAL_URL', 'https://focus.example')
    from astra_focus.app import create_app
    hosted = create_app()
    client = hosted.test_client()
    origin = 'https://focus.example'
    # The proxy receives HTTPS; the application connection itself is HTTP.
    base = 'http://focus.example'
    page = client.get('/sign-in', base_url=base)
    assert b'name="role"' not in page.data
    assert 'Secure' in page.headers['Set-Cookie']
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    form = {'username': 'owner', 'password': 'fixture-secret-12345',
            'csrf_token': token, 'role': 'mechanic'}
    assert client.post('/sign-in', base_url=base, data=form,
                       headers={'Origin': 'https://foreign.example'}).status_code == 403
    assert client.post('/sign-in', base_url=base, data=form,
                       headers={'Origin': origin}).status_code == 302
    # Read the server-owned identity from the authenticated overview shell.
    assert client.get('/', base_url=base).status_code == 200
    identity = client.get('/api/astra/bootstrap', base_url=base).get_json()
    assert identity['role'] == 'director' and identity['demo'] is False
    users_path = tmp_path / 'users.json'
    original = users_path.read_bytes()
    users = json.loads(original)
    assert users['owner']['role'] == 'director'
    assert check_password_hash(users['owner']['password_hash'], form['password'])
    assert form['password'].encode() not in original
    assert users_path.stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv('ASTRA_ADMIN_PASSWORD', 'different-restart-secret')
    create_app()
    assert users_path.read_bytes() == original


def test_bootstrap_rejects_missing_credentials(tmp_path, monkeypatch):
    from astra_focus.bootstrap import bootstrap_account
    for key in ['ASTRA_USERS_FILE', 'ASTRA_ADMIN_USERNAME', 'ASTRA_ADMIN_PASSWORD']:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match='ASTRA_ADMIN_USERNAME'):
        bootstrap_account(tmp_path)
    assert not (tmp_path / 'users.json').exists()


def test_original_dashboards_have_no_remote_asset_requirement(app, monkeypatch):
    def network_forbidden(*args, **kwargs):
        raise AssertionError('Standalone dashboard attempted an external request')
    monkeypatch.setattr('urllib.request.urlopen', network_forbidden)
    from astra_focus.app import create_app
    from astra_focus.runtime import FF_ROOT
    monkeypatch.setenv('ASTRA_LEGACY', '1')
    standalone = create_app()
    client = standalone.test_client()
    login(client)
    for route in ['/dashboard', '/dashboard/classic']:
        page = client.get(route)
        assert page.status_code == 200
        assets = re.findall(r'(?:src|href)=["\']([^"\']+)["\']', page.text)
        assert not any(a.startswith(('https://', 'http://', '//')) for a in assets)
        local = [a for a in assets if a.startswith('/static/')]
        assert any('/vendor/' in a for a in local)
        for asset in local:
            assert client.get(asset).status_code == 200, asset
    assert (FF_ROOT / 'ff/engine/scheduler.py').is_file()
    assert FF_ROOT.name == 'FF_app' and FF_ROOT.parent.name != 'upstream'


def test_sample_data_generation_does_not_enable_demo_personas(app, monkeypatch, tmp_path):
    monkeypatch.setenv('ASTRA_DEMO', '0')
    monkeypatch.setenv('ASTRA_GENERATE_SAMPLE_DATA', '1')
    monkeypatch.setenv('ASTRA_ADMIN_USERNAME', 'sample-owner')
    monkeypatch.setenv('ASTRA_ADMIN_PASSWORD', 'fixture-sample-secret')
    monkeypatch.setenv('ASTRA_AIRCRAFT', '1')
    monkeypatch.delenv('FF_DATA')
    from astra_focus.app import create_app
    hosted = create_app()
    assert hosted.extensions['ff']['mock_data'] is True
    assert hosted.extensions['astra_demo'] is False
    assert b'name="password"' in hosted.test_client().get('/sign-in').data
    assert (tmp_path / 'fleet.json.gz').is_file()
