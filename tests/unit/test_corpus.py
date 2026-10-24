"""The corpus location and the never-ingest list (#29).

Offline; every corpus is a temp folder of fictional documents from the
example universe (docs/EXAMPLES.md). The list fails closed: a configured
list that cannot be read, or a line that would silently match nothing,
is an error, and `ingest run` refuses to start without a list.
"""

from __future__ import annotations

import hashlib
import json
import os
import unicodedata
import zipfile

import pytest

from tests.fixtures import example_universe as u
from yizkr.cli import build_parser
from yizkr.core.corpus import (
    CORPUS_DIR_VAR,
    EXCLUDE_FILE_VAR,
    ExclusionConfigError,
    ExclusionList,
    corpus_dir,
    file_sha1,
    follow_decks,
    is_excluded,
    load_exclusions,
)

SOURCES = {source.key: source for source in u.SOURCES}
POSTCARD = SOURCES["postcard-1938"].text
ADDRESS_BOOK = SOURCES["directory-1912"].text


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """No corpus settings from the host: an empty env file of our own,
    and the two variables cleared from the environment."""
    env_file = tmp_path / "test.env"
    env_file.write_text("")
    monkeypatch.setenv("YIZKR_ENV_FILE", str(env_file))
    monkeypatch.delenv(CORPUS_DIR_VAR, raising=False)
    monkeypatch.delenv(EXCLUDE_FILE_VAR, raising=False)
    return env_file


@pytest.fixture
def corpus(tmp_path):
    folder = tmp_path / "corpus"
    folder.mkdir()
    # bytes, not text: the SHA-1s below hash the UTF-8 encoding
    (folder / "postcard-1938.txt").write_bytes(POSTCARD.encode())
    (folder / "Flörburg address book 1912.txt").write_bytes(
        ADDRESS_BOOK.encode()
    )
    return folder


class TestParse:
    def test_sha1s_globs_comments_and_blank_lines(self):
        listed = ExclusionList.parse(
            f"# never ingest\n\n{sha1(POSTCARD).upper()}\n"
            "   *Address*Book*   \n"
        )
        assert listed.sha1s == {sha1(POSTCARD)}
        assert listed.patterns == ("*address*book*",)

    def test_sha1sum_output_pastes_in_as_it_is(self):
        # text mode ("  name") and binary mode (" *name"); the name after
        # the hash is ignored, not read as a second entry
        listed = ExclusionList.parse(
            f"{sha1(POSTCARD)}  postcard-1938.txt\n"
            f"{sha1(ADDRESS_BOOK)} *address book.txt\n"
        )
        assert listed.sha1s == {sha1(POSTCARD), sha1(ADDRESS_BOOK)}
        assert listed.patterns == ()

    @pytest.mark.parametrize(
        "line",
        [
            hashlib.sha256(POSTCARD.encode()).hexdigest(),  # a pasted SHA-256
            sha1(POSTCARD)[:-1],  # a truncated SHA-1
        ],
    )
    def test_a_hash_that_is_not_a_sha1_is_an_error(self, line):
        # it would otherwise sit in the list as a glob matching nothing
        with pytest.raises(ExclusionConfigError, match="line 2"):
            ExclusionList.parse(f"# header\n{line}\n")

    def test_a_glob_with_an_inline_comment_is_an_error(self):
        with pytest.raises(ExclusionConfigError, match="inline comment"):
            ExclusionList.parse("*address*book*  # the directory scans\n")

    def test_a_literal_hash_mark_in_a_name_is_bracketed(self):
        listed = ExclusionList.parse("Letter [#]2.txt\n")
        assert is_excluded(listed, name="Letter #2.txt")

    def test_the_empty_list_excludes_nothing(self, corpus):
        for path in corpus.iterdir():
            assert not is_excluded(ExclusionList(), path)
            assert not is_excluded(ExclusionList.parse(""), path)


