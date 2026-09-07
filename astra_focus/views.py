"""Scoped operational read models. Scheduling and factor scores remain upstream."""
import csv
import io
import json
from collections import Counter, defaultdict

def visible_tasks(state, sc):
    tasks = state['fleet'].tasks
    if sc['role'] == 'mechanic':
        mid = sc['mech_id']
        own = {tid for tid,a in state['schedule'].assignments.items() if mid in a.mechanic_ids}
        # A completed task drops out of the replan; retain its original named assignment.
        own |= state.get('astra_own_history',{}).get(mid,set())
        return [t for t in tasks if t.task_id in own]
    return [t for t in tasks if sc['teams'] is None or t.team in sc['teams']]

def task_row(t, state, detail=False):
    a = state['schedule'].assignments.get(t.task_id)
    c = state['cpm'].get(t.task_id,{})
    from ff.domain import DAY_WORK_MINUTES
    row = {'id':t.task_id, 'aircraft':t.aircraft, 'name':t.name, 'team':t.team,
           'skill':t.skill, 'state':t.state, 'duration':t.duration_minutes,
           'remaining':t.remaining_minutes, 'crew_required':t.mechanics_required,
           'critical':c.get('slack_minutes',0) <= DAY_WORK_MINUTES*0.5,
           'slack_minutes':c.get('slack_minutes',0), 'deadline_day':t.deadline_day,
           'release_day':t.earliest_day, 'parts_eta_day':t.parts_eta_day,
           'inspection':t.is_inspection, 'rework':t.is_rework,
           'day':a.day if a else None,'shift':a.shift if a else None,
           'start':a.start_minute if a else None, 'end':a.end_minute if a else None,
           'mechanics':list(a.mechanic_ids) if a else [],
           'reason':state['schedule'].unscheduled.get(t.task_id),
           'predecessor_count':len(t.predecessors),
           'open_predecessors':sum(state['task_by_id'][p].state!='done' for p in t.predecessors if p in state['task_by_id'])}
    if detail:
        from ff.services.points import score_task
        row['predecessors'] = [{'id':p,'state':state['task_by_id'][p].state} for p in t.predecessors if p in state['task_by_id']]
        row['score'] = score_task(t.task_id,state['snapshot'])
    return row

def task_list(state, sc, args):
    tasks = visible_tasks(state, sc)
    text = args.get('q','').lower().strip()
    for name, attr in [('team','team'),('state','state'),('aircraft','aircraft'),('skill','skill')]:
        value = args.get(name)
        if value and value != 'all':
            tasks = [t for t in tasks if str(getattr(t,attr)) == value]
    if text:
        tasks = [t for t in tasks if text in (t.task_id+' '+t.name+' '+t.team+' '+t.skill).lower()]
    if args.get('critical') == '1':
        from ff.domain import DAY_WORK_MINUTES
        tasks = [t for t in tasks if state['cpm'].get(t.task_id,{}).get('slack_minutes',0)<=DAY_WORK_MINUTES*.5]
    rows = []
    for t in tasks:
        a = state['schedule'].assignments.get(t.task_id)
        if args.get('day') and (a is None or str(a.day)!=args['day']): continue
        if args.get('shift') and (a is None or str(a.shift)!=args['shift']): continue
        if args.get('mechanic') and (a is None or args['mechanic'] not in a.mechanic_ids): continue
        if args.get('unplaced') == '1' and t.task_id not in state['schedule'].unscheduled: continue
        rows.append(t)
    rows.sort(key=lambda t: (state['schedule'].assignments[t.task_id].day if t.task_id in state['schedule'].assignments else 10**9,
                             state['schedule'].assignments[t.task_id].shift if t.task_id in state['schedule'].assignments else 4,
                             state['cpm'].get(t.task_id,{}).get('slack_minutes',0), t.task_id))
    return rows

