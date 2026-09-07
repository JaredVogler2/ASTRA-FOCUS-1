import os
bind=os.environ.get('ASTRA_BIND','0.0.0.0:'+os.environ.get('PORT','8080'))
workers=1
threads=4
timeout=300
graceful_timeout=300
accesslog='-'
errorlog='-'
preload_app=False

def on_starting(server):
    if server.cfg.workers!=1:
        raise RuntimeError('FF state is process-local. Exactly one worker is required.')
