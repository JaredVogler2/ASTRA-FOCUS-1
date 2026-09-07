# Run and deploy ASTRA FOCUS

ASTRA-FOCUS-1 contains every application component, including the FF_app engine, original FOCUS Flask dashboards, browser libraries and sample-data generator. A normal clone or GitHub source ZIP is sufficient. Reference repositories are provenance only.

The host installs the pinned Python framework packages from `requirements.txt`. No external application repository, CDN, database server, LLM account, Node build, or private GitHub token is required. SQLite and operational files live on the application's persistent disk. An already-built Docker image can run without outbound internet access.

## Live hosting on Render

[Deploy this repository on Render](https://render.com/deploy?repo=https://github.com/JaredVogler2/ASTRA-FOCUS-1)

1. Sign in to Render and open the deployment link. Alternatively, choose **New → Blueprint**, connect GitHub, and select `JaredVogler2/ASTRA-FOCUS-1` on `main`.
2. Render reads the checked-in `render.yaml`. Enter `ASTRA_ADMIN_USERNAME` and `ASTRA_ADMIN_PASSWORD` when prompted. Use a unique password of at least 12 characters; enter it in Render, never in GitHub.
3. Review and approve the hosting charges. This configuration requests **one CPU / 2 GB RAM** and a **5 GB persistent disk**. The app previously measured approximately 547 MiB peak memory, so a 512 MB instance is too small. A paid service is necessary for a persistent disk. See [current pricing](https://render.com/pricing) and [persistent disk behavior](https://render.com/docs/disks).
4. Create the Blueprint. The host installs packages, creates the first administrator with a password hash, generates the synthetic 50-aircraft fleet, and starts Gunicorn. Allow several minutes for the initial build and scheduling run.
5. When the service becomes healthy, open the `https://…onrender.com` URL displayed by Render and sign in with the administrator account. `/readyz` must return HTTP 200; an unauthenticated `/api/astra/tasks` request must return 401.

The initial installation uses **password-protected sample data**. `ASTRA_DEMO=0` keeps self-selected roles disabled. `ASTRA_GENERATE_SAMPLE_DATA=1` explicitly creates a fleet when no `FF_DATA` file is configured; the workspace labels that data as synthetic.

The Blueprint stores the fleet, accounts, actuals, baselines, plans, audit, notes and roster settings under `/var/data/astra-focus`. Restarts retain that directory. The administrator bootstrap only creates a missing account file; changing its environment password does not overwrite existing accounts. After the first successful sign-in, remove `ASTRA_ADMIN_PASSWORD` from Render's environment. Persisted account hashes continue to work.

Automatic redeployment is initially off. To release later repository changes, choose **Manual Deploy → Deploy latest commit** after CI succeeds. You can enable automatic deployment after successful checks in Render if desired. A service with an attached disk has a brief outage during redeployment; keep it to one instance. [Blueprint reference](https://render.com/docs/blueprint-spec), [deployment link behavior](https://render.com/docs/deploy-to-render).

Render supplies the HTTPS origin through `RENDER_EXTERNAL_URL`; the application uses it for same-origin write checks even when the internal proxy connection is HTTP. For a custom domain, set `ASTRA_PUBLIC_ORIGIN=https://your-domain.example` and use that canonical URL. Secure cookies remain enabled; arbitrary forwarded headers are not trusted.

A Render account and approval of its charges are the remaining hosting actions. The repository does not create a paid service merely by being pushed to GitHub. If managing deployment through ChatGPT, connect the Render integration to the account/workspace you want to use.

## Local synthetic demonstration

```bash
git clone https://github.com/JaredVogler2/ASTRA-FOCUS-1.git
cd ASTRA-FOCUS-1
docker compose up --build
```

Open `http://localhost:8080`. The Compose configuration exposes localhost only, enables sample personas, and persists its state in the `focus-state` volume. `docker compose down` retains the volume. Removing the volume deletes saved state.

Python alternative on Linux/macOS with Python 3.12:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
ASTRA_DEMO=1 gunicorn -c gunicorn.conf.py wsgi:app
```

The generator defaults are 50 aircraft, 680 mechanics, seed 20260710. `ASTRA_AIRCRAFT` can change the initial fleet size; it does not replace an existing saved fleet. The complete source ZIP follows these same instructions after extraction.

## Accounts and real input data

Create or update accounts using the host shell. This command prompts for a password and writes a private JSON file containing its hash:

```bash
python scripts/create_user.py --file /var/data/astra-focus/users.json --username lead01 --role lead --scope T01
```

Restart the application after an account change. The same command updates a forgotten password; bootstrap environment variables do not reset existing accounts.

| Role | Scope |
|---|---|
| mechanic | Exact mechanic ID from the fleet roster |
| lead, flm | Exact team ID |
| super | Exact group ID from the FF organization map |
| director, vp | `all` |

For real input data, create a separate installation/state directory so its plans and actuals are not mixed with the sample fleet. Place the reviewed FF fleet JSON.gz file on persistent storage, set `FF_DATA` to its absolute path, set `ASTRA_GENERATE_SAMPLE_DATA=0`, and create accounts with the IDs from that fleet. Deploy in the environment approved for the actual data being used.

| Variable | Purpose |
|---|---|
| `ASTRA_DEMO=0` | Password authentication and server-assigned roles |
| `FF_SECRET_KEY` | Random session secret; generated automatically by the Blueprint |
| `ASTRA_STATE_DIR` | Writable persistent directory; `/var/data/astra-focus` in Render |
| `ASTRA_USERS_FILE` | Optional alternate account-file path; defaults to `ASTRA_STATE_DIR/users.json` |
| `ASTRA_ADMIN_USERNAME`, `ASTRA_ADMIN_PASSWORD` | First-start director account, only when account file is absent |
| `FF_DATA` | Absolute path to an existing validated fleet JSON.gz |
| `ASTRA_GENERATE_SAMPLE_DATA=1` | Generate a labeled synthetic fleet if `FF_DATA` is unset |
| `ASTRA_AIRCRAFT` | Generated aircraft count, 1–100; default 50 |
| `ASTRA_LEGACY=1` | Include original FOCUS comparison dashboards |
| `FF_LLM_PROVIDER=none` | Run without an LLM service |
| `PORT` | Host-provided listening port; default 8080 |
| `ASTRA_PUBLIC_ORIGIN` | Optional canonical HTTPS origin for a reverse proxy/custom domain |

`.env.example` is a reference; Gunicorn does not automatically load it. Use your host's environment configuration.

## Other Python hosts, including Posit Connect

Use Python 3.12 and `wsgi.py` as the WSGI entrypoint. Include this repository in the deployment bundle; there is no submodule initialization or second service. Configure the account, dataset, secret and persistent directory as above, then expose the application through an HTTPS gateway. For a proxy host, set `ASTRA_PUBLIC_ORIGIN` to the exact HTTPS origin.

Run **one worker and one application instance**. Gunicorn permits four threads, but scheduling/state operations serialize; HTTP 409 busy responses can be retried. The WSGI entrypoint locks the state directory. Use a 300-second request timeout and allow startup time for full-fleet scheduling. `/healthz` is liveness and `/readyz` is readiness.

Back up the persistent directory and the input fleet together while the application is stopped. SQLite, actuals and the plan lineage must be restored as one consistent snapshot. The inherited comparison database is not the operational source of truth; original dashboard mutation routes are disabled.

The application runs independently of enterprise SSO, MES, Teradata or SQL Server. Feed adapters and enterprise identity integration remain separate configuration/development work when those systems are required.

## Verification

```bash
python -m pip install -r requirements-dev.txt
python scripts/verify.py
```

The checked-in GitHub Actions workflow uses a normal checkout with the repository's default read token. It runs the integration suite, original engine tests and gates, points checks, all six benchmarks, and a container build/readiness check. No access to any other repository is required.
