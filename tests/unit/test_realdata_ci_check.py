"""The CI real-data check (scripts/ci/realdata_check.py).

Fictional salt and invented tokens only: the real hash list and its
salt never enter tests.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unicodedata
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "ci"
    / "realdata_check.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("ci_realdata", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


rc = _load()

SALT = "5a17" * 16  # fictional
ENTRIES = [
    ("person_name", "Quillon Vantreth"),
    ("place", "Nether Brisk-upon-Tarn"),  # multi-word, 4 words
    ("place", "Brüxenhöfel"),  # diacritics
    ("date", "31 February 2999"),  # one date, two renderings
    ("date", "2999-02-31"),  # impossible date
    ("organisation", "Vantreth & Söhne"),
]
SECRET_WORDS = ("quillon", "vantreth", "brisk", "bruxenhofel")


@pytest.fixture
def hashes(tmp_path):
    path = tmp_path / "hashes.txt"
    path.write_text(rc.render_hash_list(SALT, ENTRIES))
    return path


@pytest.fixture
def scanner():
    return rc.Scanner(
        rc.parse_hash_list(rc.render_hash_list(SALT, ENTRIES)), SALT
    )


def _cats(scanner, text: str) -> set[str]:
    return {cat for _, cat in scanner.scan(rc.text_run("t", text))}


def _run(monkeypatch, capsys, hashes, *args, salt=SALT):
    if salt is None:
        monkeypatch.delenv(rc.SALT_ENV, raising=False)
    else:
        monkeypatch.setenv(rc.SALT_ENV, salt)
    code = rc.main(["--hashes", str(hashes), *args])
    out, err = capsys.readouterr()
    return code, out, err


# --- canonical form and the hash list ---------------------------------------


def test_canonical_folds_case_accents_and_punctuation():
    assert rc.canonical("Brüxenhöfel") == "bruxenhofel"
    nfd = unicodedata.normalize("NFD", "Brüxenhöfel")
    assert rc.canonical(nfd) == "bruxenhofel"
    assert rc.canonical("NETHER Brisk-upon-Tarn.") == "nether brisk upon tarn"
    assert rc.canonical("2999-02-31") == "2999 02 31"


def test_hash_list_is_deterministic_and_holds_no_tokens():
    text = rc.render_hash_list(SALT, ENTRIES)
    assert text == rc.render_hash_list(SALT, list(reversed(ENTRIES)))
    parsed = rc.parse_hash_list(text)
    assert parsed.max_ngram == 4  # "nether brisk upon tarn"
    assert len(parsed.table) == len(ENTRIES)
    assert not any(w in text.lower() for w in SECRET_WORDS)


def test_hash_list_depends_on_the_salt():
    a = rc.parse_hash_list(rc.render_hash_list(SALT, ENTRIES))
    b = rc.parse_hash_list(rc.render_hash_list("other", ENTRIES))
    assert not set(a.table) & set(b.table)


@pytest.mark.parametrize(
    "text",
    [
        "salt-check: " + "0" * 64 + "\nplace:" + "1" * 64 + "\n",
        "max-ngram: 2\nplace:" + "1" * 64 + "\n",
        "max-ngram: 2\nsalt-check: " + "0" * 64 + "\n",
        "max-ngram: 2\nsalt-check: " + "0" * 64 + "\nplace:xyz\n",
    ],
    ids=["no-max-ngram", "no-salt-check", "no-entries", "malformed"],
)
def test_malformed_hash_list_fails(text):
    with pytest.raises(rc.CheckError):
        rc.parse_hash_list(text)


# --- matching ---------------------------------------------------------------


def test_multi_word_entry_matches_only_the_whole_run(scanner):
    assert _cats(scanner, "Letter from quillon  VANTRETH, 1 p.") == {
        "person_name"
    }
    assert _cats(scanner, "Quillon wrote it") == set()
    assert _cats(scanner, "Quillon Vantrethson") == set()
    assert _cats(scanner, "mailed from nether brisk upon tarn") == {"place"}
    assert _cats(scanner, "Nether Brisk") == set()


def test_date_renderings(scanner):
    assert _cats(scanner, "born 31 February 2999") == {"date"}
    assert _cats(scanner, "born 2999-02-31") == {"date"}
    assert _cats(scanner, "born 2999/02/31") == {"date"}  # same words
    # A rendering is matched only when listed: 31.02.2999 is not.
    assert _cats(scanner, "born 31.02.2999") == set()


def test_diacritics_fold_both_ways(scanner):
    assert _cats(scanner, "near BRUXENHOFEL") == {"place"}
    assert _cats(scanner, unicodedata.normalize("NFD", "Brüxenhöfel")) == {
        "place"
    }
    assert _cats(scanner, "Vantreth und Sohne") == set()
    assert _cats(scanner, "VANTRETH & SÖHNE Ltd") == {"organisation"}


# --- diff parsing -----------------------------------------------------------

DIFF = """\
diff --git a/docs/notes.md b/docs/notes.md
index 1111111..2222222 100644
--- a/docs/notes.md
+++ b/docs/notes.md
@@ -10,3 +10,4 @@ heading
 context mentioning Quillon Vantreth is already upstream
