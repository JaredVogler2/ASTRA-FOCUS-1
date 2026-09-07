import copy
import json
import threading
import pytest
from conftest import login,post

def test_dashboard_uses_real_engine_state(client,app):
    """Every headline comes from the original FF state, with mock labeling."""
    assert client.get('/').status_code==200
    s=client.get('/api/astra/overview').get_json()
    assert s['total']==len(app.extensions['ff']['fleet'].tasks)==120
    assert s['validator_total']==0 and s['mock_data'] is True
    assert client.get('/astra-static/app.js').status_code==200
    assert client.get('/astra-static/app.css').status_code==200
    assert client.get('/api/astra/tasks?size=10').get_json()['total']==120

def test_unauthenticated_and_csrf_writes_rejected(app,client):
    """A real user session and CSRF token are required for task updates."""
    assert app.test_client().get('/api/astra/tasks').status_code==401
    tid=next(iter(app.extensions['ff']['task_by_id']))
    assert client.post('/api/v1/actuals',json={'task_id':tid,'state':'done'}).status_code==403
    r=post(client,'/api/v1/actuals',{'task_id':tid,'state':'done'},headers={'Origin':'https://foreign.example'})
    assert r.status_code==403
    assert app.test_client().post('/login',json={'role':'director','scope':'all'}).status_code==303

def test_lead_scope_tampering_cannot_widen_access(app):
    """RS: query parameters cannot widen the server-owned role scope."""
    c=app.test_client(); team=app.extensions['ff']['teams'][0]; login(c,'lead',team)
    other=app.extensions['ff']['teams'][1]
    for path in ['tasks','roster','overview','export']:
        assert c.get('/api/astra/'+path+'?team='+other).status_code==403
    rows=c.get('/api/astra/tasks?size=200').get_json()['rows']
    assert rows and all(t['team']==team for t in rows)
    task=next(t for t in app.extensions['ff']['fleet'].tasks if t.team==other)
    assert post(c,'/api/v1/actuals',{'task_id':task.task_id,'state':'done'}).status_code==403

def test_mechanic_sees_only_own_tasks(app):
    """RS: mechanic detail and task lists expose only named assignments."""
    s=app.extensions['ff']; assignment=next(iter(s['schedule'].assignments.values()))
    mid=assignment.mechanic_ids[0]; c=app.test_client();login(c,'mechanic',mid)
    data=c.get('/api/astra/tasks?size=200').get_json()
    assert data['rows'] and all(mid in t['mechanics'] for t in data['rows'])
    outside=next(tid for tid,a in s['schedule'].assignments.items() if mid not in a.mechanic_ids)
    assert c.get('/api/astra/tasks/'+outside).status_code==403
    assert post(c,'/api/astra/roster',{'changes':[]}).status_code==403
    assert post(c,'/api/v1/replan',{}).status_code==403
    assert c.get('/api/astra/history').status_code==403

def test_actuals_replan_and_baseline_credit_survive(client,app):
    """GAMES-01: a completed task cannot disappear from its pinned shift baseline."""
    s=app.extensions['ff']; a=next(iter(s['schedule'].assignments.values()))
    args={'team':a.team,'day':a.day,'shift':a.shift}
    p=post(client,'/api/astra/baselines',args)
    assert p.status_code==200
    before=p.get_json(); assert before['pinned']
    update=post(client,'/api/v1/actuals',{'task_id':a.task_id,'state':'done','execution_day':a.day,'execution_shift':a.shift})
    assert update.status_code==200
    assert s['astra_stale']
    assert post(client,'/api/v1/replan',{}).status_code==200
    assert a.task_id not in s['schedule'].assignments
    after=client.get('/api/astra/baselines',query_string=args).get_json()
    row=next(r for r in after['rows'] if r['id']==a.task_id)
    assert row['done'] and after['completed']>=1
    assert after['plan_id']==before['plan_id']
    assert after['earned']>=row['points']
    assert s['astra_plan_id']>after['plan_id']
    assert post(client,'/api/astra/baselines',args).status_code==409

def test_completion_is_not_credited_to_wrong_shift(client,app):
    """Execution credit uses the recorded shift, never any later observed done state."""
    a=next(iter(app.extensions['ff']['schedule'].assignments.values()))
    args={'team':a.team,'day':a.day,'shift':a.shift}
    post(client,'/api/astra/baselines',args)
    post(client,'/api/v1/actuals',{'task_id':a.task_id,'state':'done','execution_day':a.day+1,'execution_shift':a.shift})
    result=client.get('/api/astra/baselines',query_string=args).get_json()
    assert not next(r for r in result['rows'] if r['id']==a.task_id)['done']

