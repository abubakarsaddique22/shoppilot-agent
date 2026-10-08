# ShopPilot runbook (Step Y)

One EC2 host, Docker Compose with three containers: `caddy` (80/443, UI files, reverse proxy), `api` (FastAPI + LangGraph) and `db` (Postgres + pgvector). Only Caddy publishes ports. Secrets live in SSM Parameter Store and reach the server as `/run/shoppilot.env` (tmpfs, gone after a reboot).

On the server the user is `ubuntu`. `dc` is a shortcut for `docker compose -f /opt/shoppilot/docker-compose.prod.yml --env-file /run/shoppilot.env` (set in `.bashrc` by `userdata.sh`; log in again after the first boot).

## 1. First deploy (once, after Step X)

1. Launch the instance with `infra/aws/userdata.sh` as User data (set `REPO_URL` first). Wait 3 to 5 minutes, then connect (Session Manager or SSH).
2. Check the boot: `tail -n 5 /var/log/cloud-init-output.log` ends with `userdata finished`.
3. Deploy the first version:

```
cd /opt/shoppilot
bash infra/aws/deploy.sh main
```

4. Fill the knowledge base, then create the real users (never use `--demo` here, it refuses to run in prod):

```
dc exec api python scripts/ingest_policies.py
dc exec api python scripts/seed_mockshop.py
dc exec -it api python scripts/create_user.py --email manager@your-domain.com --role manager
```

`seed_mockshop.py` drops and recreates the MockShop tables with fake data. Run it only on this first deploy.

5. Install the scheduled jobs: `crontab /opt/shoppilot/infra/aws/crontab.txt`
6. Check from your laptop: `http://<public-ip>/api/ready` must show `"status":"ready"`.

Private repository? `git clone` in `userdata.sh` needs a read-only deploy key (GitHub: Settings > Deploy keys). Easiest for a portfolio: keep the repository public (no secrets are in it).

## 2. Normal deploy

Push to `main`. When the `ci` workflow is green, the `deploy` workflow runs `deploy.sh <sha>` on the instance through SSM. Watch it in the Actions tab. By hand on the server: `bash infra/aws/deploy.sh <tag-or-sha>`.

`deploy.sh` stops with an error (and prints the API logs and the rollback command) when `/ready` does not answer.

## 3. Rollback

Deploy the previous good version. Every image is tagged with the Git SHA.

```
bash infra/aws/deploy.sh <previous-sha-or-tag>
```

Or in GitHub: Actions > deploy > Run workflow > enter the SHA. Migrations must stay backward compatible (add columns, never rename or drop in the same release). If a migration cannot be undone, restore the last backup (section 5).

## 4. Secrets

The source of truth is GitHub (repository secrets and variables, listed at the top of `.github/workflows/deploy.yml`). Every deploy copies them to SSM, so change a value in GitHub and run the deploy workflow again. Exception: `PG_PASSWORD` must not change after the first deploy (Postgres keeps the password of its first start).

Parameters live under `/shoppilot/prod/` (see `infra/aws/fetch_secrets.sh` for the required ones: `POSTGRES_PASSWORD`, `JWT_SECRET`, `LLM_API_KEY`; also `LANGSMITH_API_KEY`, `S3_BUCKET`, `WEBHOOK_SECRET`, `OWNER_EMAIL`).

- Values may not contain a space or any of `$ # " ' \`. Make random ones with hex: `openssl rand -hex 32`.
- After you change a parameter: `bash infra/aws/fetch_secrets.sh` and `dc up -d` (the API reads the file only when its container starts).
- After a reboot the file is missing: cron `@reboot` writes it again. By hand: `bash infra/aws/fetch_secrets.sh`.

## 5. Backups and restore test

Nightly at 02:15 `scripts/backup.sh` writes `s3://<bucket>/backups/postgres/<date>.sql.gz`. Run it once by hand: `bash scripts/backup.sh`.

Restore test (into a scratch database, the real one is not touched). Do this once before you call the project done:

```
aws s3 ls s3://<bucket>/backups/postgres/
dc exec -T db psql -U shop -d postgres -c "CREATE DATABASE restore_test"
aws s3 cp s3://<bucket>/backups/postgres/<file>.sql.gz - | gunzip | dc exec -T db psql -U shop -d restore_test
dc exec -T db psql -U shop -d restore_test -c "select count(*) from orders"
dc exec -T db psql -U shop -d postgres -c "DROP DATABASE restore_test"
```

A real restore after a disaster: stop the API (`dc stop api`), drop and recreate `shoppilot`, load the file the same way, `dc start api`.

## 6. Daily checks

- `dc ps` : three containers `running`, `db` and `api` `healthy`.
- `dc logs --tail 100 api` : application logs (JSON, one per line).
- `tail -n 50 ~/shoppilot-cron.log` : backups, report and low-stock jobs.
- `df -h /` and `free -m` : the disk must stay below 80 percent. `docker system prune -f` removes unused data.

## 7. Cost routine

- Finished working or recording the demo: stop the instance (EC2 console > Instance state > Stop). A stopped instance keeps its disk, which is cheap.
- After a stop and start the public IP changes (without an Elastic IP). Release any Elastic IP that is not attached to a running instance.
- Delete unattached EBS volumes and old snapshots. Check Billing once a week against the Budget alert.
- After a start: wait a minute, then `bash infra/aws/fetch_secrets.sh` happens by itself (cron `@reboot`); check `/api/ready`.

## 8. Known limits

- One small instance: no high availability. A deploy restarts the API for a few seconds.
- Without a domain the demo runs over HTTP (login only). Set a domain in `infra/caddy/Caddyfile` (replace `:80`) to get automatic HTTPS.
- MockShop is the store backend. The Shopify backend is not connected in production yet.
