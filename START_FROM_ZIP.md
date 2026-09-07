# Run this private source package

This archive contains the new ASTRA application plus the FF_app runtime, tests and synthetic mini fixture from the pinned private upstream source. The included engine files are sufficient to run this package without cloning the private dependency again.

**Keep the complete source archive private.** It includes the original private FF_app engine. The repository owner authorized public publication of the integration code and documentation on 2026-09-07; the engine remains a pinned private dependency in the public repository.

From the extracted `ASTRA-FOCUS-1` directory, either run:

```bash
docker compose up --build
```

Or use Python 3.12 on Linux/macOS:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
ASTRA_DEMO=1 gunicorn -c gunicorn.conf.py wsgi:app
```

Open `http://localhost:8080`. This starts the explicitly labeled synthetic demonstration. The default 50-aircraft dataset is generated on first launch and state is retained in `var/` (or the Compose volume).

To verify the source:

```bash
python -m pip install -r requirements-dev.txt
python scripts/verify.py
```

The archive omits upstream Git history, unrelated planning corpora, generated runtime state, credentials, and the large upstream dashboard booklet/unused animated landing-page background. It retains the FF_app executable source, original tests, benchmark definitions, preserved dashboard source and required static assets. Exact private provenance is recorded in `sources.lock.json`. The included synthetic mini fixture comes from the original deterministic generator.

For a GitHub checkout, `upstream` is represented by a pinned private submodule. GitHub's automatic source ZIP does not include that private dependency. See `README.md` and `docs/DEPLOYMENT.md` for the repository and hosting workflows.
