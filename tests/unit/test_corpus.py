"""The corpus location and the never-ingest list (#29).

Offline; every corpus is a temp folder of fictional documents from the
example universe (docs/EXAMPLES.md). The list fails closed: a configured
list that cannot be read, or a line that would silently match nothing,
is an error, and `ingest run` refuses to start without a list.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata

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
