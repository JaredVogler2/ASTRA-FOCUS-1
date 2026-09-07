import os
import sys
import re
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'upstream'/'FF_app'))

@pytest.fixture
def app(tmp_path,monkeypatch):
    for key in ['FF_ACTUALS','FF_EXCUSALS','FF_COMMITMENTS','FF_GAME_EVENTS','FF_LLM_AUDIT','FF_ENVELOPE_DIR','MAX_SCHEDULES_DIR','FF_SCHEDULE','FF_SCORECARD','ASTRA_USERS_FILE']:
        monkeypatch.delenv(key,raising=False)
    monkeypatch.setenv('ASTRA_DEMO','1')
    monkeypatch.setenv('ASTRA_LEGACY','0')
    monkeypatch.setenv('FF_SECRET_KEY','astra-tests-only-secret')
    monkeypatch.setenv('ASTRA_STATE_DIR',str(tmp_path))
    monkeypatch.setenv('FF_DATA',str(ROOT/'upstream/FF_app/data/mini/fleet.json.gz'))
    from astra_focus.app import create_app
    result=create_app()
    result.config['TESTING']=True
    return result

@pytest.fixture
def client(app):
    result=app.test_client()
    login(result)
    return result

def login(client,role='director',scope='all'):
    response=client.get('/sign-in')
    with client.session_transaction() as s: csrf=s['csrf_token']
    return client.post('/sign-in',data={'role':role,'scope':scope,'csrf_token':csrf})

def post(client,path,data,revision=None,headers=None):
    with client.session_transaction() as s: csrf=s['csrf_token']
    hs={'X-CSRF-Token':csrf}
    if revision is not None: hs['X-Astra-Revision']=str(revision)
    hs.update(headers or {})
    return client.post(path,json=data,headers=hs)
