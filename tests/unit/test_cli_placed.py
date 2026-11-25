"""The command line gives a source's placed media (#551, ADR-0039 §2.1).

Validation and the writer take a `caption` only on an artifact its source
places, so each needs the source's placed media. `yizkr claims
--validate` takes them with `--placed`: a container's inventory
(`container-inventory.json`, or the document directory that keeps it) or
a source's SHA-1 and theirs. Without it a caption is refused as before,
and the command says how to give them. The writer's own refusal is
tested with the writer (test_container_claims.TestTheWriter).

The fixture is the fictional universe's (docs/EXAMPLES.md): *Our Family
from Germany* (`family-booklet-2011`) places the top of Sebastian's letter
to Klara (`letter-excerpt-1939`) on its page 6, under the caption "Wastl
to Klara, July 1939."
"""

from __future__ import annotations

import json

import pytest
import yaml

from tests.unit.test_container_claims import (
    BOOKLET_REF,
    DECK_REF,
    JULY_28,
    LETTER_REF,
    STRAY_REF,
    batch_of,
    caption,
    itself,
)
from yizkr import cli
from yizkr.claims import PLACED_OPTION, unplaced_claims, unplaced_hint
from yizkr.cli import read_placed
from yizkr.core.exits import EXIT_OK
from yizkr.model.schema import DEFAULT_MANIFEST, read_manifest_data

# the booklet places the top of the letter: the value `--placed` takes
PLACED = f"{BOOKLET_REF.sha1}={LETTER_REF.sha1}"


def old_record(directory):
    """An older run's record of the booklet's derived media in its
    document directory, beside the bytes of the image it names (those
    LETTER_REF names): not an input."""
    image = directory / "derived-media" / f"{LETTER_REF.sha1}.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\x89PNG the top of the 1939 letter")
    path = directory / "derived-media.json"
    path.write_text(
        json.dumps(
            {
                "source_sha1": BOOKLET_REF.sha1,
                "container": "pdf",
                "title": "Our Family from Germany",
                "media": {
                    LETTER_REF.sha1: {
                        "names": ["page 6, image 1"],
                        "slides": [6],
                        "file": f"derived-media/{LETTER_REF.sha1}.png",
                    }
                },
            }
        )
    )
    return path


def batch_file(tmp_path, *claims):
    path = tmp_path / "batch.json"
    path.write_text(batch_of(*(claims or (caption(),))).model_dump_json())
    return path


def manifest_file(tmp_path, name, change=lambda caption: None):
    """A copy of the package's model manifest, its `caption` property
    changed by `change`."""
    raw = yaml.safe_load(read_manifest_data(DEFAULT_MANIFEST))
    change(raw["properties"]["caption"])
    path = tmp_path / name
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return path


class TestReadPlaced:
    def test_a_source_and_its_artifacts_by_sha1(self):
        value = f"{BOOKLET_REF.sha1}={LETTER_REF.sha1},{STRAY_REF.sha1}"
        assert read_placed([value]) == {
            BOOKLET_REF.sha1: {LETTER_REF.sha1, STRAY_REF.sha1}
        }
        # the same digests in capitals name the same bytes
        assert read_placed([value.upper()]) == read_placed([value])

    def test_the_whole_value_is_the_pair(self):
        """A value with more than a pair in it is read as a path, never
        cut to the pair it begins with."""
        with pytest.raises(ValueError) as refusal:
            read_placed([f"{BOOKLET_REF.sha1}={LETTER_REF.sha1}0"])
        assert PLACED_OPTION in str(refusal.value)

    def test_values_for_one_source_add_up(self):
        assert read_placed(
            [
                PLACED,
                f"{BOOKLET_REF.sha1}={STRAY_REF.sha1}",
                f"{DECK_REF.sha1}={LETTER_REF.sha1}",
            ]
        ) == {
            BOOKLET_REF.sha1: {LETTER_REF.sha1, STRAY_REF.sha1},
            DECK_REF.sha1: {LETTER_REF.sha1},
        }

    def test_none_given_places_nothing(self):
        assert read_placed([]) == {}

    def test_a_file_that_is_no_inventory_is_refused(self, tmp_path):
        other = tmp_path / "batch.json"
        other.write_text(batch_of(caption()).model_dump_json())
        with pytest.raises(ValueError) as refusal:
            read_placed([str(other)])
        assert PLACED_OPTION in str(refusal.value)
        # a path that is not there, or a mistyped pair, says what to give
        with pytest.raises(ValueError, match="booklet") as refusal:
            read_placed([str(tmp_path / "booklet")])
        assert PLACED_OPTION in str(refusal.value)

    def test_a_source_never_places_itself(self):
        with pytest.raises(ValueError, match="does not place itself"):
            read_placed([f"{BOOKLET_REF.sha1}={BOOKLET_REF.sha1}"])

    def test_an_older_record_is_refused(self, tmp_path):
        """A derived-media record an older run kept, given by its path,
        is refused with the forms `--placed` takes."""
        with pytest.raises(ValueError) as refusal:
            read_placed([str(old_record(tmp_path / "booklet"))])
        assert PLACED_OPTION in str(refusal.value)

    def test_a_directory_holding_only_an_older_record_is_refused(
        self, tmp_path
    ):
        """A document directory with no container inventory is refused
        with the forms `--placed` takes, whatever older record it keeps."""
        directory = tmp_path / "booklet"
        old_record(directory)
        with pytest.raises(ValueError) as refusal:
            read_placed([str(directory)])
        assert PLACED_OPTION in str(refusal.value)

    def test_the_captions_left_without_their_placed_media(self):
        batch = batch_of(caption(), caption(text="Wastl to Klara."))
        assert unplaced_claims(batch, None) == 2
        assert unplaced_claims(batch, {DECK_REF.sha1: set()}) == 2
        # a source named as placing nothing has its placed media given
        assert unplaced_claims(batch, {BOOKLET_REF.sha1: set()}) == 0
        assert "--placed" in unplaced_hint(2)
        assert PLACED_OPTION in unplaced_hint(2)

    def test_a_document_subject_claim_about_a_placed_image_wants_them(
        self,
    ):
        """A document-subject claim about the image the booklet places
        wants the booklet's placed media as a caption does (ADR-0039
        §10.5); one about the claim's own source wants none (#646)."""
        assert (
            unplaced_claims(batch_of(itself("inception", JULY_28)), None) == 0
        )
        dated = itself(
            "inception",
            JULY_28,
            source=BOOKLET_REF,
            subject=LETTER_REF.model_dump(),
        )
        assert unplaced_claims(batch_of(dated), None) == 1
        assert unplaced_claims(batch_of(dated), {BOOKLET_REF.sha1: set()}) == 0

    def test_only_a_claim_about_another_artifact_wants_them(self):
        """A caption of its own source, or a claim of a property that
        takes no artifact, is refused whatever is given, so it is not
        counted."""
        shown = itself(
            "residence",
            JULY_28,
            source=BOOKLET_REF,
            subject=LETTER_REF.model_dump(),
        )
        assert unplaced_claims(batch_of(shown), None) == 0
        of_itself = caption(BOOKLET_REF.model_dump())
        assert unplaced_claims(batch_of(of_itself), None) == 0