-removed line about Quillon Vantreth
+replacement line
+added: Quillon Vantreth
 more context
@@ -40 +41,4 @@
 tail context
+a letter posted in Nether Brisk
+upon Tarn, in the snow
+++ Brüxenhöfel after a plus-plus prefix
\\ No newline at end of file
"""


def test_diff_scans_added_lines_with_new_file_numbers(scanner):
    hits = [h for run in rc.diff_runs(DIFF) for h in scanner.scan(run)]
    assert hits == [
        ("docs/notes.md:12", "person_name"),
        ("docs/notes.md:42", "place"),  # wrapped across two added lines
        ("docs/notes.md:44", "place"),  # "+++" content inside a hunk
    ]


NEW_FILES = """\
diff --git a/docs/quillon_vantreth.md b/docs/quillon_vantreth.md
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/docs/quillon_vantreth.md
@@ -0,0 +1 @@
+nothing to see
diff --git a/var/Quillon Vantreth/.keep b/var/Quillon Vantreth/.keep
new file mode 100644
index 0000000..e69de29
diff --git "a/Br\\303\\274xenh\\303\\266fel" "b/Br\\303\\274xenh\\303\\266fel"
new file mode 100644
index 0000000..4444444
--- /dev/null
+++ "b/Br\\303\\274xenh\\303\\266fel"
@@ -0,0 +1 @@
+plain
diff --git a/old.md b/quillon-vantreth.md
similarity index 100%
rename from old.md
rename to quillon-vantreth.md
diff --git a/src/quillon_vantreth.py b/src/quillon_vantreth.py
index 5555555..6666666 100644
--- a/src/quillon_vantreth.py
+++ b/src/quillon_vantreth.py
@@ -1 +1 @@
-x = 1
+x = 2
"""


def test_paths_of_added_and_renamed_files_are_checked(scanner):
    hits = [h for run in rc.diff_runs(NEW_FILES) for h in scanner.scan(run)]
    assert hits == [
        ("new path of diff file #1", "person_name"),
        ("new path of diff file #2", "person_name"),  # empty file
        ("new path of diff file #3", "place"),  # git-quoted path
        ("new path of diff file #4", "person_name"),  # rename
    ]  # file #5 already existed: modifying it does not re-add its path


def test_commit_messages_across_pages(scanner, tmp_path):
    pages = [
        [{"sha": "a" * 40, "commit": {"message": "Tidy docs\n\nNo data."}}],
        [{"sha": "b" * 40, "commit": {"message": "Fix\n\nper 2999-02-31"}}],
    ]
    runs = list(rc.commit_runs(json.dumps(pages)))
    hits = [h for run in runs for h in scanner.scan(run)]
    assert hits == [("commit bbbbbbb message:3", "date")]


# --- CLI ----------------------------------------------------------------


def test_cli_reports_location_and_category_never_the_token(
    monkeypatch, capsys, hashes, tmp_path
):
    diff = tmp_path / "pr.diff"
    diff.write_text(DIFF, encoding="utf-8")
    body = tmp_path / "body.txt"
    body.write_text("Title\n\nSee Quillon Vantreth's letter\n")
    code, out, err = _run(
        monkeypatch,
        capsys,
        hashes,
        "--diff",
        str(diff),
        "--text",
        f"pr-body={body}",
    )
    assert code == 1
    assert "docs/notes.md:12: person_name\n" in out
    assert "pr-body:3: person_name\n" in out
    assert "4 hit(s) at 4 location(s)" in out
    assert not any(w in (out + err).lower() for w in SECRET_WORDS)


def test_cli_clean_input_passes(monkeypatch, capsys, hashes, tmp_path):
    clean = DIFF
    for token in ("Quillon", "Brisk", "Brüxenhöfel"):
        clean = clean.replace(token, "X.")
    diff = tmp_path / "pr.diff"
    diff.write_text(clean, encoding="utf-8")
    code, out, _ = _run(monkeypatch, capsys, hashes, "--diff", str(diff))
    assert code == 0
    assert "clean" in out


@pytest.mark.parametrize("salt", [None, "", "   "])
def test_missing_salt_fails_closed(monkeypatch, capsys, hashes, salt):
    code, out, err = _run(
        monkeypatch, capsys, hashes, "--text", "t=/dev/null", salt=salt
    )
    assert code == 2
    assert "REALDATA_SALT is not set" in err
    assert "failing closed" in err


def test_wrong_salt_fails_closed(monkeypatch, capsys, hashes):
    code, _, err = _run(
        monkeypatch, capsys, hashes, "--text", "t=/dev/null", salt="c0ffee"
    )
    assert code == 2
    assert "salt-check" in err


def test_unreadable_inputs_fail_closed(monkeypatch, capsys, hashes, tmp_path):
    missing = tmp_path / "missing"
    assert _run(monkeypatch, capsys, missing, "--text", "t=/dev/null")[0] == 2
    assert _run(monkeypatch, capsys, hashes, "--diff", str(missing))[0] == 2
    assert _run(monkeypatch, capsys, hashes)[0] == 2  # nothing to check
    bad = tmp_path / "commits.json"
    bad.write_text("[{")
    assert (
        _run(monkeypatch, capsys, hashes, "--commits-json", str(bad))[0] == 2
    )