def test_stale_revision_and_idempotency(client,app):
    """A stale editor cannot overwrite newer work; repeated request keys do not double credit."""
    tid=next(iter(app.extensions['ff']['schedule'].assignments)); data={'task_id':tid,'state':'in_progress'}
    r=post(client,'/api/v1/actuals',data,revision=0,headers={'Idempotency-Key':'one-request'})
    assert r.status_code==200
    assert post(client,'/api/v1/actuals',{'task_id':tid,'state':'done'},revision=0).status_code==409
    again=post(client,'/api/v1/actuals',data,headers={'Idempotency-Key':'one-request'})
    assert again.status_code==200 and again.get_json()==r.get_json()
    assert post(client,'/api/v1/actuals',{'task_id':tid,'state':'blocked'},headers={'Idempotency-Key':'one-request'}).status_code==409

def test_done_reopen_stays_guarded(client,app):
    """OR-6: reopening completed work requires explicit force=true."""
    tid=next(iter(app.extensions['ff']['schedule'].assignments))
    assert post(client,'/api/v1/actuals',{'task_id':tid,'state':'done'}).status_code==200
    assert post(client,'/api/v1/actuals',{'task_id':tid,'state':'not_started'}).status_code==409
    assert post(client,'/api/v1/actuals',{'task_id':tid,'state':'not_started','force':'false'}).status_code==400
    assert post(client,'/api/v1/actuals',{'task_id':tid,'state':'not_started','force':True}).status_code==200

def test_roster_changes_preserve_team_ownership(client,app):
    """OR-2: roster configuration may never borrow a mechanic across teams."""
    m=app.extensions['astra_base_roster'][0]
    assert post(client,'/api/astra/roster',{'changes':[{'mech_id':m.mech_id,'team':'OTHER'}]}).status_code==400
    assert post(client,'/api/astra/roster',{'changes':[{'mech_id':m.mech_id,'shift':0}]}).status_code==400
    assert post(client,'/api/astra/roster',{'changes':[{'mech_id':m.mech_id,'available':False}]}).status_code==200
    assert app.extensions['astra_store'].setting('roster')[m.mech_id]['available'] is False
    assert post(client,'/api/v1/replan',{}).status_code==200
    s=app.extensions['ff']
    assert all(m.mech_id not in a.mechanic_ids for a in s['schedule'].assignments.values())
    assert s['validator_total']==0

def test_scenario_does_not_modify_live_state(client,app):
    """A what-if run evaluates a copy; live task and crew state never change."""
    s=app.extensions['ff']; old=copy.deepcopy(s['fleet'].to_dict()); plan=s['astra_plan_id']
    m=app.extensions['astra_base_roster'][0]
    response=post(client,'/api/astra/scenarios',{'changes':[{'mech_id':m.mech_id,'available':False}]})
    assert response.status_code==200
    assert response.get_json()['validator']['total']==0
    assert response.get_json()['applied'] is False
    assert s['fleet'].to_dict()==old and s['astra_plan_id']==plan

def test_persistence_of_notes_audit_and_baseline(client,app):
    """Operational records survive opening a new store connection."""
    a=next(iter(app.extensions['ff']['schedule'].assignments.values()))
    data={'team':a.team,'day':a.day,'shift':a.shift}
    assert post(client,'/api/astra/baselines',data).status_code==200
    assert post(client,'/api/astra/shift-notes',{**data,'note':'Waiting on verified tooling release.'}).status_code==200
    from astra_focus.store import Store
    store=Store(app.extensions['astra_store'].path)
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM baselines').fetchone()[0]==1
        assert db.execute('SELECT body FROM notes').fetchone()[0]=='Waiting on verified tooling release.'
        assert db.execute("SELECT COUNT(*) FROM audit WHERE action='baseline.pinned'").fetchone()[0]==1

def test_export_is_scoped_and_formula_safe(client,app):
    """CSV exports preserve scope and neutralize spreadsheet formula injection."""
    task=app.extensions['ff']['fleet'].tasks[0]; task.name='=HYPERLINK("bad")'
    response=client.get('/api/astra/export')
    assert response.status_code==200 and response.mimetype=='text/csv'
    assert "'=HYPERLINK" in response.text
    assert 'Synthetic' in response.text

def test_reads_reject_during_mutation(client,app):
    """Readers cannot observe a half-built in-process snapshot."""
    lock=app.extensions['astra_lock']; started=threading.Event(); release=threading.Event()
    def hold():
        with lock: started.set(); release.wait(3)
    t=threading.Thread(target=hold);t.start();started.wait(2)
    try: assert client.get('/api/astra/tasks').status_code==409
    finally: release.set();t.join()
    assert client.get('/api/astra/tasks').status_code==200

@pytest.mark.parametrize('path',['/api/astra/tasks?page=-1','/api/astra/tasks?size=201','/api/astra/overview?day=1.5','/api/astra/overview?shift=4'])
def test_invalid_parameters_fail_honestly(client,path):
    assert client.get(path).status_code==400

