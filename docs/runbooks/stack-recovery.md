# Recovering a stopped or crashed stack

When the Wikibase stack in `deploy/` has stopped uncleanly (containers
killed, the host rebooted mid-write, a service stuck in a restart loop),
recover it in this order. The order matters: the first step preserves the
volumes exactly as they were before any container touches them again, so
every later step can be undone by restoring them.

This is the generic procedure from the first recovery (#16). It holds no
instance-specific counts, hostnames or paths: archives go in your private
area, outside the repository (CLAUDE.md, "Agent execution & data
boundary"). In the development stage no routine backups are taken
(ADR-0024, "Backup policy by stage"), so the archives below are one-off
safety copies for this recovery, not a schedule.

All `docker compose` commands run from `deploy/`.

## 1. Archive every volume with nothing running

1. Make sure no `yizkr-wikibase` container is running **or will restart**.
   The services are `restart: unless-stopped`, so a container the Docker
   daemon brings back at boot or after a crash can start writing between
   your checks:

   ```sh
   docker compose stop
   docker ps -a --filter name=yizkr-wikibase \
     --format '{{.Names}} {{.Status}}'   # every line "Exited", none "Restarting"
   ```

2. Tar every `yizkr-wikibase_*` volume, with checksums:

   ```sh
   ARCHIVE=/path/to/private/area/recovery-YYYYMMDD   # outside the repo
   mkdir -p "$ARCHIVE"
   for v in $(docker volume ls -q --filter name=yizkr-wikibase_); do
     docker run --rm -v "$v":/v:ro -v "$ARCHIVE":/out alpine \
       tar -C /v -czf "/out/$v.tar.gz" .
   done
   (cd "$ARCHIVE" && sha256sum *.tar.gz > SHA256SUMS && sha256sum -c SHA256SUMS)
   ```

   Re-run the `docker ps` check afterwards: nothing may have started while
   the tarballs were written.

## 2. Start the database alone and dump it

```sh
docker compose up -d mysql
docker compose exec -T mysql \
  mariadb-dump -u"$DB_USER" -p"$DB_PASS" --single-transaction "$DB_NAME" \
  | gzip > "$ARCHIVE/database-pre-recovery.sql.gz"
(cd "$ARCHIVE" && sha256sum database-pre-recovery.sql.gz >> SHA256SUMS)
docker compose logs mysql   # crash recovery should end cleanly
```

(`DB_USER`, `DB_PASS` and `DB_NAME` come from `deploy/.env`.)

## 3. Start the full stack

```sh
make up      # from the repo root
```

`make up` starts the existing containers without recreating them and waits
for them to be healthy.

## 4. Check the query service, or rebuild it

WDQS is derived state. Check that it has caught up with the wiki: its
updater logs (`docker compose logs wdqs-updater`) should show it processing
recent changes without errors, and the counts in step 6 must agree. If it
has not caught up, reset and reload it rather than nursing it back:
deploy/README.md, "Resetting" (query service only) and "Restore" (a full
reload from `full.nt.gz`). The wiki database is the authority; rebuild the
query service from it.

## 5. Reindex CirrusSearch

The search index is derived state too, and label lookups are empty or
stale until it is rebuilt:

```sh
docker compose exec wikibase php maintenance/run.php \
  /var/www/html/extensions/CirrusSearch/maintenance/ForceSearchIndex.php
```

## 6. Smoke test and compare counts

```sh
make smoke
```

Then check that the wiki and SPARQL agree: the number of items (and
properties) the wiki holds must equal the number the query service
returns. A difference means step 4 is not done.

## 7. Take a one-off backup and verify it

```sh
uv run yizkr backup
```

Verify it: every component `manifest.json` lists is present, each `.gz`
passes `gzip -t`, and a `sha256sum` of the files, written beside them,
checks. Copy it to your private area with the volume tarballs. This is a
one-off; whether backups recur is the stage's decision (ADR-0024;
deploy/README.md, "Backups and the non-privileged export").

## 8. Clear restart-loop bloat from the WDQS container

A WDQS container that went through many killed restarts can carry a large
writable layer of leftover Jetty temporary directories. Check with
`docker ps -s --filter name=wdqs` (the SIZE column). Recreating the
container clears it; the data lives in the `wdqs-data` volume and is kept:

```sh
docker compose up -d --force-recreate wdqs
```

Re-check the counts (step 6) afterwards.

## Host pitfalls (WSL)

- **Docker Desktop's WSL integration can shadow the native engine.** When
  enabled, it takes over `/var/run/docker.sock`, so `docker` talks to
  Docker Desktop's engine and the stack's containers and volumes seem to
  have vanished while the stack itself keeps running. Check
  `docker context ls` and `docker info` (the server name) before concluding
  anything is lost, and turn the integration off for the distribution that
  runs the stack.
- **WSL regenerates `/etc/hosts` at boot**, which drops the entries you
  added for the stack's hostnames (the hosts set in `deploy/.env`). Host-side
  commands such as `yizkr backup` then fail to resolve the wiki. Set
  `generateHosts = false` under `[network]` in `/etc/wsl.conf` (a one-time
  sudo step), or use the in-network route (deploy/README.md, "Run on a
  single host without editing /etc/hosts").