class TestIsExcluded:
    def test_a_glob_matches_the_name_ignoring_case(self, corpus):
        listed = ExclusionList.parse("*ADDRESS*book*\n")
        assert is_excluded(listed, corpus / "Flörburg address book 1912.txt")
        assert not is_excluded(listed, corpus / "postcard-1938.txt")

    def test_a_glob_never_matches_the_folder(self, tmp_path):
        folder = tmp_path / "address book scans"
        folder.mkdir()
        (folder / "postcard-1938.txt").write_bytes(POSTCARD.encode())
        listed = ExclusionList.parse("*address*book*\n")
        assert not is_excluded(listed, folder / "postcard-1938.txt")

    def test_a_glob_matches_either_unicode_form(self):
        # a name typed on one system (composed) and stored by another
        # (decomposed) is one name
        composed = unicodedata.normalize("NFC", "Pünktchen")
        listed = ExclusionList.parse(f"{composed}*\n")
        decomposed = unicodedata.normalize("NFD", "Pünktchen postcard.txt")
        assert decomposed != unicodedata.normalize("NFC", decomposed)
        assert is_excluded(listed, name=decomposed)

    def test_a_sha1_excludes_the_bytes_under_any_name(self, corpus):
        copy = corpus / "renamed copy.txt"
        copy.write_bytes((corpus / "postcard-1938.txt").read_bytes())
        listed = ExclusionList.parse(f"{sha1(POSTCARD)}\n")
        assert is_excluded(listed, corpus / "postcard-1938.txt")
        assert is_excluded(listed, copy)
        assert not is_excluded(
            listed, corpus / "Flörburg address book 1912.txt"
        )

    def test_without_a_file_the_name_or_sha1_is_checked(self):
        listed = ExclusionList.parse(f"{sha1(POSTCARD)}\n*address*book*\n")
        assert is_excluded(listed, sha1=sha1(POSTCARD).upper())
        assert is_excluded(listed, name="Flörburg address book.jpg")
        assert not is_excluded(listed, name="postcard.txt", sha1="0" * 40)

    def test_every_identity_given_is_checked(self, corpus):
        # a file on disk, named for its original: either identity excludes
        listed = ExclusionList.parse("*address*book*\n")
        postcard = corpus / "postcard-1938.txt"
        assert is_excluded(listed, postcard, name="address book.jpg")

    def test_an_identity_is_required(self):
        with pytest.raises(ValueError):
            is_excluded(ExclusionList())

    def test_file_sha1_is_the_content_hash(self, corpus):
        assert file_sha1(corpus / "postcard-1938.txt") == sha1(POSTCARD)


class TestLoad:
    def test_required_and_unset_refuses(self, env):
        with pytest.raises(ExclusionConfigError, match=EXCLUDE_FILE_VAR):
            load_exclusions(required=True)

    def test_optional_and_unset_is_none(self, env):
        assert load_exclusions(required=False) is None

    def test_an_empty_file_is_an_explicit_list(self, env, tmp_path):
        listed_file = tmp_path / "exclude.txt"
        listed_file.write_text("")
        env.write_text(f"{EXCLUDE_FILE_VAR}={listed_file}\n")
        listed = load_exclusions(required=True)
        assert listed is not None
        assert listed.sha1s == frozenset() and listed.patterns == ()
        assert listed.source == listed_file

    @pytest.mark.parametrize("required", [True, False])
    def test_a_configured_list_that_is_missing_fails_closed(
        self, env, tmp_path, required
    ):
        env.write_text(f"{EXCLUDE_FILE_VAR}={tmp_path / 'gone.txt'}\n")
        with pytest.raises(ExclusionConfigError, match="cannot read"):
            load_exclusions(required=required)

    def test_a_folder_is_not_a_list(self, env, tmp_path, monkeypatch):
        monkeypatch.setenv(EXCLUDE_FILE_VAR, str(tmp_path))
        with pytest.raises(ExclusionConfigError, match="cannot read"):
            load_exclusions(required=False)

    def test_the_environment_wins_over_the_env_file(
        self, env, tmp_path, monkeypatch
    ):
        from_env_file = tmp_path / "a.txt"
        from_env_file.write_text("*address*book*\n")
        from_environment = tmp_path / "b.txt"
        from_environment.write_text(f"{sha1(POSTCARD)}\n")
        env.write_text(f"{EXCLUDE_FILE_VAR}={from_env_file}\n")
        monkeypatch.setenv(EXCLUDE_FILE_VAR, str(from_environment))
        assert load_exclusions(required=True).source == from_environment

    def test_corpus_dir(self, env, tmp_path, monkeypatch):
        assert corpus_dir() is None
        env.write_text(f"{CORPUS_DIR_VAR}={tmp_path / 'corpus'}\n")
        assert corpus_dir() == tmp_path / "corpus"
        monkeypatch.setenv(CORPUS_DIR_VAR, "~/corpus")
        assert corpus_dir().is_absolute()  # ~ is expanded


