# Start from the GitHub source ZIP

The current ASTRA-FOCUS-1 source ZIP includes the FF_app engine, original FOCUS Flask dashboards, bundled browser libraries, ASTRA workspaces, tests and synthetic mini fixture. No private repository or submodule is required.

Extract the archive, open a terminal in its top-level directory and run:

```bash
docker compose up --build
```

Open `http://localhost:8080`. This starts a labeled synthetic demonstration and retains state in the Compose volume.

Without Docker, use Python 3.12 on Linux/macOS:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
ASTRA_DEMO=1 gunicorn -c gunicorn.conf.py wsgi:app
```

For password-protected live hosting, follow [the deployment guide](docs/DEPLOYMENT.md). The included `render.yaml` automates the service configuration. Python packages are installed during the build; all application and browser source is in this repository.

Original reference revisions and local adaptations are documented in `sources.lock.json` and `THIRD_PARTY/README.md`. Generated state, credentials, unrelated reference-repository material and old Git history are intentionally absent from the source archive.