class TestValidate:
    def test_with_the_placed_media_the_caption_passes(self, tmp_path, capsys):
        status = cli.main(
            [
                *("claims", "--validate", str(batch_file(tmp_path))),
                *("--placed", PLACED),
            ]
        )
        out, err = capsys.readouterr()
        assert status == EXIT_OK
        assert "valid (1 claims" in out
        assert err == ""

    def test_without_them_the_caption_is_refused(self, tmp_path, capsys):
        status = cli.main(["claims", "--validate", str(batch_file(tmp_path))])
        err = capsys.readouterr().err
        assert status != EXIT_OK
        assert "[placed_subject]" in err
        assert unplaced_hint(1) in err

    def test_another_source_s_placed_media_leave_it_refused(
        self, tmp_path, capsys
    ):
        status = cli.main(
            [
                *("claims", "--validate", str(batch_file(tmp_path))),
                *("--placed", f"{DECK_REF.sha1}={LETTER_REF.sha1}"),
            ]
        )
        err = capsys.readouterr().err
        assert status != EXIT_OK
        assert "[placed_subject]" in err
        assert unplaced_hint(1) in err

    def test_a_caption_of_what_the_source_does_not_place_is_refused(
        self, tmp_path, capsys
    ):
        batch = batch_file(tmp_path, caption(STRAY_REF.model_dump()))
        status = cli.main(
            ["claims", "--validate", str(batch), "--placed", PLACED]
        )
        err = capsys.readouterr().err
        assert status != EXIT_OK
        assert "[placed_subject]" in err
        # its source's placed media were given: no hint
        assert "--placed" not in err

    def test_a_bad_placed_is_refused_before_the_batch_is_read(
        self, tmp_path, capsys
    ):
        status = cli.main(
            [
                *("claims", "--validate", str(tmp_path / "missing.json")),
                *("--placed", str(tmp_path / "nothing")),
            ]
        )
        assert status != EXIT_OK
        assert capsys.readouterr().err.startswith("--placed: ")

    def test_it_checks_against_the_manifest_it_is_given(
        self, tmp_path, capsys
    ):
        """`--manifest` names the manifest the batch is checked against in
        place of the package's. A copy of it passes the booklet's caption
        given its placed media; one that makes `caption` a plain string
        refuses it; and under one that marks `caption` no placed subject,
        it is not counted among the claims that want placed media."""
        validate = ("claims", "--validate", str(batch_file(tmp_path)))
        placed = ("--placed", PLACED)

        same = manifest_file(tmp_path, "same.yaml")
        status = cli.main([*validate, *placed, "--manifest", str(same)])
        assert status == EXIT_OK
        assert capsys.readouterr().err == ""

        string = manifest_file(
            tmp_path, "string.yaml", lambda p: p.update(datatype="string")
        )
        status = cli.main([*validate, *placed, "--manifest", str(string)])
        assert status != EXIT_OK
        assert "caption" in capsys.readouterr().err

        unmarked = manifest_file(
            tmp_path, "unmarked.yaml", lambda p: p.pop("placed_subject")
        )
        status = cli.main([*validate, "--manifest", str(unmarked)])
        assert status != EXIT_OK
        assert unplaced_hint(1) not in capsys.readouterr().err
