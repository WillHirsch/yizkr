"""The model roles' settings, held without a driver: the effort and model
each role runs, where that model is configured, the refusal of an alias,
and the run manifest's record of the roles."""

import pytest

from yizkr.pipeline.run_resources import BORROWED, MODEL_VARIABLE, RoleSettings
from yizkr.pipeline.rundir import ModelAliasRefused
from yizkr.pipeline.stage_planning import MODEL_ROLES

MAIN = "claude-opus-5-5"
EXTRACTION = "claude-sonnet-5"


@pytest.fixture(autouse=True)
def main_model(monkeypatch):
    monkeypatch.setenv(MODEL_VARIABLE, MAIN)


def models(settings: RoleSettings) -> dict[str, str | None]:
    return {role: settings.configured_model(role)[0] for role in MODEL_ROLES}


def test_the_critique_alone_runs_the_review_effort():
    settings = RoleSettings(
        "claude-code", extract_effort="medium", review_effort="max"
    )
    assert {
        role: settings.engine_settings(role)[0] for role in MODEL_ROLES
    } == {"transcribe": "medium", "extract": "medium", "critique": "max"}
    with pytest.raises(KeyError):
        settings.engine_settings("summary")


def test_extraction_alone_takes_the_extraction_model():
    settings = RoleSettings("claude-code", extract_model=EXTRACTION)
    assert {
        role: settings.engine_settings(role)[1] for role in MODEL_ROLES
    } == {"transcribe": None, "extract": EXTRACTION, "critique": None}
    assert models(settings) == {
        "transcribe": MAIN,
        "extract": EXTRACTION,
        "critique": MAIN,
    }
    own, main = settings.configured_model("extract")[1], f"${MODEL_VARIABLE}"
    assert own != main
    assert settings.configured_model("critique")[1] == main


def test_the_main_model_is_read_at_each_call(monkeypatch):
    settings = RoleSettings("claude-code")
    monkeypatch.setenv(MODEL_VARIABLE, EXTRACTION)
    assert set(models(settings).values()) == {EXTRACTION}


@pytest.mark.parametrize("unset", [None, ""])
def test_a_role_can_have_no_model(monkeypatch, unset):
    if unset is None:
        monkeypatch.delenv(MODEL_VARIABLE)
    else:
        monkeypatch.setenv(MODEL_VARIABLE, unset)
    settings = RoleSettings("claude-code", extract_model=EXTRACTION)
    assert models(settings) == {
        "transcribe": None,
        "extract": EXTRACTION,
        "critique": None,
    }
    # no model is no alias: the preflight refuses it for itself
    settings.refuse_model_aliases()


def test_an_alias_is_refused_for_the_roles_it_serves(monkeypatch):
    own = RoleSettings("claude-code", extract_model="sonnet")
    own.refuse_model_aliases(["transcribe", "critique"])
    with pytest.raises(ModelAliasRefused):
        own.refuse_model_aliases(["extract"])
    monkeypatch.setenv(MODEL_VARIABLE, "opus")
    main = RoleSettings("claude-code", extract_model=EXTRACTION)
    main.refuse_model_aliases(["extract"])
    for roles in (["transcribe"], ["critique"], MODEL_ROLES):
        with pytest.raises(ModelAliasRefused):
            main.refuse_model_aliases(roles)


def test_a_refusal_names_each_alias_once_with_its_roles(monkeypatch):
    monkeypatch.setenv(MODEL_VARIABLE, "opus")
    settings = RoleSettings("claude-code", extract_model="sonnet")
    with pytest.raises(ModelAliasRefused) as refused:
        settings.refuse_model_aliases()
    reason = str(refused.value)
    assert reason.count("'opus'") == 1
    assert reason.count("'sonnet'") == 1
    assert "transcribe, critique" in reason


def test_a_borrowed_engine_configures_no_model(monkeypatch):
    monkeypatch.setenv(MODEL_VARIABLE, "opus")
    settings = RoleSettings(BORROWED, extract_model=EXTRACTION)
    assert set(models(settings).values()) == {None}
    settings.refuse_model_aliases()
    recorded = settings.manifest_roles()
    assert {record["model"] for record in recorded.values()} == {None}


def test_the_manifest_records_every_role():
    settings = RoleSettings(
        "claude-code",
        extract_effort="medium",
        review_effort="max",
        extract_model=EXTRACTION,
        epochs={"critique": 2},
        refresh=("transcribe",),
    )
    assert settings.manifest_roles() == {
        "transcribe": {
            "model": MAIN,
            "effort": "medium",
            "epoch": 0,
            "refresh": True,
            "client": None,
        },
        "extract": {
            "model": EXTRACTION,
            "effort": "medium",
            "epoch": 0,
            "refresh": False,
            "client": None,
        },
        "critique": {
            "model": MAIN,
            "effort": "max",
            "epoch": 2,
            "refresh": False,
            "client": None,
        },
    }
