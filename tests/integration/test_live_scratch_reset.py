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
* A kept-state page on the instance makes the reset refuse before it
  changes anything, and the page, which the test wrote, is gone again at
  the end.
* The same batches written with the same identity map after a reset give
  the same items, by their labels and descriptions, and a second write of
  them mints nothing (#109; test_live_identity_map.py).

Only the first runs in every live session. The others reset the one
instance, which takes minutes, and so run only when asked:
`YIZKR_LIVE_RESET=1 uv run pytest -m live
tests/integration/test_live_scratch_reset.py`. They run in a session of
their own, and fail at once in a session that runs other modules' tests
too, whose ids and logins the reset would take away: YIZKR_LIVE_RESET=1
left set during `make test-live` resets nothing. They fail at once on a
kept instance, which they read with calls of their own, so that a test
never resets, or unmarks, a kept instance. A reset that fails ends the
session, since it may leave the instance half reset. The reset uses the
stack beside the session's env file, under the session's hold on the
live-stack lock. Fictional data only (docs/EXAMPLES.md).
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest
import requests
from wikibaseintegrator import wbi_helpers

from yizkr.core.config import USER_AGENT
from yizkr.core.instance import (
    KEPT_STATE_PAGE,
    carries_kept_marker,
    kept_path,
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


def page_exists(config) -> bool:
    """Whether the instance holds the kept-state page, read with this
    test's own call rather than the code under test."""
    response = requests.get(
        f"{config.wikibase_url}/w/api.php",
        params={
            "action": "query",
            "titles": KEPT_STATE_PAGE,
            "format": "json",
            "formatversion": "2",
        },
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    return any(
        not page.get("missing") for page in response.json()["query"]["pages"]
    )


@pytest.fixture
def resets(request, config):
    """A reset of the stack beside the session's env file, for a test that
    resets the instance: only when asked, in a session of its own, and on
    an instance that is not kept (the module docstring). The reset is
    confirmed, and runs under the session's hold on the lock; given the
    session's client, it drops its login and vocabulary, which the fresh
    instance does not know."""
    if os.environ.get(OPT_IN) != "1":
        pytest.skip(
            f"resets the one instance, so it runs only when asked: {OPT_IN}=1"
        )
    here = request.node.nodeid.split("::")[0]
    others = sorted(
        {item.nodeid.split("::")[0] for item in request.session.items} - {here}
    )
    if others:
        pytest.fail(
            f"{OPT_IN}=1 resets the instance in a session of its own, and "
            f"this one runs {', '.join(others)} too: run {here} alone"
        )
    if config.env_file is None:
        pytest.fail(
            "the reset reads the stack's env file, and the session's "
            "connection settings name none"
        )
    marks = []
    if page_exists(config):
        marks.append(f"the page {KEPT_STATE_PAGE}")
    if os.path.lexists(kept_path(config)):
        marks.append(f"the host-side record {kept_path(config)}")
    if marks:
        pytest.fail(
            f"the instance is kept ({'; '.join(marks)}): a live test never "
            "resets a kept instance, or removes its markers"
        )

    def reset(client: WikiClient | None = None):
        compose, settings = stack(Path(config.env_file))
        try:
            return InstanceReset(
                compose, settings, yes=True, lock=contextlib.nullcontext
            ).run()
        except BaseException:
            request.session.shouldfail = (
                "a reset failed, and may have left the instance half reset, "
                "so no other test of the session runs"
            )
            raise
        finally:
            if client is not None:
                client._wbi = client._login = None
                client.forget_vocabulary()

    return reset


def test_a_live_session_clears_the_contents_key(config):
    """It writes to the instance, and the scratch write stage does not
    make its writes."""
    record = read_record(config)
    assert record["cleared"]["by"] == LIVE_TESTS
    assert record["cleared"]["pid"] == os.getpid()


def test_the_reset_runs_twice_and_empties_the_instance(resets, config, client):
    first = resets(client)
    writer = WikiClient(config)
    item = writer.wbi.item.new()
    item.labels.set("en", BETWEEN)
    item.descriptions.set("en", DESCRIPTION)
    item.write(summary="live test: written between two resets")
    assert WikiClient(config).find_item_by_label(BETWEEN) is not None
    second = resets(client)
    fresh = WikiClient(config)
    # the volumes really went
    assert fresh.find_item_by_label(BETWEEN) is None
    for done in (first, second):
        assert [step.name for step in done.steps] == STEPS
    # the fresh instance holds the manifest's vocabulary again
    properties, items = fresh.vocabulary()
    assert "stated_in" in properties
    assert "human" in items


def test_a_write_after_a_reset_voids_the_recorded_key(resets, config, client):
    resets(client)
    fresh = WikiClient(config)
    record_contents(fresh, "fixture-key-199", "live-test-199")
    assert recorded_contents(fresh).key == "fixture-key-199"
    register_fictional(
        fresh, "yizkr live test #199: a letter from Felix to Rahel", "letter"
    )
    voided = recorded_contents(fresh)
    assert voided.key is None
    assert "written to since the key was recorded" in voided.why


def test_a_kept_instance_refuses_the_reset(resets, config):
    fresh = WikiClient(config)
    try:
        fresh.edit_page(
            KEPT_STATE_PAGE,
            "A live test's kept-state marker (#199); safe to delete.",
            summary="live test: kept-state marker",
        )
        assert carries_kept_marker(fresh)
        compose, settings = stack(Path(config.env_file))
        with pytest.raises(ResetRefused, match="is kept"):
            InstanceReset(
                compose, settings, yes=True, lock=contextlib.nullcontext
            ).run()
        # nothing was changed: the marker is still there to be read
        assert carries_kept_marker(WikiClient(config))
    finally:
        # the page is this test's own: `resets` found none before it
        if page_exists(config):
            wbi_helpers.mediawiki_api_call_helper(
                data={
                    "action": "delete",
                    "title": KEPT_STATE_PAGE,
                    "reason": "live test: the kept-state marker is done with",
                    "token": fresh.login.get_edit_token(),
                },
                login=fresh.login,
            )
    assert not page_exists(config)


def test_the_same_items_after_a_reset(resets, config, client):
    """The writer's items are keyed by the identity map alone (#109): the
    same batches written with the same map after a reset give each mention
    the item they gave it before, under the same label and description,
    and writing them again mints nothing."""
    from tests.fixtures.identity import mapped

    from .test_live_identity_map import (
        HISTORY,
        TREE,
        history_batch,
        labelled,
        terms,
        tree_batch,
        write_both,
    )

    def after_a_reset():
        resets(client)
        fresh = WikiClient(config)
        history = register_fictional(fresh, HISTORY, "genealogical_report")
        tree = register_fictional(fresh, TREE, "genealogical_report")
        identity = mapped(history_batch(history), tree_batch(tree))
        # a reset instance holds one map's writes: the writer's refusal of
        # another map stays on
        made: list[dict] = []
        _, items = write_both(fresh, history, tree, identity, made, True)
        again: list[dict] = []
        _, rewritten = write_both(fresh, history, tree, identity, again, True)
        assert rewritten == items
        assert labelled(again) == []
        held = terms(fresh, items.values())
        return identity.hash, {key: held[qid] for key, qid in items.items()}

    assert after_a_reset() == after_a_reset()
