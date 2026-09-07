"""First-start account setup for a self-contained hosted installation."""
import json
import os
import tempfile
from pathlib import Path
from werkzeug.security import generate_password_hash


def bootstrap_account(state_dir):
    """Create one director once; never replace persisted accounts on restart."""
    path = Path(os.environ.get('ASTRA_USERS_FILE', state_dir / 'users.json'))
    os.environ['ASTRA_USERS_FILE'] = str(path)
    if path.exists():
        return
    username = os.environ.get('ASTRA_ADMIN_USERNAME', '').strip()
    password = os.environ.get('ASTRA_ADMIN_PASSWORD', '')
    if not username or len(password) < 12:
        raise RuntimeError('For first startup, set ASTRA_ADMIN_USERNAME and a 12+ character '
                           'ASTRA_ADMIN_PASSWORD, or supply an existing ASTRA_USERS_FILE.')
    path.parent.mkdir(parents=True, exist_ok=True)
    users = {username: {'password_hash': generate_password_hash(password),
                        'role': 'director', 'scope': 'all'}}
    fd, temporary = tempfile.mkstemp(prefix='.users-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(users, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        # Publish the complete file only if it is still absent.
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        os.unlink(temporary)
