"""Live `yizkr scratch reset` (#199): the one command that resets the
instance and repopulates it for a scratch run (yizkr.reset).

* Every live session clears the recorded instance-contents key as it
  starts (conftest.py): the record names this session's process.
* The reset runs twice in a row, each time through every step: the stack
  stopped, its graph volumes removed, started afresh, the manifest
  bootstrapped, the queries published and the smoke test passed. An item
  written between the two resets is gone after the second, so the
  volumes really went.
* A key recorded after a reset, as the scratch write stage records it,
  stands until a live test writes fictional data, and that write voids
  it: the next scratch run resets first.
* A kept-state marker on the instance makes the reset refuse before it
  changes anything, and the marker is gone again at the end.

Only the first runs in every live session. The others reset the one
instance, which takes minutes, and so run only when asked:
`YIZKR_LIVE_RESET=1 uv run pytest -m live
tests/integration/test_live_scratch_reset.py`. Run them on their own,
since a session's earlier tests hold the ids and logins of the instance
before the reset. The reset uses the stack beside the session's env file,
under the session's hold on the live-stack lock. Fictional data only
(docs/EXAMPLES.md).
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest
from wikibaseintegrator import wbi_helpers

from yizkr.core.instance import (
    KEPT_STATE_PAGE,
    carries_kept_marker,
    read_record,
    record_contents,
    recorded_contents,
)
from yizkr.core.wiki import WikiClient
from yizkr.reset import InstanceReset, ResetRefused, stack

from .conftest import LIVE_TESTS, register_fictional

pytestmark = pytest.mark.live

OPT_IN = "YIZKR_LIVE_RESET"
STEPS = [
    "check the kept-state marker",
    "clear the write-phase state",
    "stop the stack",
    "remove the graph volumes",
    "set LocalSettings.php aside",
    "start the stack",
    "bootstrap the manifest",
    "publish the queries",
    "run the smoke test",
]
BETWEEN = "yizkr live test #199: the shop sign, written between two resets"
DESCRIPTION = "written by tests/integration; safe to delete"


@pytest.fixture
def resets(config):
    """The session's connection settings, for a test that resets the
    instance: only when asked (the module docstring)."""
    if os.environ.get(OPT_IN) != "1":
        pytest.skip(
            f"resets the one instance, so it runs only when asked: {OPT_IN}=1"
        )
    if config.env_file is None:
        pytest.fail(
            "the reset reads the stack's env file, and the session's "
            "connection settings name none"
        )
    return config


def reset(config, client: WikiClient):
    """Reset the stack beside the session's env file, confirmed, under the
    session's hold on the live-stack lock. The session's client forgets
    its login and vocabulary, which the fresh instance does not know."""
    compose, settings = stack(Path(config.env_file))
    try:
        return InstanceReset(
            compose, settings, yes=True, lock=contextlib.nullcontext
        ).run()
    finally:
        client._wbi = client._login = client._vocabulary = None


def test_a_live_session_clears_the_contents_key(config):
    """It writes to the instance, and the scratch write stage does not
    make its writes."""
    record = read_record(config)
    assert record["cleared"]["by"] == LIVE_TESTS
    assert record["cleared"]["pid"] == os.getpid()


def test_the_reset_runs_twice_and_empties_the_instance(resets, client):
    first = reset(resets, client)
    writer = WikiClient(resets)
    item = writer.wbi.item.new()
    item.labels.set("en", BETWEEN)
    item.descriptions.set("en", DESCRIPTION)
    item.write(summary="live test: written between two resets")
    assert WikiClient(resets).find_item_by_label(BETWEEN) is not None
    second = reset(resets, client)
    fresh = WikiClient(resets)
    # the volumes really went
    assert fresh.find_item_by_label(BETWEEN) is None
    for done in (first, second):
        assert [step.name for step in done.steps] == STEPS
    # the fresh instance holds the manifest's vocabulary again
    properties, items = fresh.vocabulary()
    assert "stated_in" in properties
    assert "human" in items


def test_a_write_after_a_reset_voids_the_recorded_key(resets, client):
    reset(resets, client)
    fresh = WikiClient(resets)
    record_contents(fresh, "fixture-key-199", "live-test-199")
    assert recorded_contents(fresh).key == "fixture-key-199"
    register_fictional(
        fresh, "yizkr live test #199: a letter from Felix to Rahel", "letter"
    )
    voided = recorded_contents(fresh)
    assert voided.key is None
    assert "written to since the key was recorded" in voided.why


def test_a_kept_instance_refuses_the_reset(resets):
    fresh = WikiClient(resets)
    fresh.edit_page(
        KEPT_STATE_PAGE,
        "A live test's kept-state marker (#199); safe to delete.",
        summary="live test: kept-state marker",
    )
    try:
        assert carries_kept_marker(fresh)
        compose, settings = stack(Path(resets.env_file))
        with pytest.raises(ResetRefused, match="kept-state marker"):
            InstanceReset(
                compose, settings, yes=True, lock=contextlib.nullcontext
            ).run()
        # nothing was changed: the marker is still there to be read
        assert carries_kept_marker(WikiClient(resets))
    finally:
        wbi_helpers.mediawiki_api_call_helper(
            data={
                "action": "delete",
                "title": KEPT_STATE_PAGE,
                "reason": "live test: the kept-state marker is done with",
                "token": fresh.login.get_edit_token(),
            },
            login=fresh.login,
        )
    assert not carries_kept_marker(WikiClient(resets))
