"""Offline tests for media upload against MediaWiki's upload warnings
(ADR-0010, #186).

`WikiClient.upload_file` must never overwrite a wiki file and never store
the same bytes twice. It runs here against `FakeMediaWiki`, which answers
``action=upload`` the way MediaWiki 1.45 does (response shapes checked
against the live instance with stash-only uploads), including reading a
boolean parameter as true whenever it is present, so ``ignorewarnings=0``
ignores every warning just as the real API does.
"""

from __future__ import annotations

import hashlib

import pytest

from yizkr.core.wiki import NAME_WARNINGS, WikiClient

# Fictional media (docs/EXAMPLES.md): a scan of the Löbig family's
# studio portrait of the 1890s, and a second, different scan of it.
PORTRAIT = b"\x89PNG synthetic scan of the 1890s studio portrait"
RESCAN = b"\x89PNG synthetic re-scan of the 1890s studio portrait"
LETTERHEAD = b"\x89PNG synthetic Hutmacherei zum Blauen Hut letterhead"


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class FakeMediaWiki:
    """MediaWiki's ``action=upload``, as far as `upload_file` meets it.

    * A boolean parameter is true whenever it is present, whatever its
      value (MediaWiki's API convention).
    * Without ``ignorewarnings`` any warning stops the upload: the result
      is ``Warning`` and nothing is stored.
    * With it the upload goes ahead: different bytes under a stored name
      become a new version of that file (an overwrite), identical bytes
      under a new name a second file; only identical bytes under the
      same name fail (``fileexists-no-change``).
    * Warnings: ``exists`` (plus ``nochange`` when the file's current
      version has these bytes), ``duplicate`` (the other files holding
      them), ``duplicate-archive`` (a deleted file held them),
      ``bad-prefix`` for camera-style names, and any set in `extra`.
    """

    BAD_PREFIXES = ("IMG", "DSC_")

    def __init__(self) -> None:
        # file name -> the SHA-1 of each version, oldest first
        self.versions: dict[str, list[str]] = {}
        self.deleted: dict[str, str] = {}  # SHA-1 -> the deleted file's name
        self.extra: dict[str, object] = {}  # further warnings to report
        self.requests: list[dict] = []

    @staticmethod
    def normalise(name: str) -> str:
        name = name.replace(" ", "_")
        return name[:1].upper() + name[1:]

    def current(self) -> dict[str, str]:
        """File name -> SHA-1 of its current version."""
        return {name: shas[-1] for name, shas in self.versions.items()}

    def store(self, name: str, data: bytes) -> None:
        """Put a file on the wiki directly (fixture setup)."""
        self.versions.setdefault(self.normalise(name), []).append(sha1(data))

    def post_upload(self, fields: dict, data: bytes) -> dict:
        self.requests.append(dict(fields))
        name = self.normalise(fields["filename"])
        content = sha1(data)
        warnings: dict[str, object] = {}
        if name in self.versions:
            warnings["exists"] = name
            if self.versions[name][-1] == content:
                warnings["nochange"] = {"timestamp": "2026-09-27T00:00:00Z"}
        elif name.startswith(self.BAD_PREFIXES):
            warnings["bad-prefix"] = name
        duplicates = [
            other
            for other, held in self.current().items()
            if held == content and other != name
        ]
        if duplicates:
            warnings["duplicate"] = duplicates
        if content in self.deleted:
            warnings["duplicate-archive"] = self.deleted[content]
        warnings.update(self.extra)
        if "ignorewarnings" not in fields:
            if warnings:
                return {
                    "upload": {
                        "result": "Warning",
                        "warnings": warnings,
                        "filekey": "stashed.png",
                    }
                }
        elif "nochange" in warnings:
            return {"error": {"code": "fileexists-no-change", "info": "..."}}
        self.versions.setdefault(name, []).append(content)
        result: dict[str, object] = {"result": "Success", "filename": name}
        if warnings:
            result["warnings"] = warnings
        return {"upload": result}


class FakeUploadClient(WikiClient):
    """A WikiClient whose upload requests go to a FakeMediaWiki."""

    def __init__(self, api: FakeMediaWiki) -> None:
        self.api = api

    def _post_upload(self, path, fields):
        return self.api.post_upload(fields, path.read_bytes())


@pytest.fixture
def api() -> FakeMediaWiki:
    return FakeMediaWiki()


@pytest.fixture
def client(api) -> FakeUploadClient:
    return FakeUploadClient(api)


def media(tmp_path, name: str, data: bytes, folder: str = "scans"):
    path = tmp_path / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def tagged(stem: str, data: bytes, suffix: str = ".png") -> str:
    return f"{stem} {sha1(data)[:8]}{suffix}"


