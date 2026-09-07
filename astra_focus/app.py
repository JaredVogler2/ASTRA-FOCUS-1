"""One authenticated FOCUS application, using FF's live state and write paths."""
import copy
import hashlib
import json
import os
import secrets
import threading
from collections import Counter
from pathlib import Path
from flask import Blueprint, g, jsonify, redirect, render_template, request, session
from jinja2 import ChoiceLoader, FileSystemLoader
from .runtime import configure, ROOT
from .store import Store, now
from .security import install_security, authenticate
from . import views

def create_app():
    state_dir, demo = configure()
    from ff.web.app import create_app as upstream_app, resolve_scope, team_allowed, rebuild_state
    from ff.web.api import ApiError, _check_task_scope
    from ff.domain import Fleet, Mechanic
    from ff.data.loader import save_json_gz
    app=upstream_app()
    state=app.extensions['ff']
    if state.get('snapshot') is None:
        raise RuntimeError('FF engine failed to initialize: '+str(state.get('boot_error')))
    if demo and not state['mock_data']:
        raise RuntimeError('Demo persona access is only permitted with synthetic data.')
    if state['validator_total']:
        raise RuntimeError('Refusing to serve a schedule with validator violations.')
    store=Store(state_dir/'astra.sqlite3')
    app.extensions['astra_store']=store
    app.extensions['astra_demo']=demo
    lock=threading.RLock()
    app.extensions['astra_lock']=lock
    state['astra_revision']=store.setting('revision',0)
    state['astra_stale']=False
    state['astra_roster']=store.setting('roster',{})
    state['start_day']=store.setting('planning_day',0)
    base_roster=copy.deepcopy(state['fleet'].mechanics)
    app.extensions['astra_base_roster']=base_roster

    def apply_roster():
        active=[]
        for m in base_roster:
            settings=state['astra_roster'].get(m.mech_id,{})
            if settings.get('available',True):
                item=copy.deepcopy(m)
                item.shift=settings.get('shift',m.shift)
                active.append(item)
        state['fleet'].mechanics=active
        # Preserve roster identity for absent mechanics; scheduling sees active crew only.
        state['mech_by_id']={m.mech_id:m for m in base_roster}
    apply_roster()
    if state['astra_roster'] or state['start_day']:
        rebuild_state(state)
    state['astra_plan_id']=store.save_plan(state)
    state['astra_own_history']={}
    # Completed work stays visible to its original mechanic after a replan/restart.
    with store.connect() as db:
        for row in db.execute("SELECT payload FROM audit WHERE action='actuals.commit'"):
            e=json.loads(row[0])
            for mid in e.get('mechanics',[]):
                state['astra_own_history'].setdefault(mid,set()).add(e['task_id'])
    install_security(app,demo,store)
    app.jinja_loader=ChoiceLoader([FileSystemLoader(str(ROOT/'astra_focus'/'templates')),app.jinja_loader])
    assets=Blueprint('astra_assets',__name__,static_folder='static',static_url_path='/astra-static')
    app.register_blueprint(assets)

    def error(message,code='invalid_request',status=400):
        return jsonify(error=message,code=code),status

    def auth(manager=False):
        sc=resolve_scope(state)
        if sc is None: raise ApiError(401,'unauthenticated','Sign in to continue.')
        if manager and sc['role']=='mechanic': raise ApiError(403,'forbidden_role','A team lead or manager is required.')
        return sc

    def team_scope(sc,value):
        if value not in state['teams']: raise ApiError(404,'unknown_team','Unknown team.')
        if not team_allowed(sc,value): raise ApiError(403,'forbidden_scope','Team is outside your scope.')
        return value

    def integer(value,default,lo,hi):
        if value is None or value=='': return default
        if isinstance(value,bool): raise ApiError(400,'invalid_parameter','Expected a whole number.')
        try: result=int(value)
        except (TypeError,ValueError): raise ApiError(400,'invalid_parameter','Expected a whole number.')
        if str(value)!=str(result) or not lo<=result<=hi: raise ApiError(400,'invalid_parameter',f'Expected an integer from {lo} to {hi}.')
        return result

    def body():
        data=request.get_json(silent=True)
        if not isinstance(data,dict): raise ApiError(400,'invalid_json','A JSON object is required.')
        return data

    @app.errorhandler(ApiError)
    def api_error(exc): return error(exc.message,exc.code,exc.status)

    @app.before_request
    def consistent_state():
        if request.path.startswith('/api/') or request.path=='/':
            if not lock.acquire(blocking=False):
                return error('The schedule is being updated. Please retry.','state_busy',409)
            g.astra_lock=True
        if request.method=='POST' and request.path.startswith('/api/'):
            supplied=request.headers.get('X-Astra-Revision')
            if supplied is not None and supplied != str(state['astra_revision']):
                return error('The workspace changed. Refresh before applying your update.','stale_revision',409)

    @app.teardown_request
    def unlock(_exc):
        if getattr(g,'astra_lock',False):
            g.astra_lock=False
            lock.release()

    @app.route('/sign-in',methods=['GET','POST'])
    def sign_in():
        session.setdefault('csrf_token',secrets.token_urlsafe(32))
        failure=None
        if request.method=='POST':
            if demo:
                role=request.form.get('role','director'); scope=request.form.get('scope','all')
                session['role']=role; session['scope']='all' if role in {'director','vp'} else scope
                if resolve_scope(state) is None:
                    failure='Select a valid person or team for this role.'
                    session.pop('role',None); session.pop('scope',None)
                else:
                    identity={'role':session['role'],'scope':session['scope']}
                    username='demo:'+role+':'+identity['scope']
            else:
                username=request.form.get('username','').strip()
                identity,failure=authenticate(app,username,request.form.get('password',''))
            if failure is None:
                session.clear()
                session.update(username=username,role=identity['role'],scope=identity['scope'],csrf_token=secrets.token_urlsafe(32))
                session.permanent=True
                store.audit(username,'auth.sign_in',{'demo':demo})
                return redirect('/')
        return render_template('signin.html',demo=demo,error=failure,csrf_token=session['csrf_token'],
                               teams=state['teams'],groups=state['groups'],mechanics=sorted(state['mech_by_id']))

    @app.post('/sign-out')
    def sign_out():
        session.clear()
        return redirect('/sign-in')

    def home():
        sc=auth()
        return render_template('astra.html',demo=demo,mock_data=state['mock_data'],role=sc['role'],
                               username=session['username'],csrf_token=session['csrf_token'])
    app.view_functions['index']=home

    @app.get('/api/astra/bootstrap')
    def bootstrap():
        sc=auth(); ts=views.visible_tasks(state,sc)
        allowed={t.team for t in ts}
        return jsonify(role=sc['role'],scope=sc['scope'],username=session['username'],demo=demo,
                       mock_data=state['mock_data'],teams=sorted(allowed),
                       aircraft=[{'id':a.aircraft,'name':a.name} for a in state['fleet'].aircraft if a.aircraft in {t.aircraft for t in ts}],
                       skills=sorted({t.skill for t in ts}),mechanic=sc['mech_id'],
                       revision=state['astra_revision'],plan_id=state['astra_plan_id'],
                       day=state['start_day'],shift=1,csrf_token=session['csrf_token'])

    @app.get('/api/astra/overview')
    def overview():
        sc=auth()
        if request.args.get('team'):
            t=team_scope(sc,request.args['team']); sc={**sc,'teams':{t}}
        return jsonify(views.summary(state,sc,integer(request.args.get('day'),0,0,10000),integer(request.args.get('shift'),1,1,3)))

    @app.get('/api/astra/tasks')
    def tasks():
        sc=auth()
        if request.args.get('team'): team_scope(sc,request.args['team'])
        rows=views.task_list(state,sc,request.args)
        page=integer(request.args.get('page'),1,1,100000); size=integer(request.args.get('size'),50,1,200)
        return jsonify(total=len(rows),page=page,size=size,rows=[views.task_row(t,state) for t in rows[(page-1)*size:page*size]],revision=state['astra_revision'])

    @app.get('/api/astra/tasks/<task_id>')
    def task_detail(task_id):
        sc=auth(); t=state['task_by_id'].get(task_id)
        if not t: return error('Unknown task.','not_found',404)
        if task_id not in {t.task_id for t in views.visible_tasks(state,sc)}: return error('Task is outside your scope.','forbidden_scope',403)
        result=views.task_row(t,state,True)
        # Predecessor status is required for work readiness; names/crew remain scoped.
        return jsonify(result)

    @app.get('/api/astra/roster')
    def roster_get():
        sc=auth(); team=request.args.get('team')
        if team: team_scope(sc,team)
        day=integer(request.args.get('day'),0,0,10000)
        bookings={}
        for assignment in state['schedule'].assignments.values():
            if assignment.day==day:
                for mid in assignment.mechanic_ids:
                    bookings.setdefault(mid,[]).append(assignment)
        result=[]
        for m in base_roster:
            if not team_allowed(sc,m.team) or (team and m.team!=team) or (sc['role']=='mechanic' and m.mech_id!=sc['mech_id']): continue
            settings=state['astra_roster'].get(m.mech_id,{})
            assignments=bookings.get(m.mech_id,[])
            result.append({**m.to_dict(),**settings,'available':settings.get('available',True),
                           'tasks':len(assignments),'minutes':sum(a.end_minute-a.start_minute for a in assignments)})
        return jsonify(rows=result,revision=state['astra_revision'])

    def roster_changes(data,sc):
        changes=data.get('changes')
        if not isinstance(changes,list) or not 1<=len(changes)<=1000: raise ApiError(400,'invalid_roster','Supply 1–1,000 roster changes.')
        result=copy.deepcopy(state['astra_roster'])
        known={m.mech_id:m for m in base_roster}
        for row in changes:
            if not isinstance(row,dict) or row.get('mech_id') not in known: raise ApiError(400,'invalid_roster','Unknown mechanic.')
            if set(row)-{'mech_id','shift','available'}: raise ApiError(400,'invalid_roster','Only availability and shift may change. Team ownership and skills come from the source roster.')
            m=known[row['mech_id']]; team_scope(sc,m.team)
            old=result.get(m.mech_id,{})
            shift=integer(row.get('shift'),old.get('shift',m.shift),1,3)
            available=row.get('available',old.get('available',True))
            if not isinstance(available,bool): raise ApiError(400,'invalid_roster','Availability must be true or false.')
            result[m.mech_id]={'shift':shift,'available':available}
        return result

    @app.post('/api/astra/roster')
    def roster_post():
        """OR-6-adjacent roster configuration; never changes task state or team ownership."""
        sc=auth(True); changes=roster_changes(body(),sc)
        store.set_setting('roster',changes,g.actor)
        state['astra_roster']=changes; apply_roster(); state['astra_stale']=True
        bump_revision()
        return jsonify(ok=True,stale=True,revision=state['astra_revision'])

    @app.post('/api/astra/scenarios')
    def scenarios():
        sc=auth(True); data=body(); changes=roster_changes(data,sc)
        from ff.engine.cpm import compute_cpm
        from ff.engine.scheduler import build_schedule, incumbent_from_schedule
        from ff.engine.validator import validate
        from ff.engine.economics import fleet_economics
        candidate=Fleet.from_dict(state['fleet'].to_dict())
        candidate.mechanics=[]
        for m in base_roster:
            setting=changes.get(m.mech_id,{})
            if setting.get('available',True):
                entry=copy.deepcopy(m); entry.shift=setting.get('shift',m.shift); candidate.mechanics.append(entry)
        cpm=compute_cpm(candidate.tasks)
        plan=build_schedule(candidate,cpm,start_day=state['start_day'],incumbent=incumbent_from_schedule(state['schedule']))
        validation=validate(plan,candidate)
        proposed={k:plan.stats.get(k) for k in ['scheduled','unscheduled','fleet_lateness_days','otd_count','makespan_day','wall_seconds']}
        baseline={k:state['schedule'].stats.get(k) for k in proposed}
        report={'baseline':baseline,'scenario':proposed,'delta':{k:proposed[k]-baseline[k] for k in proposed if isinstance(proposed[k],(float,int)) and isinstance(baseline[k],(float,int))},
                'validator':validation['summary'],'mock_data':state['mock_data'],'applied':False,
                'scope':'Fleet consequences of changes restricted to your teams. No other-team individual records are returned.',
                'economics':fleet_economics(plan.stats.get('aircraft',[]),state['start_day'])}
        report['baseline']['feasible_otd_count']=sum(a['lateness_days']==0 and a.get('unscheduled_tasks',0)==0 for a in state['schedule'].stats.get('aircraft',[]))
        report['scenario']['feasible_otd_count']=sum(a['lateness_days']==0 and a.get('unscheduled_tasks',0)==0 for a in plan.stats.get('aircraft',[]))
        report['delta']['feasible_otd_count']=report['scenario']['feasible_otd_count']-report['baseline']['feasible_otd_count']
        store.audit(g.actor,'scenario.evaluated',{'changes':data['changes'],'report':report})
        return jsonify(report)

    @app.route('/api/astra/baselines',methods=['GET','POST'])
    def baselines():
        sc=auth(request.method=='POST'); data=body() if request.method=='POST' else request.args
        team=team_scope(sc,data.get('team',''))
        day=integer(data.get('day'),0,0,10000); shift=integer(data.get('shift'),1,1,3)
        if request.method=='POST':
            if state['astra_stale']: return error('Replan before pinning a baseline.','stale_plan',409)
            with store.connect() as db:
                existing=db.execute('SELECT plan_id FROM baselines WHERE team=? AND day=? AND shift=?',(team,day,shift)).fetchone()
                if existing: return error('This shift already has an immutable baseline.','already_pinned',409)
                db.execute('INSERT INTO baselines VALUES(?,?,?,?,?,?)',(team,day,shift,state['astra_plan_id'],g.actor,now()))
                db.execute('INSERT INTO audit(created_at,actor,action,payload) VALUES(?,?,?,?)',(now(),g.actor,'baseline.pinned',json.dumps({'team':team,'day':day,'shift':shift,'plan_id':state['astra_plan_id']})))
            bump_revision()
        result=views.performance(state,store,sc,team,day,shift)
        if sc['role']=='mechanic':
            own={t.task_id for t in views.visible_tasks(state,sc)}
            result['rows']=[r for r in result.get('rows',[]) if r['id'] in own]
        return jsonify(result)

    @app.route('/api/astra/shift-notes',methods=['GET','POST'])
    def notes():
        sc=auth(); data=body() if request.method=='POST' else request.args
        team=team_scope(sc,data.get('team','')); day=integer(data.get('day'),0,0,10000); shift=integer(data.get('shift'),1,1,3)
        with store.connect() as db:
            if request.method=='POST':
                note=data.get('note','')
                if not isinstance(note,str) or not 1<=len(note.strip())<=2000: return error('Write a note from 1 to 2,000 characters.')
                db.execute('INSERT INTO notes(team,day,shift,actor,created_at,body) VALUES(?,?,?,?,?,?)',(team,day,shift,g.actor,now(),note.strip()))
                db.execute('INSERT INTO audit(created_at,actor,action,payload) VALUES(?,?,?,?)',(now(),g.actor,'shift_note.added',json.dumps({'team':team,'day':day,'shift':shift})))
            rows=[dict(r) for r in db.execute('SELECT * FROM notes WHERE team=? AND day=? AND shift=? ORDER BY id DESC LIMIT 100',(team,day,shift))]
        return jsonify(rows=rows)

    @app.get('/api/astra/history')
    def history():
        sc=auth(True)
        with store.connect() as db:
            plans=[dict(r) for r in db.execute('SELECT id,snapshot_id,created_at,parent_id FROM plans ORDER BY id DESC LIMIT 50')]
            audit=[dict(r) for r in db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 200')]
        if sc['teams'] is not None: audit=[r for r in audit if r['actor']==g.actor]
        for r in audit: r['payload']=json.loads(r['payload'])
        return jsonify(plans=plans,audit=audit)

    @app.get('/api/astra/export')
    def export():
        import csv,io
        sc=auth()
        if request.args.get('team'): team_scope(sc,request.args['team'])
        rows=views.task_list(state,sc,request.args)
        output=io.StringIO(); writer=csv.writer(output)
        writer.writerow(['Task','Aircraft','Name','Team','Skill','State','Day','Shift','Start minute','End minute','Named crew','Synthetic'])
        def safe(value):
            value=str(value) if value is not None else ''
            return "'"+value if value.startswith(('=','+','-','@','\t','\r')) else value
        for t in rows:
            r=views.task_row(t,state)
            writer.writerow([safe(v) for v in [r['id'],r['aircraft'],r['name'],r['team'],r['skill'],r['state'],r['day'],r['shift'],r['start'],r['end'],', '.join(r['mechanics']),state['mock_data']]])
        return app.response_class(output.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=focus-tasks.csv'})

    def bump_revision():
        state['astra_revision']+=1
        store.set_setting('revision',state['astra_revision'],g.actor)

    # Decorate the ORIGINAL task-state writer and replan endpoint; no sibling writer.
    original_actuals=app.view_functions['api.api_actuals']
    def actuals():
        data=body(); sc=auth(); tid=data.get('task_id'); task=state['task_by_id'].get(tid)
        if task: _check_task_scope(state,sc,task)
        day=integer(data.get('execution_day'),state['start_day'],0,10000); shift=integer(data.get('execution_shift'),1,1,3)
        if sc['role']=='mechanic' and data.get('force'): return error('A lead must reopen completed work.','forbidden_role',403)
        if data.get('force') not in (None,True,False): return error('force must be a boolean.')
        a=state['schedule'].assignments.get(tid)
        request_id=request.headers.get('Idempotency-Key')
        digest=hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()
        if request_id:
            if len(request_id)>128: return error('Idempotency key is too long.')
            with store.connect() as db:
                cached=db.execute('SELECT * FROM requests WHERE request_key=?',(request_id,)).fetchone()
            if cached:
                if cached['actor']!=g.actor or cached['payload_hash']!=digest: return error('Idempotency key was already used.','idempotency_conflict',409)
                return jsonify(json.loads(cached['response'])),cached['status']
        store.audit(g.actor,'actuals.intent',{'task_id':tid,'state':data.get('state'),'request_id':request_id})
        before_task=(task.state,task.remaining_minutes) if task else None
        before_actual=copy.deepcopy(state['actuals'].get(tid))
        try:
            response=app.make_response(original_actuals())
        except Exception:
            if task:
                task.state,task.remaining_minutes=before_task
                if before_actual is None: state['actuals'].pop(tid,None)
                else: state['actuals'][tid]=before_actual
            raise
        if response.status_code<300:
            result=response.get_json()
            event={**result,'day':day,'shift':shift,'plan_id':state['astra_plan_id'],'mechanics':list(a.mechanic_ids) if a else [],'team':task.team if task else None}
            store.audit(g.actor,'actuals.commit',event)
            for mid in event['mechanics']: state['astra_own_history'].setdefault(mid,set()).add(tid)
            state['astra_stale']=True; bump_revision()
            result['revision']=state['astra_revision']
            if request_id:
                with store.connect() as db:
                    db.execute('INSERT INTO requests VALUES(?,?,?,?,?)',(request_id,g.actor,digest,json.dumps(result),response.status_code))
            return jsonify(result)
        return response
    app.view_functions['api.api_actuals']=actuals

    original_replan=app.view_functions['api.api_replan']
    def replan():
        auth(True)
        data=request.get_json(silent=True) or {}
        if not isinstance(data,dict): return error('A JSON object is required.')
        new_day=integer(data.get('start_day'),state['start_day'],state['start_day'],10000)
        old_day=state['start_day']
        state['start_day']=new_day
        previous={k:state.get(k) for k in ['schedule','snapshot','snapshot_id','cpm','built_at','validator_summary','validator_total','build_count','boot_error']}
        try:
            response=app.make_response(original_replan())
        except Exception:
            state.update(previous)
            state['start_day']=old_day
            state['astra_stale']=True
            raise
        if response.status_code<300:
            if state['validator_total']:
                state.update(previous)
                state['start_day']=old_day
                state['astra_stale']=True
                return error('Validation failed; schedule cannot be accepted.','invalid_schedule',503)
            state['astra_plan_id']=store.save_plan(state)
            store.set_setting('planning_day',new_day,g.actor)
            state['astra_stale']=False; bump_revision()
            store.audit(g.actor,'plan.published',{'plan_id':state['astra_plan_id'],'snapshot_id':state['snapshot_id']})
            result=response.get_json(); result.update(plan_id=state['astra_plan_id'],revision=state['astra_revision'])
            refresh=app.extensions.get('astra_legacy_refresh')
            if refresh: refresh()
            return jsonify(result)
        state.update(previous)
        state['start_day']=old_day
        state['astra_stale']=True
        return response
    app.view_functions['api.api_replan']=replan
    original_excusals=app.view_functions['api.api_excusals_post']
    def excusal_capture():
        response=app.make_response(original_excusals())
        if response.status_code<300:
            result=response.get_json()
            store.audit(g.actor,'excusal.captured',result['excusal'])
            bump_revision()
            result['revision']=state['astra_revision']
            return jsonify(result)
        return response
    app.view_functions['api.api_excusals_post']=excusal_capture
    if os.environ.get('ASTRA_LEGACY','1')=='1':
        from .legacy import install
        install(app,state_dir)
    return app