def run_cli(*argv):
    args = build_parser().parse_args(list(argv))
    return args.func(args)


class TestCli:
    def test_ingest_run_refuses_without_a_list(self, env, corpus):
        with pytest.raises(SystemExit, match=EXCLUDE_FILE_VAR):
            run_cli("ingest", "run", str(corpus))

    def test_ingest_run_checks_the_list_before_the_folder(self, env, tmp_path):
        with pytest.raises(SystemExit, match=EXCLUDE_FILE_VAR):
            run_cli("ingest", "run", str(tmp_path / "no such folder"))

    def test_ingest_run_defaults_to_the_corpus(
        self, env, corpus, tmp_path, monkeypatch
    ):
        from yizkr.pipeline.orchestrate import Orchestrator

        listed_file = tmp_path / "exclude.txt"
        listed_file.write_text("*address*book*\n")
        env.write_text(
            f"{CORPUS_DIR_VAR}={corpus}\n{EXCLUDE_FILE_VAR}={listed_file}\n"
        )
        seen = {}

        def fake_run(self):
            seen["folder"] = self.folder
            seen["exclusions"] = self.exclusions
            seen["documents"] = [d.source.name for d in self.documents()]
            return 0

        monkeypatch.setattr(Orchestrator, "run", fake_run)
        assert run_cli("ingest", "run", "--state", str(tmp_path / "s")) == 0
        assert seen["folder"] == corpus
        assert seen["exclusions"].patterns == ("*address*book*",)
        assert seen["documents"] == ["postcard-1938.txt"]

    def test_ingest_run_needs_a_folder_or_the_corpus_setting(
        self, env, tmp_path
    ):
        listed_file = tmp_path / "exclude.txt"
        listed_file.write_text("")
        env.write_text(f"{EXCLUDE_FILE_VAR}={listed_file}\n")
        with pytest.raises(SystemExit, match=CORPUS_DIR_VAR):
            run_cli("ingest", "run")

    def test_a_single_step_runs_without_a_list(self, env, corpus, tmp_path):
        # the single steps are debugging tools: no list, no refusal
        out = tmp_path / "transcription.json"
        # the postcard's scan, and the text read from it
        scan = tmp_path / "postcard-1938.png"
        scan.write_bytes(b"fictional scan: the 1938 postcard")
        postcard = corpus / "postcard-1938.txt"
        args = ("--file", str(scan), "--text-file", str(postcard))
        assert run_cli("ingest", "transcribe", *args, "--out", str(out)) == 0
        transcription = json.loads(out.read_text())
        assert transcription["text"] == POSTCARD
        # it names the scan by the SHA-1 of its bytes, never the text file
        # it was read from (#127)
        assert transcription["artifact"] == {
            "kind": "artifact",
            "sha1": hashlib.sha1(scan.read_bytes()).hexdigest(),
        }

    def test_a_single_step_refuses_a_listed_file(
        self, env, corpus, tmp_path, monkeypatch
    ):
        import yizkr.pipeline.registrar as registrar

        def never(*args, **kwargs):
            raise AssertionError("an excluded file reached the registrar")

        monkeypatch.setattr(registrar, "register_artifact", never)
        listed_file = tmp_path / "exclude.txt"
        listed_file.write_text(f"{sha1(POSTCARD)}\n")
        monkeypatch.setenv(EXCLUDE_FILE_VAR, str(listed_file))
        postcard = corpus / "postcard-1938.txt"
        out = tmp_path / "transcription.json"
        with pytest.raises(SystemExit, match="never-ingest"):
            run_cli("ingest", "transcribe", "--file", str(postcard),
                    "--text-file", str(postcard),
                    "--out", str(out))  # fmt: skip
        assert not out.exists()
        with pytest.raises(SystemExit, match="never-ingest"):
            run_cli("ingest", "register", "--file", str(postcard),
                    "--type", "letter", "--title", "Postcard")  # fmt: skip


