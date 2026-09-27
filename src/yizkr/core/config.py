"""Connection configuration, shared by every tool that talks to the stack.

Values come from the environment first, then an env file with the same
variables the deployment itself uses (deploy/.env):
WIKIBASE_PUBLIC_HOST, WDQS_PUBLIC_HOST, PUBLIC_SCHEME, HTTP_PORT,
MW_ADMIN_NAME, MW_ADMIN_PASS. WIKIBASE_URL / WDQS_SPARQL_URL override
the derived URLs directly.

The env file is located by, in order: an explicit --env-file argument,
$YIZKR_ENV_FILE, ./deploy/.env, ./.env (relative to the working
directory — run tools from the repo root, as the Makefile does).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from yizkr import __version__

USER_AGENT = f"yizkr/{__version__} (family-archive tooling)"

ENV_FILE_CANDIDATES = (Path("deploy/.env"), Path(".env"))


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser matching the .env format compose uses."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def resolve_env_file(explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        return explicit
    env_var = os.environ.get("YIZKR_ENV_FILE")
    if env_var:
        return Path(env_var)
    for candidate in ENV_FILE_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def env_setting(key: str, env_file: Path | None = None) -> str:
    """One setting, read like Config.load reads its values: the
    environment first, then the env file. Empty when set in neither."""
    value = os.environ.get(key)
    if value:
        return value
    path = resolve_env_file(env_file)
    return read_env_file(path).get(key, "") if path else ""


class ConfigError(SystemExit):
    """Missing configuration; message tells the user what to set."""


@dataclass(frozen=True)
class Config:
    """Resolved connection settings for one Wikibase Suite instance."""

    wikibase_url: str
    concept_uri: str
    admin_name: str
    admin_pass: str
    wdqs_url: str | None
    sparql_url: str | None
    env_file: Path | None

    @classmethod
    def load(
        cls, env_file: Path | None = None, require_admin: bool = True
    ) -> Config:
        env_file = resolve_env_file(env_file)
        file_values = read_env_file(env_file) if env_file else {}

        def get(key: str, default: str = "") -> str:
            return os.environ.get(key) or file_values.get(key) or default

        scheme = get("PUBLIC_SCHEME", "http")
        wikibase_host = get("WIKIBASE_PUBLIC_HOST")
        wdqs_host = get("WDQS_PUBLIC_HOST")
        port = get("HTTP_PORT", "80")
        port_suffix = (
            "" if port in ("80", "") or scheme == "https" else f":{port}"
        )

        wikibase_url = get("WIKIBASE_URL") or (
            f"{scheme}://{wikibase_host}{port_suffix}" if wikibase_host else ""
        )
        wdqs_url = (
            f"{scheme}://{wdqs_host}{port_suffix}" if wdqs_host else None
        )
        sparql_url = get("WDQS_SPARQL_URL") or (
            f"{wdqs_url}/sparql" if wdqs_url else None
        )

        missing = []
        if not wikibase_url:
            missing.append("WIKIBASE_URL or WIKIBASE_PUBLIC_HOST")
        admin_name = get("MW_ADMIN_NAME")
        admin_pass = get("MW_ADMIN_PASS")
        if require_admin and not admin_name:
            missing.append("MW_ADMIN_NAME")
        if require_admin and not admin_pass:
            missing.append("MW_ADMIN_PASS")
        if missing:
            location = env_file or "deploy/.env (not found)"
            raise ConfigError(
                f"Missing configuration: {missing}. Set the variables in the "
                f"environment or in {location}."
            )

        # The concept URI is an identifier prefix baked into the triples:
        # scheme://host, never a port (matches WIKIBASE_CONCEPT_URI in the
        # compose file).
        concept_uri = (
            f"{scheme}://{wikibase_host}" if wikibase_host else wikibase_url
        )
        return cls(
            wikibase_url=wikibase_url,
            concept_uri=concept_uri,
            admin_name=admin_name,
            admin_pass=admin_pass,
            wdqs_url=wdqs_url,
            sparql_url=sparql_url,
            env_file=env_file,
        )
