"""WSGI entry for Gunicorn, Posit Connect and compatible Python hosts."""
import fcntl
from astra_focus.runtime import configure
state_dir,_demo=configure()
_instance_lock=open(state_dir/'instance.lock','a')
try:
    fcntl.flock(_instance_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError:
    raise RuntimeError('Another FOCUS process owns this state directory. Run exactly one instance.')
from astra_focus.app import create_app
app=create_app()