# -- following a glob into the corpus's decks (#640) --------------------------

# the photograph the 2018 library talk embeds, as the deck's archive names
# it, and the same bytes kept as a file of their own under another name
TALK = "library-talk-2018.pptx"
TALK_IMAGE = b"\x89PNG fictional: the family album photograph in the talk"
# a fixed member time, so no archive reads the real clock
DECK_TIME = (2018, 5, 4, 12, 0, 0)


def talk(folder, name: str = TALK, image: bytes = TALK_IMAGE):
    """A deck in `folder` whose archive holds `image` as image2.png."""
    folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(folder / name, "w") as archive:
        for member, data in (
            ("ppt/slides/slide1.xml", b"<p:sld/>"),
            ("ppt/media/image2.png", image),
        ):
            archive.writestr(zipfile.ZipInfo(member, DECK_TIME), data)
    return folder / name


class TestFollowDecks:
    LISTED = ExclusionList.parse("image2.png\n")

    def test_the_decks_are_found_as_a_run_finds_them(self, tmp_path):
        """Every one of the folder's own decks, any case of suffix; not a
        hidden one, nor one in a subfolder, nor a folder named like a
        deck, nor another kind of archive."""
        folder = tmp_path / "corpus"
        talk(folder, "TALK-COPY.PPTX", b"upper suffix")
        talk(folder, "talk-final.pptx", b"another deck's image")
        talk(folder, ".~lock.pptx", b"hidden")
        talk(folder / "older", TALK, b"in a subfolder")
        (folder / "old.pptx").mkdir()
        talk(folder, "library-talk-2018.zip", b"not a deck")
        followed = follow_decks(self.LISTED, folder)
        assert followed.embedded == {
            hashlib.sha1(b"upper suffix").hexdigest(),
            hashlib.sha1(b"another deck's image").hexdigest(),
        }

    @pytest.mark.parametrize(
        "text", ["image2.png\n", f"{sha1(POSTCARD)}\n"], ids=["glob", "sha1"]
    )
    def test_it_replaces_what_was_followed_before(self, tmp_path, text):
        """What another folder's decks named is dropped, whether or not
        the list holds a glob to follow into this one's."""
        listed = ExclusionList.parse(text)
        before = listed.with_embedded([sha1("from another corpus")])
        assert follow_decks(before, tmp_path) == listed

    def test_a_list_with_no_glob_reads_no_folder(self, tmp_path):
        listed = ExclusionList.parse(f"{sha1(POSTCARD)}\n")
        followed = follow_decks(listed, tmp_path / "no such folder")
        assert followed == listed

    def test_a_folder_that_cannot_be_listed_fails_closed(self, tmp_path):
        with pytest.raises(ExclusionConfigError, match="no such folder"):
            follow_decks(self.LISTED, tmp_path / "no such folder")

    def test_a_folder_it_may_not_read_fails_closed(self, tmp_path):
        folder = tmp_path / "corpus"
        talk(folder)
        folder.chmod(0)
        try:
            if os.access(folder, os.R_OK):
                pytest.skip("this user reads any folder")
            with pytest.raises(ExclusionConfigError, match="corpus"):
                follow_decks(self.LISTED, folder)
        finally:
            folder.chmod(0o700)


