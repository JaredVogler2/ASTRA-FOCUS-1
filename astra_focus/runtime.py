"""Configure the original engine before importing it. No alternate scheduler."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FF_ROOT = ROOT / 'FF_app'

def configure():
    if not (FF_ROOT / 'ff' / 'domain.py').exists():
        raise RuntimeError('Bundled FF_app source is missing. Use a complete ASTRA-FOCUS-1 checkout or source ZIP.')
    sys.path.insert(0, str(FF_ROOT)) if str(FF_ROOT) not in sys.path else None
    state_dir = Path(os.environ.get('ASTRA_STATE_DIR', ROOT / 'var')).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    demo = os.environ.get('ASTRA_DEMO', '0') == '1'
    if not demo and not os.environ.get('FF_SECRET_KEY'):
        raise RuntimeError('FF_SECRET_KEY is required outside explicit ASTRA_DEMO=1 mode.')
    if not demo:
        from .bootstrap import bootstrap_account
        bootstrap_account(state_dir)
    if demo:
        os.environ.setdefault('FF_ENV', 'dev')
        secret_file = state_dir / 'session.key'
        if not secret_file.exists():
            import secrets
            try:
                fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as f:
                    f.write(secrets.token_hex(32))
            except FileExistsError:
                pass
        os.environ.setdefault('FF_SECRET_KEY', secret_file.read_text().strip())
    for key, filename in {'FF_ACTUALS':'actuals.json', 'FF_EXCUSALS':'excusals.json',
                          'FF_COMMITMENTS':'commitments.json', 'FF_GAME_EVENTS':'game_events.jsonl',
                          'FF_LLM_AUDIT':'llm_audit.jsonl', 'FF_ENVELOPE_DIR':'schedules'}.items():
        os.environ.setdefault(key, str(state_dir / filename))
    os.environ.setdefault('MAX_SCHEDULES_DIR', str(state_dir / 'schedules'))
    os.environ.setdefault('FF_LLM_PROVIDER', 'none')
    os.environ.setdefault('FF_WALL_PUBLIC', '0')
    data_path = os.environ.get('FF_DATA')
    if data_path:
        if not Path(data_path).is_file():
            raise RuntimeError('FF_DATA does not exist; refusing fallback to a different dataset.')
    elif demo or os.environ.get('ASTRA_GENERATE_SAMPLE_DATA', '0') == '1':
        from ff.data.generator import generate_fleet
        from ff.data.loader import save_json_gz
        fixture = state_dir / 'fleet.json.gz'
        if not fixture.exists():
            count = int(os.environ.get('ASTRA_AIRCRAFT', '50'))
            if count < 1 or count > 100:
                raise RuntimeError('ASTRA_AIRCRAFT must be between 1 and 100.')
            import config
            fleet = generate_fleet(count, None, config.GEN_TEAMS, config.GEN_MECHANICS, config.GEN_SEED, task_universe=config.GEN_TASK_UNIVERSE)
            save_json_gz(fleet.to_dict(), str(fixture))
        os.environ['FF_DATA'] = str(fixture)
    else:
        raise RuntimeError('Set FF_DATA to a fleet file, or explicitly enable ASTRA_GENERATE_SAMPLE_DATA=1.')
    return state_dir, demo
