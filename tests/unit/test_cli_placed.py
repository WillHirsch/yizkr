"""The command line gives a source's placed media (#551, ADR-0039 §2.1).

Validation and the writer take a `caption` only on an artifact its source
places, so each needs the source's placed media. `yizkr claims
--validate`, `claims.py --validate` and `yizkr ingest write` take them with
`--placed`: a container's record of derived media (`derived-media.json`,
or the directory that keeps it) or a source's SHA-1 and theirs. Without
it a caption is refused as before, and the command says how to give them.

The fixture is the fictional universe's (docs/EXAMPLES.md): *Our Family
from Germany* (`family-booklet-2011`) places the top of Sebastian's letter
to Klara (`letter-excerpt-1939`) on its page 6, under the caption "Wastl
to Klara, July 1939."
"""

from __future__ import annotations

import json
import sys

import pytest

from tests.unit.test_container_claims import (
    BOOKLET_REF,
    CAPTION,
    DECK_REF,
    JULY_28,
    LETTER_REF,
    STRAY_REF,
    CaptionWiki,
    artifacts,
    batch_of,
    caption,
    itself,
)
from yizkr import claims as claims_module
from yizkr import cli
from yizkr.claims import (
    PLACED_OPTION,
    read_placed,
    unplaced_claims,
    unplaced_hint,
)
from yizkr.core.exits import EXIT_OK
from yizkr.pipeline.presentation import (
    DERIVED_MEDIA,
    DERIVED_MEDIA_DIR,
    DerivedMediaRecord,
    DerivedMedium,
)

TITLE = "Our Family from Germany"