class TestUploadWarnings:
    def test_a_new_file_is_uploaded_with_warnings_honoured(
        self, api, client, tmp_path
    ):
        page = client.upload_file(
            media(tmp_path, "portrait-1890s.png", PORTRAIT)
        )

        assert page == "File:Portrait-1890s.png"
        assert api.current() == {"Portrait-1890s.png": sha1(PORTRAIT)}
        # any ignorewarnings, even "0", would make MediaWiki ignore them
        assert all("ignorewarnings" not in r for r in api.requests)

    def test_same_name_same_bytes_reuses_the_file(self, api, client, tmp_path):
        path = media(tmp_path, "portrait-1890s.png", PORTRAIT)
        first = client.upload_file(path)

        assert client.upload_file(path) == first  # exists + nochange
        assert api.versions == {"Portrait-1890s.png": [sha1(PORTRAIT)]}

    def test_same_bytes_under_a_new_name_reuse_the_stored_file(
        self, api, client, tmp_path
    ):
        path = media(tmp_path, "portrait-1890s.png", PORTRAIT)
        first = client.upload_file(path)

        copy = media(tmp_path, "muckel and cilli.png", PORTRAIT)
        assert client.upload_file(copy) == first  # duplicate
        assert api.current() == {"Portrait-1890s.png": sha1(PORTRAIT)}

    def test_changed_bytes_never_overwrite_the_stored_file(
        self, api, client, tmp_path
    ):
        client.upload_file(media(tmp_path, "portrait-1890s.png", PORTRAIT))

        rescan = media(
            tmp_path, "portrait-1890s.png", RESCAN, folder="rescans"
        )
        page = client.upload_file(rescan)

        assert page == f"File:{tagged('Portrait-1890s', RESCAN)}"
        assert api.versions == {
            "Portrait-1890s.png": [sha1(PORTRAIT)],  # untouched, one version
            tagged("Portrait-1890s", RESCAN).replace(" ", "_"): [sha1(RESCAN)],
        }
        # again: the tagged file is found as a duplicate, nothing stored
        assert client.upload_file(rescan) == page
        assert len(api.versions) == 2

    def test_changed_bytes_already_stored_elsewhere_reuse_that_file(
        self, api, client, tmp_path
    ):
        api.store("Portrait-1890s.png", PORTRAIT)
        api.store("Letterhead.png", LETTERHEAD)

        # exists (other bytes) + duplicate (these bytes, another name)
        path = media(tmp_path, "portrait-1890s.png", LETTERHEAD)
        assert client.upload_file(path) == "File:Letterhead.png"
        assert api.current() == {
            "Portrait-1890s.png": sha1(PORTRAIT),
            "Letterhead.png": sha1(LETTERHEAD),
        }

    def test_several_stored_copies_resolve_deterministically(
        self, api, client, tmp_path
    ):
        # legacy duplicates: any of them holds the bytes; the first by name
        api.store("Portrait-1890s.png", PORTRAIT)
        api.store("Muckel and cilli.png", PORTRAIT)
        path = media(tmp_path, "portrait scan.png", PORTRAIT)
        assert client.upload_file(path) == "File:Muckel and cilli.png"

    def test_both_names_holding_other_bytes_is_refused(
        self, api, client, tmp_path
    ):
        api.store("Portrait-1890s.png", PORTRAIT)
        api.store(tagged("Portrait-1890s", RESCAN), LETTERHEAD)
        before = {name: list(shas) for name, shas in api.versions.items()}

        rescan = media(tmp_path, "portrait-1890s.png", RESCAN)
        with pytest.raises(RuntimeError, match="neither is overwritten"):
            client.upload_file(rescan)
        assert api.versions == before

    def test_a_name_only_warning_is_overridden(self, api, client, tmp_path):
        page = client.upload_file(media(tmp_path, "IMG_0001.png", PORTRAIT))

        assert page == "File:IMG 0001.png"
        assert api.current() == {"IMG_0001.png": sha1(PORTRAIT)}
        warned, overridden = api.requests
        assert "ignorewarnings" not in warned
        assert overridden["ignorewarnings"] == "1"

    @pytest.mark.parametrize("warning", sorted(NAME_WARNINGS))
    def test_every_name_warning_is_overridden(
        self, api, client, tmp_path, warning
    ):
        api.extra = {warning: "Portrait-1890s.png"}
        page = client.upload_file(
            media(tmp_path, "portrait-1890s.png", PORTRAIT)
        )
        assert page == "File:Portrait-1890s.png"
        assert api.current() == {"Portrait-1890s.png": sha1(PORTRAIT)}

    def test_deleted_bytes_are_refused(self, api, client, tmp_path):
        api.deleted[sha1(PORTRAIT)] = "Portrait-1890s.png"

        path = media(tmp_path, "portrait-1890s.png", PORTRAIT)
        with pytest.raises(RuntimeError, match="duplicate-archive"):
            client.upload_file(path)
        assert api.versions == {}
        assert all("ignorewarnings" not in r for r in api.requests)

    def test_deleted_bytes_still_stored_elsewhere_are_reused(
        self, api, client, tmp_path
    ):
        api.store("Portrait-1890s.png", PORTRAIT)
        api.deleted[sha1(PORTRAIT)] = "Portrait copy.png"

        path = media(tmp_path, "portrait copy.png", PORTRAIT)
        assert client.upload_file(path) == "File:Portrait-1890s.png"
        assert api.current() == {"Portrait-1890s.png": sha1(PORTRAIT)}

    def test_an_unknown_warning_is_refused(self, api, client, tmp_path):
        api.extra = {"large-file": 99_000_000}
        path = media(tmp_path, "portrait-1890s.png", PORTRAIT)
        with pytest.raises(RuntimeError, match="large-file"):
            client.upload_file(path)
        assert api.versions == {}

    def test_a_file_stored_between_the_two_requests_is_reported(
        self, api, client, tmp_path
    ):
        # the override request would overwrite a file that appeared after
        # the first request; the clash is reported, not passed off
        class Racing(FakeMediaWiki):
            def post_upload(self, fields, data):
                if "ignorewarnings" in fields:
                    self.store(fields["filename"], LETTERHEAD)
                return super().post_upload(fields, data)

        racing = Racing()
        path = media(tmp_path, "IMG_0001.png", PORTRAIT)
        with pytest.raises(RuntimeError, match="met existing bytes"):
            FakeUploadClient(racing).upload_file(path)

    def test_an_upload_error_is_raised(self, api, client, tmp_path):
        class Failing(FakeMediaWiki):
            def post_upload(self, fields, data):
                return {"error": {"code": "verification-error"}}

        path = media(tmp_path, "portrait-1890s.jpg", PORTRAIT)
        with pytest.raises(RuntimeError, match="verification-error"):
            FakeUploadClient(Failing()).upload_file(path)
