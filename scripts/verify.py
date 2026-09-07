"""Run actual integration and upstream gates; any failure stops the chain."""
import subprocess
import sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
ff=root/'upstream/FF_app'
commands=[(root,[sys.executable,'-m','pytest','-q','integration_tests']),
          (ff,[sys.executable,'-m','pytest','-q']),
          (ff,[sys.executable,'run.py','gates']),
          (ff,[sys.executable,'run.py','generate-data','--aircraft','50','--out','data/fleet50.json.gz']),
          (ff,[sys.executable,'tools/points_gate.py']),
          (ff,[sys.executable,'tools/bench.py'])]
for cwd,cmd in commands:
    print('Running:', ' '.join(cmd),flush=True)
    subprocess.run(cmd,cwd=cwd,check=True)
