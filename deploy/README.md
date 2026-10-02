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
package + the `llm` extra). To ingest, set `YIZKR_CORPUS_DIR` and
`YIZKR_EXCLUDE_FILE` in `.env` (see [The corpus and the never-ingest
list](#the-corpus-and-the-never-ingest-list)). The overlay mounts both
read-only into the one-off container, at `/corpus` and
`/corpus-exclude.txt`, and `ingest run` defaults to the corpus:

```sh
make cli ARGS="ingest run --llm anthropic --language auto"
# or, without make:
cd deploy && docker compose -f docker-compose.yml -f docker-compose.localhost.yml \
    --profile cli run --rm --user "$(id -u):$(id -g)" \
    yizkr ingest run --llm anthropic --language auto
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

## The corpus and the never-ingest list

The source documents are records about real, partly living people. They
live in a private local folder **outside the repository**, never under the
working tree, and a local never-ingest list names documents that must
never enter the pipeline at all (contact data about living people, for
instance). Two settings, in `.env` or the environment, name them:

- `YIZKR_CORPUS_DIR`: the corpus folder, the default for
  `yizkr ingest run [folder]`.
- `YIZKR_EXCLUDE_FILE`: the never-ingest list.

Use absolute paths, and keep both private (`chmod 700` the folder,
`chmod 600` the list):

```sh
YIZKR_CORPUS_DIR=/home/you/private/corpus
YIZKR_EXCLUDE_FILE=/home/you/private/corpus-exclude.txt
```

The list is a UTF-8 text file, one entry per line:

```text
# A line starting with # is a comment; blank lines are ignored.
# A 40-digit SHA-1 excludes those bytes under any file name. Anything
# after the hash is ignored, so `sha1sum FILE` output pastes in as is:
3f786850e387550fdab836ed7e6dc881de23001b  contacts.docx
# Any other line is a glob matched against the file name (not its
# folder), ignoring case:
*contacts*
```

**It fails closed.**

- `yizkr ingest run` refuses to start unless `YIZKR_EXCLUDE_FILE` is set.
  An empty file is an explicit, allowed list; no setting is not.
- A configured list that is missing or unreadable is an error, never an
  empty list. So is a line that would silently match nothing: a hash that
  is not a SHA-1 (a pasted SHA-256), or a glob followed by an inline
  comment.
- A listed document is dropped before any step reads it: no artifact, no
  transcription, no state under `var/runs/`.
- The single-file steps (`ingest register`, `transcribe`, `extract`,
  `gedcom`, `detect-faces`) are debugging tools and run without a list, but
  once one is configured they refuse a listed file. For `extract`, that is
  the media its transcription names: by its SHA-1, and by name and hash
  through `--media`, the media file, which must be the one the
  transcription names. A transcription keeps no file name, so with a list
  that names files by pattern, `extract` refuses to run without `--media`.

In the containerised CLI (`docker-compose.localhost.yml`), a set path that
does not exist stops the container from starting; nothing is created in
its place. Neither the corpus nor the list belongs in the repository;
back them up with the other private data.

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
- `public.nt.gz` + `public-exclusions.json` — the non-privileged
  export, which leaves out every possibly-living person (R7, ADR-0020).
  "Possibly living" is a runtime determination from the absence of death
  evidence, not a stored flag. Conservative by construction: a person is
  included only on positive evidence they are not living (any death
  evidence — a date, place, or cause of death, a burial place, or a
  death event — or births that all lie beyond the cutoff,
  `--cutoff-years` (by default `DEFAULT_CUTOFF_YEARS` in
  `yizkr.core.privacy`), each read at the latest year it allows:
  docs/MODELING.md, "Living persons and privacy"); *unknown means
  excluded*. Every triple that mentions an excluded person is dropped —
  including other people's claims about them and orphaned provenance
  nodes — and a statement whose value or a qualifier's value is an item
  left out goes whole, with its dates, qualifiers and references, even
  on an item kept (a reference a kept statement cites too stays with
  it, its excerpt included). Who counts as a person fails closed as well: every
  item with person evidence in the dump, at any rank (a `human` type, a
  birth, kinship, a part in a death...), is dropped unless the check
  released it, and so is every item that states no type, no
  `instance of` at normal or preferred rank that names an item (the
  vocabulary and the classes items are typed with aside), every family,
  whose people may include a living person, every event that any of
  these took part in (named as a participant, or in a participant
  statement's qualifier) or that any statement of it names (unless that
  item is left out only as untyped, or as a family whose members are all
  released; a union, or an item typed human, is no event for this), every
  person event that names no participant, every event that has
  participant statements, all of an unknown value or none, and the
  events that start and
  end a union it leaves out. Everything
  else leaves as it is, text included: whatever is recorded about a
  released person (their labels, descriptions, names, and the source
  excerpts of their statements) and about the other items it keeps
  (places, organizations, artifacts, the vocabulary, and events whose
  participants it all keeps). Only the extractor's judgement notes are
  dropped. So a text about a released person, or a document's title, can
  name a possibly living relative: the export is a non-privileged view,
  not a public copy (the owner, 2026-09-29). A dump that holds a
  sitelink or a property of a type the model does not use (a URL, a
  media file) makes the export refuse: `yizkr backup` keeps the other
  dumps, records the refusal in `manifest.json` and exits non-zero.
  Nothing here publishes anything. `public.nt.gz` is the only file of a
  backup that may be shared, and only with those a non-privileged view
  is for. `public-exclusions.json` is for curator review: it stays with
  the backup and is never shared, as it gives every item left out by
  QID, with why, a birth year and the events it took part in among the
  reasons.

  The check runs on the query service, so the export first waits (up to
  three minutes) for the query service to hold the dump's revision of
  every item and property, and **refuses to run** while it does not, or
  when the graph changes during the check: `yizkr backup` then keeps the
  other dumps, records the refusal in `manifest.json`, prints it on one
  line and exits non-zero. Run it again once the graph is at rest. The
  refusal names the entities whose revisions differ: one whose revision
  never reaches the query service keeps every export refusing until that
  entity is edited again, and a query service that lacks entities for
  good (after a restore, or a reset of the query service alone, below)
  needs a full reload from `full.nt.gz`. Pruning counts a refused run
  too: with `--keep N`, N refused nights in a row leave no public export
  among the backups kept.

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
needs a public copy, which `public.nt.gz` is not: its texts can name a
possibly living relative of a released person (#163).

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

(the WDQS updater replays recent changes only, so the rebuilt query
service can lack entities the recent changes no longer list, and the
public export refuses to run until it holds them all: import
`full.nt.gz` via loadData.sh for a full reload — see the wdqs image
docs.)

### Resetting

In the development stage, reset the instance with `yizkr scratch reset`
(docs/runbooks/scratch-run.md, Reset the instance), the only sanctioned
way: it refuses on a kept instance, removes the graph volumes and
nothing else, sets `LocalSettings.php` aside rather than deleting it,
clears the recorded instance-contents key, and repopulates the
instance. The commands below do none of that. They read no kept-state
marker, so they would destroy a kept instance too, and
`down --volumes` also deletes QuickStatements' volume (and, run with
the HTTPS overlay, traefik's certificates).

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
