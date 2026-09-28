# OpenKefu production deployment

Production deployment is intentionally not automatic.

Do not reuse the test deployment script for production. The test script fetches
Gitee, synchronizes the test source checkout to `origin/main`, switches releases,
and restarts services automatically. Production must keep a separate, explicit
release process so that code sync, database migration, and service restart happen
only after manual confirmation.

Build frontend assets:

```bash
cd web
npm ci
npm run build
```

Run API processes behind Nginx:

```bash
OPENKEFU_RUNTIME_ROLE=api OPENKEFU_SERVER_HOST=0.0.0.0 OPENKEFU_SERVER_PORT=8000 python run_web.py --prod --role api
```

Startup modes:

```bash
python run_web.py                 # dev mode, reload enabled
python run_web.py --test --role api # test mode, binds to 0.0.0.0:18000 by default
python run_web.py --prod --role api # production mode, binds to configured port, usually 0.0.0.0:8000
```

`OPENKEFU_SERVER_HOST`, `OPENKEFU_SERVER_PORT`, `--host`, and `--port` can override
`config.local.json` when a deployment needs a specific intranet address or port.
When test and production run on the same server, keep their ports different
for example test on `18000` and production on `8000`.

Run one or more runtime workers:

```bash
OPENKEFU_RUNTIME_ROLE=worker OPENKEFU_RUNTIME_WORKER_ID=runtime-1 python run_runtime_worker.py
```

Production requires MySQL and Redis. API processes may be scaled horizontally. Runtime
workers can also be scaled; `shop_runtime_leases` keeps one active owner per shop.

Security requirements:

- Keep same-host MySQL and Redis bound to loopback. Hardened templates are in
  `deploy/mysql/` and `deploy/redis/`.
- Use a dedicated MySQL account with only the application database privileges;
  never run the application as MySQL `root`.
- API/worker split mode rejects Redis passwords shorter than 16 characters.
- Remote MySQL is rejected unless `mysql.ssl_ca` is configured; certificate and
  hostname verification are enabled by default.
- Terminate HTTPS at Nginx or another trusted reverse proxy. The application
  emits CSP, clickjacking, MIME-sniffing, referrer, permissions, and HSTS headers.
