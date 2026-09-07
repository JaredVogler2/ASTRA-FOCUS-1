"""Create/update a server-owned account without exposing its password in argv."""
import argparse
import getpass
import json
import os
import tempfile
from pathlib import Path
from werkzeug.security import generate_password_hash

parser=argparse.ArgumentParser()
parser.add_argument('--file',default='users.json')
parser.add_argument('--username',required=True)
parser.add_argument('--role',required=True,choices=['mechanic','lead','flm','super','director','vp'])
parser.add_argument('--scope',required=True,help='Mechanic ID, team, group, or all for director/vp')
args=parser.parse_args()
password=getpass.getpass('Password (12+ characters): ')
if len(password)<12: parser.error('Use at least 12 characters.')
if password!=getpass.getpass('Confirm password: '): parser.error('Passwords do not match.')
path=Path(args.file).resolve();path.parent.mkdir(parents=True,exist_ok=True)
users=json.loads(path.read_text()) if path.exists() else {}
users[args.username]={'password_hash':generate_password_hash(password),'role':args.role,'scope':'all' if args.role in {'director','vp'} else args.scope}
fd,temp=tempfile.mkstemp(dir=path.parent,prefix='.users-')
try:
    os.fchmod(fd,0o600)
    with os.fdopen(fd,'w') as f:
        json.dump(users,f,sort_keys=True,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)
finally:
    if os.path.exists(temp):os.unlink(temp)
print('Saved account '+args.username)