class TestSingleStepsFollowDecks:
    """`ingest transcribe` and `ingest register` refuse a file whose bytes
    a glob names inside one of the corpus's decks, as a run does, though
    the file is called otherwise and lies outside the corpus."""

    @pytest.fixture
    def stand_alone(self, env, tmp_path, monkeypatch):
        """The talk's photograph as a file of its own, the list naming
        image2.png, and a registrar that must never be reached."""
        import yizkr.pipeline.registrar as registrar

        def never(*args, **kwargs):
            raise AssertionError("an excluded file reached the registrar")

        monkeypatch.setattr(registrar, "register_artifact", never)
        listed_file = tmp_path / "exclude.txt"
        listed_file.write_text("image2.png\n")
        monkeypatch.setenv(EXCLUDE_FILE_VAR, str(listed_file))
        scan = tmp_path / "album-photograph.png"
        scan.write_bytes(TALK_IMAGE)
        return scan

    @staticmethod
    def transcribe(tmp_path, scan):
        text = tmp_path / "reading.txt"
        text.write_text("fictional reading")
        out = tmp_path / "transcription.json"
        run_cli("ingest", "transcribe", "--file", str(scan),
                "--text-file", str(text), "--out", str(out))  # fmt: skip
        return out

    @staticmethod
    def register(scan):
        run_cli("ingest", "register", "--file", str(scan),
                "--type", "photograph", "--title", "Album")  # fmt: skip

    def test_a_file_a_glob_names_in_a_deck_is_refused(
        self, stand_alone, tmp_path, monkeypatch
    ):
        talk(tmp_path / "corpus")
        monkeypatch.setenv(CORPUS_DIR_VAR, str(tmp_path / "corpus"))
        with pytest.raises(SystemExit, match="never-ingest") as refused:
            self.transcribe(tmp_path, stand_alone)
        assert "album-photograph.png" in str(refused.value.code)
        assert not (tmp_path / "transcription.json").exists()
        with pytest.raises(SystemExit, match="never-ingest"):
            self.register(stand_alone)

    def test_the_corpus_folder_an_env_file_names_is_followed(
        self, stand_alone, tmp_path
    ):
        talk(tmp_path / "corpus")
        settings = tmp_path / "settings.env"
        settings.write_text(f"{CORPUS_DIR_VAR}={tmp_path / 'corpus'}\n")
        with pytest.raises(SystemExit, match="never-ingest"):
            run_cli("--env-file", str(settings), "ingest", "register",
                    "--file", str(stand_alone),
                    "--type", "photograph", "--title", "Album")  # fmt: skip

    def test_other_bytes_in_the_deck_leave_the_file_in(
        self, stand_alone, tmp_path, monkeypatch, capsys
    ):
        talk(tmp_path / "corpus", image=b"another photograph")
        monkeypatch.setenv(CORPUS_DIR_VAR, str(tmp_path / "corpus"))
        out = self.transcribe(tmp_path, stand_alone)
        assert json.loads(out.read_text())["text"] == "fictional reading"
        assert CORPUS_DIR_VAR not in capsys.readouterr().err

    def test_an_unreadable_deck_refuses_the_step(
        self, stand_alone, tmp_path, monkeypatch
    ):
        (tmp_path / "corpus").mkdir()
        (tmp_path / "corpus" / TALK).write_bytes(b"not an archive")
        monkeypatch.setenv(CORPUS_DIR_VAR, str(tmp_path / "corpus"))
        with pytest.raises(ExclusionConfigError, match=TALK):
            self.transcribe(tmp_path, stand_alone)
        assert not (tmp_path / "transcription.json").exists()
        with pytest.raises(ExclusionConfigError, match=TALK):
            self.register(stand_alone)

    def test_a_corpus_folder_that_cannot_be_listed_refuses_the_step(
        self, stand_alone, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(CORPUS_DIR_VAR, str(tmp_path / "moved away"))
        with pytest.raises(ExclusionConfigError, match="moved away"):
            self.transcribe(tmp_path, stand_alone)
        assert not (tmp_path / "transcription.json").exists()

    def test_without_a_corpus_folder_it_says_it_cannot_follow(
        self, stand_alone, tmp_path, capsys
    ):
        """No folder to find the decks in: the step goes on, and says
        plainly that a glob is not followed into decks."""
        out = self.transcribe(tmp_path, stand_alone)
        assert out.exists()
        err = capsys.readouterr().err
        assert CORPUS_DIR_VAR in err
        assert "not followed into the corpus's decks" in err

    @pytest.mark.parametrize(
        "folder_set", [True, False], ids=["folder", "none"]
    )
    def test_a_list_with_no_glob_neither_reads_the_corpus_nor_warns(
        self, stand_alone, tmp_path, monkeypatch, capsys, folder_set
    ):
        (tmp_path / "exclude.txt").write_text(f"{sha1(POSTCARD)}\n")
        (tmp_path / "corpus").mkdir()
        (tmp_path / "corpus" / TALK).write_bytes(b"not an archive")
        if folder_set:
            monkeypatch.setenv(CORPUS_DIR_VAR, str(tmp_path / "corpus"))
        assert self.transcribe(tmp_path, stand_alone).exists()
        assert capsys.readouterr().err == ""


# -- the single vision step and a deck (#684) ---------------------------------


class Reached(Exception):
    """The vision engine was asked: the step got past every refusal."""


class Engine:
    """A vision client that records each file it is given, by name and
    bytes, and then stops the step."""

    model_identifier = "fictional-vision-model"

    def __init__(self):
        self.sent: list[tuple[str, bytes]] = []

    def complete(self, prompt, files):
        self.sent += [(path.name, path.read_bytes()) for path in files]
        raise Reached


# the talk under its own name, in another case, and renamed: with no
# suffix, as another archive, as a macro deck and as an image
DECK_NAMES = [
    TALK,
    "LIBRARY-TALK-2018.PPTX",
    "library-talk-2018",
    "library-talk-2018.zip",
    "library-talk-2018.pptm",
    "album-photograph.png",
]


def forbid_reading(monkeypatch):
    """Fail the test if anything opens an archive's members, hashes a
    file or counts its pages."""
    import yizkr.core.corpus as corpus_module
    import yizkr.pipeline.transcribe as transcribe
    from yizkr.claims import ArtifactRef

    def never(what):
        def fail(*args, **kwargs):
            raise AssertionError(f"{what} before the refusal")

        return fail

    monkeypatch.setattr(zipfile, "ZipFile", never("an archive was opened"))
    monkeypatch.setattr(
        corpus_module, "file_sha1", never("the file was hashed")
    )
    monkeypatch.setattr(
        ArtifactRef, "from_path", never("the artifact was hashed")
    )
    monkeypatch.setattr(
        transcribe, "media_page_count", never("its pages were counted")
    )


class TestSingleVisionStepRefusesADeck:
    """`ingest transcribe --engine llm` would give the vision engine its
    file whole, embedded images and all, so it refuses a deck or any
    other ZIP archive, whatever it is called, and names the run that
    reads a deck slide by slide: nothing of it, and so nothing of a
    member a never-ingest glob names in it, is read or sent."""

    @pytest.fixture
    def engine(self, env, monkeypatch):
        import yizkr.pipeline.llm as llm

        engine = Engine()
        monkeypatch.setattr(llm, "make_client", lambda *a, **k: engine)
        return engine

    @pytest.fixture
    def listed(self, tmp_path, monkeypatch):
        """The list names the talk's image2.png, and the corpus folder
        holds the talk."""
        listed_file = tmp_path / "exclude.txt"
        listed_file.write_text("image2.png\n")
        monkeypatch.setenv(EXCLUDE_FILE_VAR, str(listed_file))
        monkeypatch.setenv(CORPUS_DIR_VAR, str(tmp_path / "corpus"))
        return talk(tmp_path / "corpus")

    @staticmethod
    def deck_as(tmp_path, name):
        """The talk's bytes in a file of its own called `name`."""
        deck = talk(tmp_path / "elsewhere")
        renamed = tmp_path / "elsewhere" / name
        if renamed != deck:
            deck.rename(renamed)
        return renamed

    @staticmethod
    def transcribe(tmp_path, media):
        out = tmp_path / "transcription.json"
        run_cli("ingest", "transcribe", "--engine", "llm",
                "--file", str(media), "--out", str(out))  # fmt: skip
        return out

    @staticmethod
    def assert_refused(refused, name):
        message = str(refused.value.code)
        assert name in message and "scratch run" in message
        assert "\n" not in message

    @pytest.mark.parametrize("name", DECK_NAMES)
    def test_a_deck_whose_member_a_glob_names_is_refused(
        self, engine, listed, tmp_path, name
    ):
        deck = self.deck_as(tmp_path, name)
        with pytest.raises(SystemExit) as refused:
            self.transcribe(tmp_path, deck)
        self.assert_refused(refused, name)
        assert engine.sent == []
        assert not (tmp_path / "transcription.json").exists()

    @pytest.mark.parametrize("name", DECK_NAMES)
    def test_nothing_is_read_and_no_client_made_before_the_refusal(
        self, env, listed, tmp_path, monkeypatch, name
    ):
        """The refusal comes before the list is followed into the
        corpus's decks, before the file is hashed, and before any client
        is made."""
        import yizkr.pipeline.llm as llm

        def no_client(*args, **kwargs):
            raise AssertionError("a client was made before the refusal")

        monkeypatch.setattr(llm, "make_client", no_client)
        deck = self.deck_as(tmp_path, name)
        forbid_reading(monkeypatch)
        with pytest.raises(SystemExit) as refused:
            self.transcribe(tmp_path, deck)
        self.assert_refused(refused, name)

    def test_an_unreadable_deck_is_refused_naming_the_run(
        self, engine, listed, tmp_path
    ):
        """A deck that is not an archive is still refused as a deck, not
        as a list that cannot be followed."""
        deck = tmp_path / "corpus" / TALK
        deck.write_bytes(b"not an archive")
        with pytest.raises(SystemExit) as refused:
            self.transcribe(tmp_path, deck)
        self.assert_refused(refused, TALK)
        assert engine.sent == []

    def test_a_corpus_folder_that_cannot_be_listed_is_not_reached(
        self, engine, listed, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(CORPUS_DIR_VAR, str(tmp_path / "moved away"))
        with pytest.raises(SystemExit) as refused:
            self.transcribe(tmp_path, listed)
        self.assert_refused(refused, TALK)
        assert engine.sent == []

    def test_a_deck_is_refused_with_no_list(self, engine, tmp_path):
        deck = talk(tmp_path / "corpus")
        with pytest.raises(SystemExit, match="deck"):
            self.transcribe(tmp_path, deck)
        assert engine.sent == []

    def test_an_image_still_reaches_the_engine(self, engine, tmp_path):
        """The refusal is the archive's alone: the talk's photograph as a
        file of its own, on no list, is sent."""
        scan = tmp_path / "album-photograph.png"
        scan.write_bytes(TALK_IMAGE)
        with pytest.raises(Reached):
            self.transcribe(tmp_path, scan)
        assert engine.sent == [("album-photograph.png", TALK_IMAGE)]

    @pytest.mark.parametrize(
        "checked", [True, False], ids=["checked", "unchecked"]
    )
    @pytest.mark.parametrize("name", DECK_NAMES)
    def test_the_transcriber_never_sends_a_deck(
        self, tmp_path, monkeypatch, name, checked
    ):
        """The vision transcriber refuses a deck or another archive
        itself, whoever calls it and whatever it is called, before it
        hashes, counts or sends anything."""
        from yizkr.pipeline.transcribe import LlmVisionTranscriber

        engine = Engine()
        deck = self.deck_as(tmp_path, name)
        forbid_reading(monkeypatch)
        transcriber = LlmVisionTranscriber(engine)
        read = (
            transcriber.transcribe
            if checked
            else transcriber.transcribe_unchecked
        )
        with pytest.raises(ValueError, match="ZIP archive"):
            read(deck)
        assert engine.sent == []

    def test_a_run_fails_a_renamed_deck_rather_than_send_it(self, tmp_path):
        """A run routes a deck called as an image to vision, and the
        transcriber's refusal fails that document closed."""
        from tests.unit.test_orchestrate import ScriptedOrchestrator, quiet

        engine = Engine()

        class Run(ScriptedOrchestrator):
            def _llm(self, role, doc):
                return engine

        folder = tmp_path / "docs"
        self.deck_as(tmp_path, "album-photograph.png").rename(
            folder.mkdir() or folder / "album-photograph.png"
        )
        orchestrator = Run(folder, tmp_path / "state", log=quiet)
        orchestrator.run()
        [doc] = orchestrator.documents()
        assert doc.state.status == "failed"
        assert "ZIP archive" in doc.state.error
        assert engine.sent == []
