"""Live check that the label lookups outside the writer find an item by
its label given in NFD, as the instance keeps it in NFC (#370;
ADR-0010):

* the place-hierarchy pass's run item, made under a model identifier
  in NFD, is labelled in NFC on the instance, and found again under the
  identifier in either form, so a pass makes no second run item. The
  resolve step looks up no label (#109).

Fictional synthetic data only (docs/EXAMPLES.md): the model identifier
names the Löbigs. The fingerprint carries random bytes drawn once a
session, so each session meets the run item as a first pass does.
"""

from __future__ import annotations

import hashlib
import secrets
import unicodedata

import pytest

from yizkr.pipeline.places import ensure_places_run_item

pytestmark = pytest.mark.live

FINGERPRINT = secrets.token_hex(5)
MODEL = unicodedata.normalize("NFC", "Löbig-model")
LABEL = f"place-hierarchy run {MODEL} {FINGERPRINT}"
MEDIA_SHA1 = hashlib.sha1(b"fictional scan: the 1912 directory").hexdigest()


def nfd(text: str) -> str:
    return unicodedata.normalize("NFD", text)


def test_an_nfd_run_item_is_found_in_nfc(client):
    assert nfd(MODEL) != MODEL
    qid = ensure_places_run_item(client, FINGERPRINT, nfd(MODEL))

    data = client.api_get(
        {"action": "wbgetentities", "ids": qid, "props": "labels"}
    )
    assert data["entities"][qid]["labels"]["en"]["value"] == LABEL
    assert ensure_places_run_item(client, FINGERPRINT, MODEL) == qid
    assert ensure_places_run_item(client, FINGERPRINT, nfd(MODEL)) == qid
