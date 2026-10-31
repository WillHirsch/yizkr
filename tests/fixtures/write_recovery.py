"""Authoritative recovery evidence for the existing in-memory wiki fixtures.

The page store models create-only reservations. Item creation evidence comes
from the fake's actual writes, never from labels or expected registry results.
Real API semantics are exercised separately by the required fictional lane.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from yizkr.core.wiki import EntityCreation, RecoveryEvidenceError, WikiPage
from yizkr.core.write_recovery import GENERATION_PAGE, Generation


class RecoveryWiki:
    def initialize_recovery(self) -> None:
        self._recovery_pages: dict[str, WikiPage] = {}
        self._recovery_edited: set[str] = set()
        self._recovery_page_lock = threading.RLock()
        self._recovery_revision = 0
        if not hasattr(self, "config"):
            self.config = SimpleNamespace(
                concept_uri="https://fictional.invalid"
            )
        # Explicit disposable-fixture setup precedes any authored entities.
        # Tests of legacy migration remove this page before making their graph.
        generation = Generation(
            deployment_id="1" * 32,
            generation="2" * 32,
            instance_uri=self.config.concept_uri,
        )
        self.create_page_once(
            GENERATION_PAGE,
            json.dumps(generation.model_dump(mode="json"), sort_keys=True),
            "fixture: establish disposable deployment generation",
        )

    def immutable_page(self, title: str) -> WikiPage | None:
        with self._recovery_page_lock:
            if title in self._recovery_edited:
                raise RecoveryEvidenceError("recovery page has been edited")
            return self._recovery_pages.get(title)

    def create_page_once(
        self, title: str, text: str, summary: str
    ) -> WikiPage:
        with self._recovery_page_lock:
            if title not in self._recovery_pages:
                self._recovery_revision += 1
                self._recovery_pages[title] = WikiPage(
                    title, text, self._recovery_revision
                )
            return self.immutable_page(title)

    def page_titles(self, prefix: str) -> list[str]:
        with self._recovery_page_lock:
            return sorted(
                title
                for title in self._recovery_pages
                if title.startswith(prefix)
            )

    def list_entity_ids(self) -> list[str]:
        properties, items = self.vocabulary()
        return sorted(
            set(self.wbi.item.store)
            | set(properties.values())
            | set(items.values())
        )

    def creation_revisions(self, marker: str) -> list[EntityCreation]:
        history = self.wbi.item.creation_history
        if any(
            row.get("summary") is None or row.get("hidden") for row in history
        ):
            raise RecoveryEvidenceError("initial revision comment unavailable")
        return [
            EntityCreation(row["qid"], row["revision"])
            for row in history
            if marker in row["summary"].split()
        ]

    def verify_creation(self, qid: str, revision: int, marker: str) -> None:
        matches = [
            row
            for row in self.wbi.item.creation_history
            if row["qid"] == qid and row["revision"] == revision
        ]
        if (
            len(matches) != 1
            or matches[0].get("hidden")
            or marker not in (matches[0].get("summary") or "").split()
            or qid not in self.wbi.item.store
        ):
            raise RecoveryEvidenceError(
                "creation revision does not prove binding"
            )
