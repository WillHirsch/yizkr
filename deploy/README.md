# Wikibase Suite deployment runbook

Runs the project's system of record (ADR-0001) on a home server with
Docker Compose: MediaWiki + Wikibase, the WDQS SPARQL query service
with its updater and frontend, Elasticsearch, QuickStatements, MariaDB,
and a traefik reverse proxy.

Adapted from [Wikibase Suite Deploy] 7.0.0. Differences: LAN/HTTP-first
(no TLS by default — this archive is privacy-first and nothing is
public by default), and the anonymous usage-metadata callback to
Wikimedia Deutschland is off by default.

[Wikibase Suite Deploy]: https://github.com/wmde/wikibase-release-pipeline/tree/main/deploy

## Prerequisites

- Docker Engine with Compose v2 on a 64-bit Linux host.
- ~4 GB free RAM for the stack (Elasticsearch and Blazegraph are the
  hungry ones) and a few GB of disk to start; artifact media grows over
  time.
- Two hostnames that resolve to the server, one for the wiki and one
  for the query service — traefik routes by `Host` header, so an IP
  address alone is not enough. On a LAN, add entries to your router's
  DNS/Pi-hole, or to each client's `/etc/hosts`:

  ```
  192.168.1.50  wikibase.lan query.wikibase.lan
  ```

## First boot

```sh
cd deploy
cp .env.example .env
$EDITOR .env      # set hosts, admin account, DB_PASS — see comments
docker compose up -d
```

The first boot installs MediaWiki and takes a few minutes. Follow along
with `docker compose ps` (wait for `wikibase` to be `healthy`) or
`docker compose logs -f wikibase`.

Once healthy:

- Wiki: `http://WIKIBASE_PUBLIC_HOST/` (log in as `MW_ADMIN_NAME`)
- Query service UI: `http://WDQS_PUBLIC_HOST/`
- SPARQL endpoint: `http://WDQS_PUBLIC_HOST/sparql`
- QuickStatements: `http://WIKIBASE_PUBLIC_HOST/tools/quickstatements/`

> **Warning:** `.env` values are *initial setup* values. After first
> boot, changing hosts, scheme, admin or DB credentials requires a
> reset (below). Pick hostnames you can keep.

## Run on a single host without editing /etc/hosts

The two hostnames exist so traefik can tell the wiki and query service
apart (it routes by `Host` header) — they do not require two machines, and
they do not require touching your system's `/etc/hosts`. The overlay
`docker-compose.localhost.yml` makes traefik answer to both names *inside*
the Docker network (via network aliases) and adds a containerised `yizkr`
CLI that runs there, so every command that talks to the stack resolves the
names with no host configuration.

Set the two hostnames and `PUBLIC_SCHEME=http` in `.env` as usual (any
names work; if you are restoring a backup, reuse the names it was created
under so the baked concept URIs match — see Restore). Then:

```sh
make up-local                 # stack up; hostnames resolve in-network
make cli ARGS="smoke-test"     # then bootstrap, backup, publish-site, ...
make cli ARGS="bootstrap"
```

`make cli` runs the `yizkr` image built from the repo's `Dockerfile` (base
package + the `llm` extra). To ingest, mount the input folder into the
one-off container:

```sh
cd deploy && docker compose -f docker-compose.yml -f docker-compose.localhost.yml \
    --profile cli run --rm --user "$(id -u):$(id -g)" -v /path/to/documents:/data \
    yizkr ingest run /data --llm anthropic --language auto
```

`--user "$(id -u):$(id -g)"` makes the outputs the CLI writes into `../var`
land with your ownership rather than root's. `make cli` / `make up-local`
supply this automatically (via the `user:` line in the overlay); pass it
explicitly on raw `docker compose run` invocations. (`--llm claude-code`
needs that CLI, which is not in the image — run those ingests from the
host.)

The DB/media restore steps (below) use `docker compose exec mysql|wikibase`
and run *inside* containers, so they never needed host DNS in the first
place; the overlay is about the `yizkr` HTTP client.

**The browser is the only client still outside Docker's DNS.** To view the
live wiki / query UIs without host config: browse the static site instead
(`make cli ARGS="publish-site"`, then serve `var/site` on a plain port — no
hostnames at all); or use `*.localhost` names, which browsers resolve to
loopback automatically (but that is a different concept URI than a `.lan`
backup, so only for a fresh instance); or add the two `/etc/hosts` lines
just for the browser.

## Smoke test

Verifies item creation via the API and the SPARQL round-trip through
the wdqs-updater. Idempotent — re-runs reuse the marker item it
creates. It reads `deploy/.env` for hosts and admin credentials.

From the repo root (see the root README for uv setup):

```sh
make smoke            # = uv run yizkr smoke-test
```

Expected output ends with `SMOKE TEST PASSED`. The WDQS round-trip
normally completes in well under a minute; the test waits up to
5 minutes (`--timeout` to change).

## HTTPS / public exposure (optional, off by default)

Nothing in this archive is public by default (it is privacy-first) —
only do this deliberately. Requirements: public DNS for both hosts pointing
at this machine, ports 80/443 reachable from the internet.

1. In `.env`, set `PUBLIC_SCHEME=https` **before first boot** (the
   scheme is baked into WDQS entity URIs; changing it later needs a
   WDQS reset).
2. Start with the overlay:

   ```sh
   docker compose -f docker-compose.yml -f docker-compose.https.yml up -d
   ```

