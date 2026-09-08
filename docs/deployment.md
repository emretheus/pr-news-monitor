# Docker deployment notes

Start Docker Desktop or Docker Engine with Compose, configure `.env`, then run `docker compose up --build -d --wait` from the project root. Open http://localhost:8501. If that port is occupied, set `NEWS_MONITOR_PORT=8502` in `.env` before starting and use http://localhost:8502.

## Everyday commands

```sh
docker compose ps
docker compose logs --tail=100 app
docker compose down
docker compose up -d --wait
```

`down` keeps the named volume. `down --volumes` deletes it and all stored coverage. The container uses `/app/data/news.db`, separate from local `data/news.db`; Compose intentionally fixes that path so local overrides cannot bypass persistence.

Edit `config.yaml`, then `docker compose restart app`. After editing credentials or the port in `.env`, use `docker compose up -d --force-recreate --wait`. Restarting alone does not reload Compose environment values. Rebuild after code changes.

## Why this packaging

- Python `3.12.13-slim-bookworm` pins the interpreter patch and Debian family. The base tag can receive OS updates; exact image reproducibility would additionally need a digest and package hashes.
- `requirements.lock` pins transitive dependencies. Installing them before copying code lets Docker cache that layer. The verified Linux ARM64 build needs no compiler or extra system packages.
- UID/GID 10001 owns `/app/data`. A new named volume inherits those permissions, allowing the non-root app to write SQLite. Arbitrary host bind mounts may need different ownership.
- `.dockerignore` allows only required application inputs. `.env`, local databases, Git metadata, and caches stay outside the build context. Compose passes credentials at runtime. Docker operators can inspect environment values, so public production needs a managed secret store.
- Streamlit binds to `0.0.0.0` inside Docker; Compose exposes it only on host loopback. The Python health check calls `/_stcore/health`, following [Streamlit's guidance](https://docs.streamlit.io/deploy/tutorials/docker). It checks the server, not external integrations.
- `init: true` forwards signals and reaps children. The restart policy handles process exits, but an unhealthy health check alone does not restart the process. One replica matches SQLite and the process-wide refresh lock.

To update dependencies deliberately:

```sh
uv pip compile requirements.txt --python-version 3.12 --universal --output-file requirements.lock
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
docker compose up --build -d --wait
```

Local Docker is verified in [integration evidence](integration-check.md). Streamlit Cloud remains optional and unverified. Its local SQLite file would be a rebuildable demo cache rather than the intended durable deployment storage. Docker's [named volume behavior](https://docs.docker.com/engine/storage/volumes/) provides the persistence used here.
