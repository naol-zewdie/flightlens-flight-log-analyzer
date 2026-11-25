# Deploying FlightLens

There are two ways to put FlightLens on the internet:

| | Public demo (Vercel) | Full app (container host) |
|---|---|---|
| What it is | the demo flights, read-only | your own library: uploads, reviews, notes, debriefs, video |
| Runs | the released package from PyPI as a Vercel Python function | the Docker image `ghcr.io/naol-zewdie/flightlens` |
| Data | built at deploy time, never changes | a persistent volume at `/data` |
| Login | not needed: nothing can be changed | **none built in**: put it behind access control |

## Public demo on Vercel

The demo lives in `deploy/vercel/` and needs no code from the repository
except those few files:

- `pyproject.toml` pins the released `flightlens` (its wheel includes the web
  UI) and tells Vercel the entrypoint (`app:app`) and the build script;
- `build.py` runs at deploy time and generates the 16 demo flights (each with a
  known fault), analyzed by the installed version, into `demo-data/`;
- `app.py` copies that library to `/tmp` on a cold start (SQLite needs a
  writable folder) and serves it with `read_only=True`: every upload, edit,
  review, note, debrief and delete is refused with 403.

Set up once, in the Vercel project linked to the repository:

1. **Settings → Build and Deployment → Root Directory**: `deploy/vercel`.
2. Leave the framework, build and install commands at their defaults (Vercel
   detects FastAPI and runs `python build.py` from `[tool.vercel.scripts]`).
3. Optional: a custom domain. Add it to the environment variable
   `FLIGHTLENS_ALLOWED_HOSTS` (comma-separated); the deployment's own
   `*.vercel.app` names are allowed automatically.

Every push to `main` then redeploys the demo; pull requests get preview
deployments. After each release, bump the `flightlens==` pin (and `version`)
in `deploy/vercel/pyproject.toml` so the demo runs the new version.

Sizes for 1.4.0: the packages are 78 MB as Linux wheels (pyarrow 55 MB,
numpy 17 MB), an estimated 200 MB unpacked, plus the 61 MB demo library,
against Vercel's 500 MB limit for Python; the largest response (a demo log's original download) is 2.7 MB
against the 4.5 MB response limit, every replay and chart request is under
0.7 MB.

!!! note "Why the demo is read-only"
    Vercel functions are stateless: each instance has its own `/tmp`, which is
    lost when the instance stops, and requests are capped at 4.5 MB. FlightLens's
    background analysis and its library on disk need a server that stays up,
    which is what the full app below is for.

## Full app on a container host

Any host that runs a container with a persistent volume works (Fly.io,
Render, Railway, a VPS with Docker). The image is published for every release,
for `linux/amd64` and `linux/arm64`:

```sh
docker run -d --name flightlens -p 8000:8000 \
  -v flightlens-data:/data \
  -e FLIGHTLENS_ALLOWED_HOSTS=flightlens.example.com \
  ghcr.io/naol-zewdie/flightlens:1.4.1
```

What the host needs:

- **A persistent volume at `/data`.** The library (database, original logs,
  analysis, videos) lives there; without a volume it is lost on every
  restart. Give it room for your logs and videos (a video can be up to 4 GB).
- **Port 8000**, behind the host's HTTPS.
- **`FLIGHTLENS_ALLOWED_HOSTS`** set to the public domain: FlightLens answers
  only to the host names it knows (protection against DNS rebinding).
- **The health check** at `/api/health` (the image has a Docker
  `HEALTHCHECK`; point the host's check at the same path).
- **A stop grace period of 45 s**, so running analyses can finish; unfinished
  ones resume at the next start.
- **Access control.** FlightLens has no login: anyone who can reach it can
  upload, change and delete flights. Use the host's private networking or
  access rules, or a reverse proxy that adds authentication (see
  [SECURITY.md](https://github.com/naol-zewdie/flightlens-flight-log-analyzer/blob/main/SECURITY.md)).
  To publish a library for others to look at, add `--read-only`:
  `flightlens serve --host 0.0.0.0 --read-only`.

Optional settings: `--workers N` analyses at once (one per CPU core is a good
start), `FLIGHTLENS_LOG_FORMAT=json` (the image's default) for the host's log
collector, and `/metrics` for Prometheus.

### Backups on the host

`flightlens backup` is safe while the server runs. Run it inside the container
and copy the file out:

```sh
docker exec flightlens flightlens backup /tmp/backup.zip --data /data
docker cp flightlens:/tmp/backup.zip ./flightlens-backup.zip
docker exec flightlens rm /tmp/backup.zip
```

`--no-video` leaves videos out (they are often most of the size);
`--no-analysis` leaves out the analysis results, which are rebuilt on restore. `flightlens restore` checks
every log's and video's SHA-256 and restores into a new folder. Keep backups
off the host, and try a restore now and then.

### Upgrading

Pull the new tag and restart the container on the same volume. The database
is upgraded on first start, after a safety copy is written next to it;
flights analyzed by an older version show as outdated until re-analyzed
(**Re-analyze them** on the Library page).