Certificates come from Let's Encrypt via traefik (HTTP-01 challenge).
Test against the staging CA first by uncommenting the `caserver` line
in `docker-compose.https.yml` to avoid rate limits.

## Operations

- **Status / logs:** `docker compose ps`,
  `docker compose logs -f <service>`
- **Stop / start:** `docker compose down` / `docker compose up -d`
  (data lives in named volumes and survives `down`)
- **Upgrades:** images are pinned to major versions (`wikibase:7`,
  `wdqs:2`, ...). Before upgrading majors, read the upstream release
  notes — MediaWiki upgrades can require `update.php` runs. Take a
  backup first (`yizkr backup`, below).
- **Generated config:** first boot writes `config/LocalSettings.php`,
  `config/wdqs-frontend-config.json`, and `config/wikibase-php.ini`
  (all gitignored). Local settings tweaks go in
  `config/Extensions.php` / `LocalSettings.d` overrides, not upstream
  files.

### Backups and the public export

`yizkr backup` (run from the repo root, stack up) writes a timestamped
directory under `var/backups/` containing:

- `database.sql.gz` — mariadb dump. **The authoritative backup**:
  items, statements, full edit history, users.
- `media.tar.gz` — uploaded artifact files.
- `full.nt.gz` — complete RDF dump. Portable export; also what a full
  WDQS reload imports (WDQS state itself is derived and not backed up).
- `public.nt.gz` + `public-exclusions.json` — the possibly-living-
  excluded export (R7, ADR-0020). "Possibly living" is a runtime
  determination from the absence of death evidence, not a stored flag.
  Conservative by construction: a person is included only on positive
  evidence they are not living (any death evidence — a date, place, or
  cause of death, a burial place, or a death event — or birth beyond the
  100-year cutoff); *unknown means excluded*. Every triple that
  mentions an excluded person is dropped — including other people's
  claims about them and orphaned provenance nodes — and transcription
  excerpts (raw source quotations) never enter the public tier at all.
  The exclusions file records who was excluded and why, for curator
  review. Nothing here publishes anything: the file is an artifact you
  may choose to publish.

Schedule it with cron on the host, e.g. nightly at 03:15 keeping two
weeks:

```cron
15 3 * * * cd /path/to/yizkr && uv run yizkr backup --keep 14 >> var/backups/backup.log 2>&1
```

The static site (frontend ladder rung 2) regenerates the same way —
nightly after the backup, for example:

```cron
45 3 * * * cd /path/to/yizkr && uv run yizkr publish-site --clean >> var/site.log 2>&1
```

`var/site` is built from the LIVE graph and therefore includes living
people: serve it on the LAN only (any static server works, e.g.
`python -m http.server -d var/site 8080`). An internet-facing variant
must be built from the public export instead.

Copy `var/backups/` off-machine (rsync, restic, a NAS — anything);
a backup that lives only on the server it backs up is not a backup.

**Restore** (new or broken instance):

```sh
docker compose up -d                     # fresh stack, healthy
gunzip -c database.sql.gz | docker compose exec -T mysql \
  mariadb -u"$DB_USER" -p"$DB_PASS" "$DB_NAME"
docker compose exec -T wikibase tar -C /var/www/html -xzf - < media.tar.gz
docker compose exec wikibase php maintenance/run.php update --quick
# rebuild the search index (derived state; wbsearchentities and every
# label lookup are empty until this runs):
docker compose exec wikibase php maintenance/run.php \
  /var/www/html/extensions/CirrusSearch/maintenance/ForceSearchIndex.php
# rebuild the query service from the restored wiki:
docker compose stop wdqs wdqs-updater
docker compose rm -f wdqs wdqs-updater   # stopped containers pin the volume
docker volume rm yizkr-wikibase_wdqs-data
docker compose up -d
```

(the WDQS updater replays recent changes; for large graphs import
`full.nt.gz` via loadData.sh instead — see the wdqs image docs.)

### Resetting

Destroys all wiki content and rebuilds from `.env`. Only for a broken
first install or a deliberate start-over — **this deletes the graph**.

```sh
docker compose down --volumes
rm -f config/LocalSettings.php
docker compose up -d
```

To reset only the query service (e.g. after changing the concept URI —
re-indexes from the wiki's RecentChanges, so only recent edits are
replayed; a full reload needs an RDF dump import):

```sh
docker compose stop wdqs wdqs-updater
docker volume rm yizkr-wikibase_wdqs-data
docker compose up -d
```

## Troubleshooting

- **`wikibase` never becomes healthy:** `docker compose logs wikibase`.
  Most commonly a missing required `.env` value (empty
  `MW_ADMIN_PASS`, `DB_PASS`) or a password shorter than MediaWiki's
  10-character minimum.
- **Elasticsearch restarts / exits 78:** the host may need
  `sysctl -w vm.max_map_count=262144` (persist in
  `/etc/sysctl.d/99-elasticsearch.conf`).
- **Blazegraph aborts with "unable to allocate file descriptor
  table":** the compose file already raises `nofile` ulimits; check
  your Docker daemon doesn't override them lower.
- **Items never appear in SPARQL:** `docker compose logs wdqs-updater`.
  The updater must be able to reach the `wikibase` service and its
  `WIKIBASE_CONCEPT_URI` must match `MW_WG_SERVER` (both are derived
  from the same `.env` values here, so drift means `.env` was changed
  after first boot).
- **404s or wrong site answered:** the `Host` header didn't match
  either configured hostname — check DNS/hosts entries point at the
  server and match `.env` exactly.
