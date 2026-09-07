# Run and deploy ASTRA FOCUS

## Local synthetic demonstration

Clone recursively with authenticated GitHub access. `upstream` is private; a ZIP of the public repository does not contain that dependency.

```bash
git clone --recurse-submodules https://github.com/JaredVogler2/ASTRA-FOCUS-1.git
cd ASTRA-FOCUS-1
docker compose up --build
```

The application is at `http://localhost:8080`. The Compose file exposes localhost only and stores the synthetic fleet, actuals, session key, plan database and envelopes in the `focus-state` volume. `docker compose down` retains that volume. Removing the volume deletes the demonstration's saved state.

The full-fleet demonstration uses the original generator defaults: 50 aircraft, 680 mechanics, seed 20260710. For Python-only local runs, set `ASTRA_DEMO=1` and use `gunicorn -c gunicorn.conf.py wsgi:app` after installing requirements.

## Non-demo configuration

The account creator prompts for a password and writes a mode-0600 JSON file containing a hash, never the password:

```bash
python scripts/create_user.py --file /private/focus/users.json --username lead01 --role lead --scope T01
```

Supported role/scope pairs:

| Role | Scope value |
|---|---|
| mechanic | Exact mechanic ID from the fleet roster |
| lead, flm | Exact team ID |
| super | Exact group ID from the original FF organization map |
| director, vp | `all` |

Configure the host's environment manager:

| Variable | Required behavior |
|---|---|
| `ASTRA_DEMO=0` | Uses authenticated accounts; self-selected personas disabled |
| `FF_SECRET_KEY` | Random application secret, stored by the host; never checked in |
| `ASTRA_USERS_FILE` | Absolute path to the private server-owned account file |
| `FF_DATA` | Absolute path to a validated FF fleet JSON.gz file |
| `ASTRA_STATE_DIR` | Writable, persistent directory retained through restarts/redeployments |
| `ASTRA_LEGACY` | `1` to include preserved comparison dashboards; `0` to omit them |
| `FF_LLM_PROVIDER=none` | Deterministic non-LLM operation; optional upstream providers require their own reviewed configuration |

The `.env.example` is a reference. Plain Gunicorn does not automatically load it. Configure the environment using your host or shell; do not expect copying `.env.example` to enable authentication.

Terminate HTTPS on the approved private application gateway. Secure session cookies are required outside demo mode. If a reverse proxy terminates HTTPS, it must preserve the application origin correctly for the CSRF origin comparison. This build does not blindly trust arbitrary forwarded headers. Configure the hosting adapter's trusted proxy handling for that deployment before use.

## Host constraints

- Python 3.12, Flask and Gunicorn. Container entrypoint: `wsgi:app`.
- Exactly **one worker and one application instance**. Four threads may serve it, but shared-state operations serialize. A 409 busy response is an explicit retry condition.
- `/healthz` is liveness; `/readyz` is engine readiness.
- Configure a 300-second request timeout and sufficient startup time for full-fleet scheduling and plan serialization.
- Measured peak memory with the preserved full-fleet dashboard was approximately 547 MiB. Provision headroom for replans, retained envelopes and larger datasets; 128 MiB JavaScript Workers cannot run this application.
- Keep the state directory and user file private, back them up, and retain a consistent copy of the input dataset alongside backups. Restore while the application is stopped.

The preserved dashboard's old local SQLite comparison database is not the new operational source of truth. Legacy mutation routes are disabled. ASTRA's actuals, baselines, roster settings and notes are the supported operational workflow.

## Posit Connect

Deploy the Python content with `wsgi.py` as the WSGI entrypoint, the recursively initialized private dependency included in the deployment bundle, and the same environment variables above. Set process/instance limits to one and supply persistent state storage. Provision authentication, an HTTPS origin, filesystem permissions and any enterprise data feeds in the approved environment.

## Data integration

The input contract remains `FF_app/ff/domain.py`. Configure `FF_DATA` to the reviewed source extract before starting. Missing configured input files fail startup rather than falling back to an unrelated demo. The scheduler's original validator checks the resulting plan; it does not certify source-system joins, business semantics or authorization to process enterprise data.

MES/Teradata/SQL Server feed adapters, enterprise SSO and a hosted Python service have not been connected in this delivery. External ingestion must preserve the single actuals write path and role/source attribution. No production records or credentials are included in this repository.

## Continuous verification

Run `python scripts/verify.py` with access to the private submodule. A CI runner needs read access to `FABLE_FOCUS_REVIEW`; the public target repository's default token does not grant access to unrelated private repositories. Do not publish a source archive or CI artifact containing the private dependency to this public repository.