def record(directory, source=BOOKLET_REF, *placed):
    """The booklet's record of derived media, as the pipeline keeps it in
    its document directory: the images its pages place, by their SHA-1s
    (only the record: the media's bytes are not read)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / DERIVED_MEDIA
    path.write_text(
        DerivedMediaRecord(
            source_sha1=source.sha1,
            container="pdf",
            title=TITLE,
            media={
                ref.sha1: DerivedMedium(
                    names=[f"page 6, image {place}"],
                    slides=[6],
                    file=f"{DERIVED_MEDIA_DIR}/{ref.sha1}.png",
                )
                for place, ref in enumerate(placed or (LETTER_REF,), 1)
            },
        ).model_dump_json(indent=2)
    )
    return path


def batch_file(tmp_path, *claims):
    path = tmp_path / "batch.json"
    path.write_text(batch_of(*(claims or (caption(),))).model_dump_json())
    return path


class TestReadPlaced:
    def test_a_record_names_its_container_and_what_it_places(self, tmp_path):
        path = record(tmp_path / "booklet", BOOKLET_REF, LETTER_REF, STRAY_REF)
        expected = {BOOKLET_REF.sha1: {LETTER_REF.sha1, STRAY_REF.sha1}}
        assert read_placed([str(path)]) == expected
        # the document directory that keeps the record names it too
        assert read_placed([str(path.parent)]) == expected

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
        with pytest.raises(ValueError, match="--placed <derived-media"):
            read_placed([f"{BOOKLET_REF.sha1}={LETTER_REF.sha1}0"])

    def test_values_for_one_source_add_up(self, tmp_path):
        path = record(tmp_path / "booklet")
        assert read_placed(
            [
                str(path),
                f"{BOOKLET_REF.sha1}={STRAY_REF.sha1}",
                f"{DECK_REF.sha1}={LETTER_REF.sha1}",
            ]
        ) == {
            BOOKLET_REF.sha1: {LETTER_REF.sha1, STRAY_REF.sha1},
            DECK_REF.sha1: {LETTER_REF.sha1},
        }

    def test_none_given_places_nothing(self):
        assert read_placed([]) == {}

    def test_a_file_that_is_no_record_is_refused(self, tmp_path):
        other = tmp_path / "batch.json"
        other.write_text(batch_of(caption()).model_dump_json())
        with pytest.raises(ValueError, match="derived media") as refusal:
            read_placed([str(other)])
        assert "--placed <derived-media.json>" in str(refusal.value)
        # a path that is not there, or a mistyped pair, says what to give
        with pytest.raises(ValueError, match="booklet") as refusal:
            read_placed([str(tmp_path / "booklet")])
        assert PLACED_OPTION in str(refusal.value)

    def test_a_source_never_places_itself(self):
        with pytest.raises(ValueError, match="does not place itself"):
            read_placed([f"{BOOKLET_REF.sha1}={BOOKLET_REF.sha1}"])

    def test_a_record_never_places_its_own_container(self, tmp_path):
        path = record(
            tmp_path / "booklet", BOOKLET_REF, LETTER_REF, BOOKLET_REF
        )
        with pytest.raises(ValueError, match="does not place itself"):
            read_placed([str(path)])

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
        placed = record(tmp_path / "booklet")
        status = cli.main(
            [
                *("claims", "--validate", str(batch_file(tmp_path))),
                *("--placed", str(placed)),
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
        placed = record(tmp_path / "booklet")
        batch = batch_file(tmp_path, caption(STRAY_REF.model_dump()))
        status = cli.main(
            ["claims", "--validate", str(batch), "--placed", str(placed)]
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


class TestClaimsScript:
    def main(self, monkeypatch, *argv):
        monkeypatch.setattr(sys, "argv", ["claims.py", *argv])
        return claims_module.main()

    def test_with_the_placed_media_the_caption_passes(
        self, tmp_path, monkeypatch, capsys
    ):
        placed = record(tmp_path / "booklet")
        batch = batch_file(tmp_path)
        assert (
            self.main(
                monkeypatch, "--validate", str(batch), "--placed", str(placed)
            )
            == 0
        )
        captured = capsys.readouterr()
        assert "valid (1 claims" in captured.out
        assert captured.err == ""

    def test_without_them_the_caption_is_refused(
        self, tmp_path, monkeypatch, capsys
    ):
        batch = batch_file(tmp_path)
        assert self.main(monkeypatch, "--validate", str(batch)) == 1
        err = capsys.readouterr().err
        assert "[placed_subject]" in err
        assert unplaced_hint(1) in err

    def test_a_bad_placed_is_refused(self, tmp_path, monkeypatch, capsys):
        batch = batch_file(tmp_path)
        argv = ("--validate", str(batch), "--placed", str(batch))
        assert self.main(monkeypatch, *argv) == 1
        err = capsys.readouterr().err
        assert err.startswith("--placed: ")
        # refused before the batch is checked
        assert "manifest check" not in err


class TestWrite:
    def test_with_the_placed_media_the_caption_is_written(
        self, tmp_path, monkeypatch, capsys
    ):
        wiki = CaptionWiki()
        qids = artifacts(wiki, BOOKLET_REF, LETTER_REF)
        monkeypatch.setattr(cli, "_client", lambda args: wiki)
        placed = record(tmp_path / "booklet")
        status = cli.main(
            [
                *("ingest", "write", "--batch", str(batch_file(tmp_path))),
                *("--placed", str(placed.parent)),
            ]
        )
        out, err = capsys.readouterr()
        assert status == EXIT_OK
        assert '"written": 1' in out
        assert "--placed" not in err
        (statement,) = wiki.wbi.item.get(qids[LETTER_REF.sha1]).claims.get(
            "P91"
        )
        assert statement.mainsnak.datavalue["value"] == {
            "text": CAPTION,
            "language": "en",
        }
        assert not wiki.wbi.item.get(qids[BOOKLET_REF.sha1]).claims.get("P91")

    def test_without_them_the_caption_is_refused(
        self, tmp_path, monkeypatch, capsys
    ):
        wiki = CaptionWiki()
        qids = artifacts(wiki, BOOKLET_REF, LETTER_REF)
        monkeypatch.setattr(cli, "_client", lambda args: wiki)
        batch = batch_file(
            tmp_path, caption(), caption(text="Wastl to Klara.", page="7")
        )
        status = cli.main(["ingest", "write", "--batch", str(batch)])
        out, err = capsys.readouterr()
        # each claim is refused alone, as the writer refuses it
        assert status == EXIT_OK
        result = json.loads(out)
        assert result["written"] == 0
        # counted under the subject rule's reason
        assert result["refusal_reasons"] == ["subject_rule", "subject_rule"]
        assert "is no artifact given to this write as placed" in out
        assert unplaced_hint(2) in err
        for qid in qids.values():
            assert not wiki.wbi.item.get(qid).claims.get("P91")

    def test_another_source_s_placed_media_leave_it_refused(
        self, tmp_path, monkeypatch, capsys
    ):
        wiki = CaptionWiki()
        qids = artifacts(wiki, BOOKLET_REF, LETTER_REF)
        monkeypatch.setattr(cli, "_client", lambda args: wiki)
        status = cli.main(
            [
                *("ingest", "write", "--batch", str(batch_file(tmp_path))),
                *("--placed", f"{DECK_REF.sha1}={LETTER_REF.sha1}"),
            ]
        )
        out, err = capsys.readouterr()
        assert status == EXIT_OK
        assert '"written": 0' in out
        assert unplaced_hint(1) in err
        for qid in qids.values():
            assert not wiki.wbi.item.get(qid).claims.get("P91")

    def test_a_bad_placed_is_refused_before_the_instance(
        self, tmp_path, monkeypatch, capsys
    ):
        def contacted(args):
            raise AssertionError("the instance was contacted")

        monkeypatch.setattr(cli, "_client", contacted)
        batch = batch_file(tmp_path)
        status = cli.main(
            ["ingest", "write", "--batch", str(batch), "--placed", str(batch)]
        )
        assert status != EXIT_OK
        assert capsys.readouterr().err.startswith("--placed: ")

    def test_a_bad_placed_is_refused_before_the_batch_is_read(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(cli, "_client", lambda args: CaptionWiki())
        status = cli.main(
            [
                *("ingest", "write", "--batch", str(tmp_path / "none.json")),
                *("--placed", str(tmp_path / "nothing")),
            ]
        )
        assert status != EXIT_OK
        assert capsys.readouterr().err.startswith("--placed: ")
