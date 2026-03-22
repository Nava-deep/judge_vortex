# Judge Vortex

Judge Vortex is a Django-based online judge for proctored coding exams. Teachers create rooms, add question pools, manage schedules, and monitor submissions. Students join with a room code, work in a fullscreen exam workspace, and submit to an asynchronous judge pipeline.

## GitHub Codespaces demo setup

This repository includes a Codespaces-ready devcontainer.

### 1. Open in Codespaces

- Open the GitHub repository page.
- Click `Code`.
- Open the `Codespaces` tab.
- Click `Create codespace on main`.

### 2. Wait for the devcontainer setup

The Codespace will:

- start a Python 3.11 environment
- enable Docker-in-Docker
- install the Python dependencies from `requirements.txt`

### 3. Start the app

Inside the Codespaces terminal:

```bash
cd /workspaces/judge_vortex
chmod +x start_codespaces.sh start_vortex.sh stop_vortex.sh
./start_codespaces.sh
```

This starts:

- Django on port `53562`
- Kafka
- Redis
- PostgreSQL
- Nginx
- core and Java executors

Swift, Haskell, and C# executors are disabled by default in Codespaces to reduce startup time and quota usage.

### 4. Open the app

- In the `Ports` tab, open forwarded port `53562`
- If you need to share it, set the port visibility to `Public`

### 5. Stop the app

```bash
cd /workspaces/judge_vortex
./stop_vortex.sh
```

## Notes

- `start_vortex.sh` is still the main launcher
- `start_codespaces.sh` only applies lighter executor defaults for Codespaces
- for a full Linux production deployment with `isolate`, use a dedicated Linux VM rather than Codespaces