def summary(state, sc, day, shift):
    import config
    tasks = visible_tasks(state,sc)
    ids = {t.task_id for t in tasks}
    ac_ids = {t.aircraft for t in tasks}
    counts = Counter(t.state for t in tasks)
    slot = [a for tid,a in state['schedule'].assignments.items() if tid in ids and a.day==day and a.shift==shift]
    mechanics = [m for m in state['fleet'].mechanics if (sc['teams'] is None or m.team in sc['teams']) and m.shift==shift and (sc['role']!='mechanic' or m.mech_id==sc['mech_id'])]
    roster = state.get('astra_roster',{})
    enabled = [m for m in mechanics if roster.get(m.mech_id,{}).get('available',True)]
    crew_minutes = sum((a.end_minute-a.start_minute)*len(a.mechanic_ids) for a in slot)
    from ff.domain import shift_eligible
    capacity = len(enabled)*config.SHIFT_EFFECTIVE[shift] if shift_eligible(day,shift) else 0
    aircraft=[]
    stats_by_ac={a['aircraft']:a for a in state['schedule'].stats.get('aircraft',[])}
    for ac in state['fleet'].aircraft:
        if ac.aircraft not in ac_ids: continue
        ats=[t for t in tasks if t.aircraft==ac.aircraft]
        s=stats_by_ac.get(ac.aircraft,{})
        aircraft.append({'id':ac.aircraft,'name':ac.name,'station':ac.station,'due':ac.delivery_deadline_day,
                         'finish':s.get('completion_day'), 'late_days':s.get('lateness_days',0),
                         'total':len(ats),'done':sum(t.state=='done' for t in ats),
                         'blocked':sum(t.state=='blocked' for t in ats),
                         'unscheduled':sum(t.task_id in state['schedule'].unscheduled for t in ats)})
    workload=defaultdict(lambda:{'tasks':0,'minutes':0,'blocked':0,'critical':0})
    for a in slot:
        workload[a.team]['tasks']+=1
        workload[a.team]['minutes']+=(a.end_minute-a.start_minute)*len(a.mechanic_ids)
    for t in tasks:
        if t.state=='blocked': workload[t.team]['blocked']+=1
    teams=[]
    for team in sorted({t.team for t in tasks}):
        n=sum(m.team==team for m in enabled)
        cap=n*config.SHIFT_EFFECTIVE[shift] if shift_eligible(day,shift) else 0
        row=workload[team]
        teams.append({'id':team,**row,'mechanics':n,'capacity_minutes':cap,'utilization':row['minutes']/cap if cap else None})
    return {'counts':dict(counts),'total':len(tasks),'scheduled':sum(t.task_id in state['schedule'].assignments for t in tasks),
            'unscheduled':sum(t.task_id in state['schedule'].unscheduled for t in tasks),'slot_tasks':len(slot),
            'crew_hours':round(crew_minutes/60,1),'capacity_hours':round(capacity/60,1),
            'utilization':crew_minutes/capacity if capacity else None,'aircraft':aircraft,'teams':teams,
            'scope_is_fleet':sc['teams'] is None,'validator_total':state['validator_total'],
            'stale':state.get('astra_stale',False),'mock_data':state['mock_data'],
            'plan_id':state.get('astra_plan_id'),'snapshot_id':state['snapshot_id'],
            'revision':state.get('astra_revision',0),'built_at':state['built_at']}

def performance(state, store, sc, team, day, shift):
    with store.connect() as db:
        baseline=db.execute('SELECT * FROM baselines WHERE team=? AND day=? AND shift=?',(team,day,shift)).fetchone()
        events=[json.loads(r[0]) for r in db.execute("SELECT payload FROM audit WHERE action='actuals.commit' ORDER BY id")]
    if not baseline:
        return {'team':team,'day':day,'shift':shift,'pinned':False}
    plan=store.plan(baseline['plan_id']); payload=plan['payload']
    assignments={tid:a for tid,a in payload['schedule']['assignments'].items() if a['team']==team and a['day']==day and a['shift']==shift}
    live=state['task_by_id']; value=payload['points']
    # Existing FF attribution remains authoritative; frozen plan establishes the denominator.
    from ff.domain import Fleet, Schedule
    from ff.services.snapshot import build_snapshot
    from ff.services.points import _slice_excusals
    fleet=Fleet.from_dict(payload['fleet'])
    for t in fleet.tasks:
        if t.task_id in live: t.state=live[t.task_id].state
    snap=build_snapshot(fleet,Schedule.from_dict(payload['schedule']),payload['cpm'])
    snap['manual_excusals']=state['excusals']
    excusals=_slice_excusals(snap,team,day,shift)
    completions={e['task_id'] for e in events if e.get('day')==day and e.get('shift')==shift and e.get('state')=='done' and e.get('previous_state')!='done' and e.get('plan_id',0)>=baseline['plan_id']}
    rows=[]
    for tid,a in assignments.items():
        t=live.get(tid)
        if not t: continue
        done=tid in completions and t.state=='done'
        excused=not done and tid in excusals
        current=state['schedule'].assignments.get(tid)
        moved=bool(current and (current.day,current.shift,current.start_minute)!=(a['day'],a['shift'],a['start_minute']))
        rows.append({'id':tid,'name':t.name,'points':value[tid],'state':t.state,'done':done,'excused':excused,'moved':moved,
                     'crew_hours':t.duration_minutes*t.mechanics_required/60,'causes':excusals.get(tid,[])})
    goal=sum(r['points'] for r in rows if not r['excused'])
    earned=sum(r['points'] for r in rows if r['done'])
    return {'team':team,'day':day,'shift':shift,'pinned':True,'plan_id':baseline['plan_id'],'pinned_at':baseline['created_at'],
            'goal':goal,'earned':earned,'attainment':earned/goal if goal else None,
            'planned':len(rows),'completed':sum(r['done'] for r in rows),'moved':sum(r['moved'] for r in rows),
            'excused_points':sum(r['points'] for r in rows if r['excused']),
            'planned_hours':sum(r['crew_hours'] for r in rows),'earned_hours':sum(r['crew_hours'] for r in rows if r['done']),
            'rows':rows,'scoring_basis':'Original FF score_task values frozen at plan capture; original excusal policy; recorded shift completions.'}
