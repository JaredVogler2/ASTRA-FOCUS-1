"""Mount the preserved FOCUS Flask views as a director-only comparison surface.

Templates load bundled browser libraries. Reads use the authenticated FF functions in this process,
eliminating the upstream bridge's director/all service-login transport. Actuals
and planning controls live in the ASTRA operational workspace.
"""
import os
import sys
from pathlib import Path
from flask import jsonify, redirect, request, session, send_from_directory
from jinja2 import FileSystemLoader
from .runtime import FF_ROOT

STATE_ATTRIBUTES=('scheduler','scenario_results','saved_scenarios','loader','dags','segment_groups',
                  'focus_aircraft','final_schedule','mechanic_timelines','calculated_pool_sizes',
                  'summary_metrics','mechanic_assignments','current_schedule_data','current_schedule_file',
                  'previous_schedule_data','previous_schedule_file','available_schedules','active_schedule')

def install(app,state_dir):
    from ff.export.envelope import export_envelope
    state=app.extensions['ff']
    export_envelope(state['fleet'],state['schedule'],state['cpm'],state['snapshot'],str(state_dir/'schedules'))
    path=FF_ROOT/'web_flask'
    if str(path) not in sys.path: sys.path.insert(0,str(path))
    from src import paths
    paths.SCHEDULES_DIR=state_dir/'schedules'
    from src import app as legacy_module
    legacy_module.SCHEDULES_DIR=state_dir/'schedules'
    legacy=legacy_module.create_app()
    app.jinja_loader.loaders.append(FileSystemLoader(str(path/'templates')))
    endpoints=set()
    for rule in legacy.url_map.iter_rules():
        if rule.endpoint=='static' or str(rule)=='/' or rule.endpoint.startswith('ff_bridge.'):
            continue
        if rule.endpoint in app.view_functions: raise RuntimeError('Legacy endpoint collision: '+rule.endpoint)
        app.add_url_rule(str(rule),rule.endpoint,legacy.view_functions[rule.endpoint],methods=sorted(rule.methods-{'HEAD','OPTIONS'}))
        endpoints.add(rule.endpoint)
    def refresh():
        legacy.reload_schedules()
        for key in STATE_ATTRIBUTES:
            if hasattr(legacy,key): setattr(app,key,getattr(legacy,key))
        return True
    refresh()
    app.reload_schedules=refresh
    app.extensions['astra_legacy_refresh']=refresh
    app.add_url_rule('/focus','astra_focus_legacy',lambda:redirect('/dashboard'))
    endpoints.add('astra_focus_legacy')
    for folder in ('css','js','images'):
        endpoint='legacy_assets_'+folder
        app.add_url_rule('/static/'+folder+'/<path:filename>',endpoint,
                         lambda filename,folder=folder:send_from_directory(str(path/'static'/folder),filename))
        endpoints.add(endpoint)
    # Fixed read-only bridge allow-list. Calls the same scoped functions directly,
    # under the current request and current signed user session; no service persona.
    reads={'/api/ff/points/shift':'api.api_points_shift','/api/ff/points/leaderboard':'api.api_points_leaderboard',
           '/api/ff/scorecard':'api.api_scorecard','/api/ff/recap':'api.api_recap',
           '/api/ff/excusals':'api.api_excusals_get','/api/ff/explain/candidates':'api.api_explain_candidates',
           '/api/ff/progression/team/<team>':'api.api_progression_team'}
    for index,(url,target) in enumerate(reads.items()):
        endpoint='legacy_ff_read_'+str(index)
        app.add_url_rule(url,endpoint,lambda target=target,**kwargs:app.view_functions[target](**kwargs),methods=['GET'])
        endpoints.add(endpoint)

    @app.before_request
    def legacy_boundary():
        if request.endpoint in endpoints:
            if session.get('role') not in {'director','vp'}:
                return jsonify(error='The full-fleet comparison is available to directors and executives. Use the scoped ASTRA workspace.',code='forbidden_scope'),403
            if request.method not in {'GET','HEAD','OPTIONS'}:
                return jsonify(error='This preserved comparison is read-only. Use the ASTRA workspace to update actuals, staffing, and shift records.',code='legacy_read_only'),409

    @app.after_request
    def comparison_banner(response):
        if request.path in {'/dashboard','/dashboard/classic'} and response.status_code==200:
            text=response.get_data(as_text=True)
            notice='<div style="padding:12px 24px;background:#fff4dc;color:#704d15;font:13px system-ui;position:relative;z-index:2000">Preserved FOCUS comparison · Read-only · <a href="/">Open ASTRA for task updates, staffing and pinned shift performance →</a></div>'
            response.set_data(text.replace('<body>','<body>'+notice,1))
        return response
