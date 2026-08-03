# Windows Native Runtime

The repository includes a native Windows entry point that keeps every runtime
file outside the checkout. It does not start Docker or WSL. The default runtime
root is `%LOCALAPPDATA%\AshareAI\runtime`; override it with
`ASHARE_NATIVE_ROOT` or `-Root`, but keep it outside the source directory.

## Install and Run

Run PowerShell from the repository root:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\native\ashare-native.cmd install
.\scripts\native\ashare-native.cmd start
.\scripts\native\ashare-native.cmd status
```

The installer validates the locked PostgreSQL and Redis-compatible archives
before extraction, checks out the pinned SearXNG source commit, creates the
external Python environment, and builds `web/` into the external runtime root.
The checksum source for an archive is either the lock entry, the vendor
checksum URL, or the fixed GitHub release asset digest. A missing trusted
checksum is an installation error; the installer never silently accepts an
unverified archive.

The generated administrator credentials are written only to
`<runtime>\config\admin-credentials.txt`. Model credentials are entered later
through the existing administrator model-settings page and are stored by the
same encrypted database path as the Docker deployment.

Installation creates the local non-administrator `AshareAIService` account and
runs PostgreSQL, Redis, SearXNG, API, and workers with that identity. The
installer grants it access only to the external runtime and the host Python
installation used to create the venv. Port selection is host-aware: the chosen
PostgreSQL, Redis, API, and SearXNG ports are persisted in
`<runtime>\config\native-ports.json` and reused on every restart.

The native API serves the built SPA from the same origin, so browser Cookie,
CSRF, SSE, queue, search, cache, and model-settings behavior remains unchanged.
The `web` status entry is explicitly marked `embedded_in=api`; this avoids a
second static server and preserves same-origin requests without adding a
reverse-proxy process.

## Lifecycle

```powershell
.\scripts\native\ashare-native.cmd doctor
.\scripts\native\ashare-native.cmd status -Json
.\scripts\native\ashare-native.cmd stop
.\scripts\native\ashare-native.cmd start -ResearchMode DUAL -ResearchWorkers 2
```

`status` reads only the PIDs recorded by the native entry point. Its
`NATIVE_PROCESS_GROUP` total is the sum of distinct Windows `WorkingSet64`
values for the managed process trees, which is the native equivalent of the
Docker cgroup working-set measurement. Logs are in `<runtime>\logs`; PostgreSQL,
Redis, objects, lake files, the SearXNG checkout, Python environment and SPA
assets are all under `<runtime>`.

The native lock is intentionally kept in Git at
`scripts/native/dependencies.lock.json`. Downloaded archives, generated
configuration, credentials, databases, logs and build output are not repository
artifacts.