def test_failed_actuals_persistence_rolls_back_memory(client,app,monkeypatch):
    """A failed actuals file write cannot leave task memory ahead of persisted truth."""
    from ff.web import app as upstream
    tid=next(iter(app.extensions['ff']['schedule'].assignments)); task=app.extensions['ff']['task_by_id'][tid]
    before=task.state
    def fail(_): raise OSError('disk unavailable')
    monkeypatch.setattr(upstream,'persist_actuals',fail)
    response=post(client,'/api/v1/actuals',{'task_id':tid,'state':'done'})
    assert response.status_code==500 and task.state==before

def test_planning_day_advances_and_cannot_move_backwards(client,app):
    """Replanning uses the selected as-of day and cannot silently plan in the past."""
    assert post(client,'/api/v1/replan',{'start_day':2}).status_code==200
    state=app.extensions['ff']
    assert state['start_day']==2
    assert all(a.day>=2 for a in state['schedule'].assignments.values())
    assert app.extensions['astra_store'].setting('planning_day')==2
    assert post(client,'/api/v1/replan',{'start_day':1}).status_code==400

def test_restart_preserves_actuals_and_shift_credit(client,app):
    """The pinned baseline and observed completions survive application restart."""
    state=app.extensions['ff']; a=next(iter(state['schedule'].assignments.values()))
    args={'team':a.team,'day':a.day,'shift':a.shift}
    post(client,'/api/astra/baselines',args)
    post(client,'/api/v1/actuals',{'task_id':a.task_id,'state':'done','execution_day':a.day,'execution_shift':a.shift})
    from astra_focus.app import create_app
    restarted=create_app(); c=restarted.test_client();login(c)
    assert restarted.extensions['ff']['task_by_id'][a.task_id].state=='done'
    result=c.get('/api/astra/baselines',query_string=args).get_json()
    assert next(r for r in result['rows'] if r['id']==a.task_id)['done']

def test_production_roles_are_server_assigned(app,monkeypatch,tmp_path):
    """Production users cannot become directors by submitting a role or scope."""
    from werkzeug.security import generate_password_hash
    from astra_focus.app import create_app
    users=tmp_path/'accounts.json'
    users.write_text(json.dumps({'lead-user':{'password_hash':generate_password_hash('test-password-long'),'role':'lead','scope':'T01'}}))
    monkeypatch.setenv('ASTRA_DEMO','0');monkeypatch.setenv('ASTRA_USERS_FILE',str(users))
    production=create_app();c=production.test_client();c.get('/sign-in')
    with c.session_transaction() as s:token=s['csrf_token']
    bad=c.post('/sign-in',data={'csrf_token':token,'username':'lead-user','password':'wrong','role':'director','scope':'all'})
    assert b'incorrect' in bad.data
    good=c.post('/sign-in',data={'csrf_token':token,'username':'lead-user','password':'test-password-long','role':'director','scope':'all'})
    assert good.status_code==302
    with c.session_transaction() as s: assert s['role']=='lead' and s['scope']=='T01'
    assert production.config['SESSION_COOKIE_SECURE'] is True

def test_legacy_views_and_bridge_use_current_identity(app,monkeypatch):
    """The preserved FOCUS comparison never uses a shared director service session."""
    from astra_focus.app import create_app
    monkeypatch.setenv('ASTRA_LEGACY','1')
    combined=create_app(); c=combined.test_client();login(c)
    assert c.get('/dashboard').status_code==200
    assert c.get('/dashboard/classic').status_code==200
    assert c.get('/api/scenario/3stage').status_code==200
    assert c.get('/api/ff/points/shift?team=T01&day=0&shift=1').status_code==200
    assert b'Read-only' in c.get('/dashboard').data
    for name in ['src.blueprints.ff_bridge','ff.web.app']:
        assert name in __import__('sys').modules
    login(c,'lead','T01')
    assert c.get('/dashboard').status_code==403
    assert c.get('/api/scenario/3stage').status_code==403

def test_bad_validator_result_never_replaces_visible_plan(client,app,monkeypatch):
    """Independent validator failures cannot publish a new accepted plan."""
    from ff.web import app as upstream
    old_id=app.extensions['ff']['astra_plan_id'];old_snapshot=app.extensions['ff']['snapshot_id']
    original=upstream.rebuild_state
    def fail(state): original(state);state['validator_total']=1
    monkeypatch.setattr(upstream,'rebuild_state',fail)
    response=post(client,'/api/v1/replan',{})
    assert response.status_code==503
    assert app.extensions['ff']['astra_plan_id']==old_id
    assert app.extensions['ff']['snapshot_id']==old_snapshot
