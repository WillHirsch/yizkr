# Recovering a stopped or crashed stack

When the Wikibase stack in `deploy/` has stopped uncleanly (containers
killed, the host rebooted mid-write, a service stuck in a restart
loop), recover it in this order. The order matters: the first step
preserves the volumes exactly as they were before any container
touches them again, so every later step can be undone by restoring
them (step 1, item 3).

This is the generic procedure from the first recovery (#16). It holds
no instance-specific counts, hostnames or paths: archives go in your
private area, outside the repository (CLAUDE.md, "Agent execution &
data boundary"). The volume archives and the dump in steps 1 and 2
are one-off safety copies for this recovery, not a schedule. In the
development stage no backup is taken unless a decision asks for one
(ADR-0024, "Backup policy by stage"), and none is ever made of a
scratch graph (ADR-0025 §1), so step 7 depends on the stage.

All `docker compose` commands run from `deploy/`; `make` and
`uv run yizkr` commands run from the repo root.

## 1. Archive every volume with nothing running

1. Make sure no `yizkr-wikibase` container is running **or will
   restart**. The services are `restart: unless-stopped`, and
   `docker compose stop` marks only the containers it stops as
   stopped by hand, so an engine or WSL restart brings an
   already-exited one back; switch the policy off for the recovery
   (step 3 puts it back):

   ```sh
   docker compose stop
   docker update --restart=no \
     $(docker ps -aq --filter name=yizkr-wikibase)
   # every line "Exited", none "Restarting":
   docker ps -a --filter name=yizkr-wikibase \
     --format '{{.Names}} {{.Status}}'
   ```

2. Tar every `yizkr-wikibase_*` volume, with checksums:

   ```sh
   ARCHIVE=/path/to/private/area/recovery-YYYYMMDD   # outside the repo
   mkdir -p "$ARCHIVE"
   for v in $(docker volume ls -q --filter name=yizkr-wikibase_); do
     docker run --rm -v "$v":/v:ro -v "$ARCHIVE":/out alpine \
       tar -C /v -czf "/out/$v.tar.gz" .
   done
   (cd "$ARCHIVE" && sha256sum *.tar.gz > SHA256SUMS \
     && sha256sum -c SHA256SUMS)
   ```

   Re-run the `docker ps` check afterwards: nothing may have started
   while the tarballs were written.

3. To undo any later step, put the volumes back as archived: stop the
   stack and repeat the `docker ps` check, verify the archive
   (`sha256sum -c SHA256SUMS` in it), run `docker compose up --no-start`
   (it recreates, unstarted, any volume a later step removed), then
   restore each volume:

   ```sh
   for t in "$ARCHIVE"/yizkr-wikibase_*.tar.gz; do
     v=$(basename "$t" .tar.gz)
     docker run --rm -v "$v":/v -v "$ARCHIVE":/in:ro alpine sh -c \
       'find /v -mindepth 1 -delete && tar -C /v -xzf "/in/$1.tar.gz"' \
       sh "$v"
   done
   ```

   The volume name goes in as `$1`: inside the single quotes, a bare
   `$v` is the container's and empty.

## 2. Start the database alone and dump it

```sh
# returns once its healthcheck passes:
docker compose up -d --wait --no-recreate mysql
docker compose logs mysql   # crash recovery should end cleanly
docker compose exec -T mysql sh -c 'mariadb-dump -u"$MYSQL_USER" \
  -p"$MYSQL_PASSWORD" --single-transaction "$MYSQL_DATABASE"' \
  | gzip > "$ARCHIVE/database-pre-recovery.sql.gz"
# the last line must read "-- Dump completed":
zcat "$ARCHIVE/database-pre-recovery.sql.gz" | tail -n 1
(cd "$ARCHIVE" && sha256sum database-pre-recovery.sql.gz >> SHA256SUMS)
```

The single quotes leave `MYSQL_*` to the container's shell, where
compose sets them from `DB_USER`, `DB_PASS` and `DB_NAME`. Don't
source `deploy/.env` into your shell: it is compose syntax, not shell.
Record the checksum only once the last line reads `-- Dump completed`;
an empty or cut-off file means the dump failed (gzip writes a file
either way), so take it again before step 3.

## 3. Start the full stack

```sh
docker compose up -d --wait --no-recreate
docker update --restart=unless-stopped \
  $(docker ps -aq --filter name=yizkr-wikibase)
```

`--no-recreate` starts the existing containers as they are, writable
layers included, even where an image, a compose file or `.env` has
changed since they were made; a plain `make up` would recreate those
(traefik too, on a stack started with `make up-local`). The second
line puts back the restart policy step 1 switched off.

## 4. Check the query service, or rebuild it

WDQS is derived state. Check that it has caught up with the wiki: its
updater logs (`docker compose logs wdqs-updater`) should show it
processing recent changes without errors, and the counts in step 6
must agree. If it has not caught up, reset it:

```sh
docker compose stop wdqs wdqs-updater
# stopped containers pin the volume:
docker compose rm -f wdqs wdqs-updater
docker volume rm yizkr-wikibase_wdqs-data
docker compose up -d
```

The updater then replays only recent changes. If step 6's counts still
differ, reload it in full from a fresh RDF dump (the development stage
keeps no backups):

```sh
docker compose exec -T wikibase php maintenance/run.php \
  ./extensions/Wikibase/repo/maintenance/dumpRdf.php --format nt \
  | gzip > "$ARCHIVE/full.nt.gz"
```

Then import it as deploy/README.md, "Restore" describes. The wiki
database is the authority; rebuild the query service from it.

## 5. Reindex CirrusSearch

The search index is derived state too, and label lookups are empty or
stale until it is rebuilt:

```sh
docker compose exec wikibase php maintenance/run.php \
  /var/www/html/extensions/CirrusSearch/maintenance/ForceSearchIndex.php
```

## 6. Smoke test and compare counts

```sh
make smoke   # from the repo root
```

Then check that the wiki and SPARQL agree. Count the wiki's items and
properties (non-redirect pages by content model):

```sh
docker compose exec -T mysql sh -c 'mariadb -u"$MYSQL_USER" \
  -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -e "SELECT page_content_model,
  COUNT(*) FROM page WHERE page_is_redirect = 0 AND page_content_model
  IN ('"'"'wikibase-item'"'"','"'"'wikibase-property'"'"')
  GROUP BY page_content_model"'
```

Count the query service's entities by `schema:version` (its updater
drops the `wikibase:Item` type, so counting `?item a wikibase:Item`
gives 0):

```sh
docker compose exec -T wdqs curl -s --get -H 'Accept: text/csv' \
  --data-urlencode 'query=SELECT ?kind (COUNT(?e) AS ?n) WHERE {
    ?e <http://schema.org/version> ?v .
    FILTER NOT EXISTS { ?e <http://www.w3.org/2002/07/owl#sameAs> ?t }
    BIND(SUBSTR(STRAFTER(STR(?e), "/entity/"), 1, 1) AS ?kind)
  } GROUP BY ?kind' \
  http://localhost:9999/bigdata/namespace/wdq/sparql
```

The `wikibase-item` count must equal `Q`, and `wikibase-property` must
equal `P`. A difference means step 4 is not done.

## 7. Back up only where the stage or a decision asks for one

In the development stage, take no backup here unless an owner decision
asks for one, as the archive-and-reset decision did at the first
recovery (#16), and never of a scratch graph: nothing in one is kept,
published or exported (ADR-0025 §1; CLAUDE.md, "Development stage").
A scratch graph is regenerated instead: `yizkr scratch reset`
(docs/runbooks/scratch-run.md, Reset the instance), then the next run.
From the kept-state stage on, take a one-off backup and verify it:

```sh
uv run yizkr backup   # from the repo root
```

Verify it: every component `manifest.json` lists is present, each
`.gz` passes `gzip -t`, and a `sha256sum` of the files, written beside
them, checks. Copy it to your private area with the volume tarballs.
Whether backups recur is the stage's decision (ADR-0024;
deploy/README.md, "Backups and the non-privileged export").

## 8. Clear restart-loop bloat from the WDQS container

A WDQS container that went through many killed restarts can carry a
large writable layer of leftover Jetty temporary directories. Check
with `docker ps -s --filter name=wdqs` (the SIZE column). Recreating
the container clears it; the data lives in the `wdqs-data` volume and
is kept:

```sh
docker compose up -d --force-recreate --no-deps --wait wdqs
```

`--no-deps` leaves wikibase, mysql and elasticsearch untouched.
Re-check the counts (step 6) afterwards.

## Host pitfalls (WSL)

- **Docker Desktop's WSL integration can shadow the native engine.**
  When enabled, it takes over `/var/run/docker.sock`, so `docker` talks
  to Docker Desktop's engine and the stack's containers and volumes
  seem to have vanished while the stack itself keeps running. Check
  `docker context ls` and `docker info` (the server name) before
  concluding anything is lost, and turn the integration off for the
  distribution that runs the stack. That doesn't give the native
  engine its socket back: restore it with
  `sudo systemctl restart docker.socket`, which restarts the daemon and
  so brings back `unless-stopped` containers, so redo step 1's check.
- **WSL regenerates `/etc/hosts` at boot**, which drops the entries you
  added for the stack's hostnames (the hosts set in `deploy/.env`).
  Host-side commands that reach the stack by name, such as `make smoke`
  and the non-privileged export of `yizkr backup`, then fail to resolve
  those names. Re-add the entries, then keep them across boots: set
  `generateHosts = false` under `[network]` in `/etc/wsl.conf` (a
  one-time sudo step, read at the next boot), since that stops the
  regeneration but restores nothing. Without them,
  `uv run yizkr backup --no-nonprivileged-export` still makes the three
  dumps, as they run through `docker compose exec` and only the export
  reaches the wiki and the query service by name. The smoke test can
  run in-network instead (`make up-local`, then
  `make cli ARGS="smoke-test"`; deploy/README.md, "Run on a single host
  without editing /etc/hosts"), but `yizkr backup` cannot: the CLI
  image holds no `deploy/` and no docker CLI.
