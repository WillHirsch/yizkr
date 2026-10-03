"""The LLM-path fixture of the live names test (tests/integration/
test_live_names.py), shared with its offline mirror (tests/unit/
test_live_names_fixture.py) so the two cannot drift apart.

One fictional person, Ida Livecheck, in two fictional documents. Each
document's entity manifest gives her the mention record extraction hands
over (ADR-0035 §2, as #416's prompts ask for it), so the alias cleanup
reads her aliases through that record (§8): an alias that gives no name
part the label lacks is derivable and goes, one that adds a part stays.
Every name here is fictional.
"""

from __future__ import annotations

import json

from yizkr.claims import ArtifactRef

LABEL = "Ida Livecheck"
# the forms a model might list for one person, messy on purpose, and what
# the record-based cleanup (ADR-0035 §8) makes of each
FIRST_DOCUMENT_ALIASES = [
    "Ida",  # gives the given name Ida, which the label holds -> dropped
    "I. Livecheck",  # gives the surname Livecheck, the label's -> dropped
    "ida livecheck",  # the label again, in another case -> dropped
    "Peppi",  # gives the nickname Peppi, a part the label lacks -> kept
    "PEPPI",  # a case-duplicate of a kept form -> dropped
    "Ida Probewald",  # adds the birth surname Probewald -> kept
    "Livecheck-Probewald",  # adds the compound surname, whole -> kept
    "van der Probewald",  # adds the particle surname, whole -> kept
    "  ",  # blank -> dropped
]
SECOND_DOCUMENT_ALIASES = ["Ida", "Peppi", "I. Livecheck", "Ida Testerhof"]
EXPECTED_ALIASES = {
    "Peppi",
    "Ida Probewald",
    "Livecheck-Probewald",
    "van der Probewald",
    "Ida Testerhof",  # the second document's one form adding a part
}
# the mention record of each document's person (ADR-0035 §2): which part
# of her names is which, each part as the document writes it
FIRST_RECORD = {
    "kind": "human",
    "status": "name",
    "given": [{"written": "Ida"}],
    "surnames": [
        {"written": "Livecheck", "type": "other"},
        {"written": "Probewald", "type": "birth"},
        {"written": "Livecheck-Probewald", "type": "other"},
        {"written": "van der Probewald", "type": "birth"},
    ],
    "nicknames": ["Peppi"],
}
SECOND_RECORD = {
    "kind": "human",
    "status": "name",
    "given": [{"written": "Ida"}],
    "surnames": [
        {"written": "Livecheck", "type": "other"},
        {"written": "Testerhof", "type": "married"},
    ],
    "nicknames": ["Peppi"],
}

FIRST_DOCUMENT = """\
Register of the Livecheck household, 1950.
1. Ida Livecheck, nee Probewald, called Peppi; b. 1881 in Moshausen.
"""
SECOND_DOCUMENT = """\
Letter from Stupfield, 1960.
Mrs Ida Testerhof (her married name) wrote; the family called her Peppi.
"""


def name(text: str) -> dict:
    return {"type": "monolingual", "value": {"text": text, "language": "en"}}


FIRST_CLAIMS = [
    ("birth_name", name("Ida Probewald"), "nee Probewald"),
    ("nickname", name("Peppi"), "called Peppi"),
    ("date_of_birth", {"type": "time", "year": 1881}, "b. 1881"),
]
SECOND_CLAIMS = [("married_name", name("Ida Testerhof"), "Mrs Ida Testerhof")]


class ScriptedEngine:
    """A fictional stand-in for the model: fixed responses, no model call.
    It has no conversation support, so the extractor sends chunks inline."""

    model_identifier = "integration-test-scripted"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def complete(self, prompt, image_paths=()):
        return self.responses.pop(0)


def entity(aliases: list[str], mention_line: int, record: dict | None):
    """The manifest's one entity, with its mention record when given."""
    found = {
        "id": "e1",
        "label": LABEL,
        "aliases": aliases,
        "kind": "human",
        "summary": "fictional live-test person",
        "mention_lines": [mention_line],
    }
    if record is not None:
        found["structure"] = record
    return found


def entity_manifest(
    aliases: list[str], mention_line: int, record: dict | None
) -> str:
    return json.dumps({"entities": [entity(aliases, mention_line, record)]})


def chunk_response(artifact: ArtifactRef, claims: list[tuple]) -> str:
    """A chunk's answer, each claim citing `artifact` as the prompt asks."""
    lines = [
        json.dumps(
            {
                "subject": {
                    "kind": "local",
                    "local_id": "e1",
                    "label": LABEL,
                    "instance_of": "human",
                },
                "property": prop,
                "value": value,
                "source": {
                    "artifact": artifact.model_dump(mode="json"),
                    "transcription_excerpt": {
                        "text": excerpt,
                        "language": "en",
                    },
                },
                "confidence": 0.9,
            }
        )
        for prop, value, excerpt in claims
    ]
    lines.append(json.dumps({"done": True, "claims_emitted": len(claims)}))
    return "\n".join(lines)
