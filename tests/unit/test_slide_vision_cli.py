"""Run and estimate adapters share explicit, validated slide selections."""

import json
from pathlib import Path

import pytest

from yizkr import cli
from yizkr.claims import ArtifactRef
from yizkr.pipeline import estimate, scratch

SOURCE = ArtifactRef.from_bytes(b"fictional native presentation").sha1


@pytest.mark.parametrize("action", ["run", "estimate"])
@pytest.mark.parametrize("mode", ["explicit", "layout"])
def test_cli_passes_same_typed_selection_to_run_and_estimate(
    tmp_path, monkeypatch, action, mode
):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    exclusions = tmp_path / "exclusions.txt"
    exclusions.write_text("")
    env = tmp_path / "fictional.env"
    env.write_text(f"YIZKR_EXCLUDE_FILE={exclusions}\n")
    selection = tmp_path / "slides.json"
    selection.write_text(
        json.dumps(
            {
                SOURCE: {
                    **(
                        {"positions": [3, 1]}
                        if mode == "explicit"
                        else {"mode": "layout"}
                    ),
                    "settings": {
                        "executable": "fictional-office",
                        "long_edge": 640,
                    },
                }
            }
        )
    )
    seen = {}

    def invoke(folder, **options):
        assert folder == corpus
        seen.update(options)
        if action == "run":
            return 0
        return estimate.Estimate(
            run=tmp_path / "run", resumed=False, cache=Path("cache")
        )

    module = scratch if action == "run" else estimate
    monkeypatch.setattr(module, f"scratch_{action}", invoke)
    args = cli.build_parser().parse_args(
        [
            "--env-file",
            str(env),
            "scratch",
            action,
            "--corpus",
            str(corpus),
            "--slide-vision",
            str(selection),
            "--until",
            "transcription",
        ]
    )
    assert args.func(args) == 0
    choice = seen["slide_vision"][SOURCE]
    assert choice.configuration() == {
        **(
            {"positions": [1, 3]} if mode == "explicit" else {"mode": "layout"}
        ),
        "settings": {
            "executable": "fictional-office",
            "long_edge": 640,
            "timeout": 120.0,
        },
    }
    assert (
        choice.request(tmp_path / "renders").directory == tmp_path / "renders"
    )


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"short": {"positions": [1]}},
        {SOURCE.upper(): {"positions": [1]}},
        {SOURCE: {"positions": []}},
        {SOURCE: {"positions": [0]}},
        {SOURCE: {"positions": [True]}},
        {SOURCE: {"positions": [1, 1]}},
        {SOURCE: {"positions": [1.0]}},
        {SOURCE: {"positions": ["1"]}},
        {SOURCE: {"positions": [1], "directory": "/tmp/unexpected"}},
        {SOURCE: {"positions": [1], "settings": {"executable": []}}},
        {SOURCE: {"positions": [1], "settings": {"timeout": True}}},
        {SOURCE: {"positions": [1], "settings": {"timeout": float("nan")}}},
        {SOURCE: {"positions": [1], "settings": {"long_edge": True}}},
        {SOURCE: {"positions": [1], "settings": {"misspelled": 1}}},
        {SOURCE: {"mode": "unknown"}},
        {SOURCE: {"mode": []}},
        {SOURCE: {"mode": "explicit"}},
        {SOURCE: {"mode": "layout", "positions": []}},
        {SOURCE: {"mode": "layout", "positions": [1]}},
    ],
)
def test_invalid_selection_cannot_become_another_reading_plan(tmp_path, value):
    path = tmp_path / "selection.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        cli._load_slide_vision(path)


def test_duplicate_source_or_setting_is_not_silently_overwritten(tmp_path):
    path = tmp_path / "selection.json"
    for value in (
        f'{{"{SOURCE}":{{"positions":[1]}},"{SOURCE}":{{"positions":[2]}}}}',
        f'{{"{SOURCE}":{{"positions":[1],"positions":[2]}}}}',
    ):
        path.write_text(value)
        with pytest.raises(ValueError, match="repeats a key"):
            cli._load_slide_vision(path)


def test_default_cli_does_not_enable_slide_vision():
    assert (
        cli.build_parser().parse_args(["scratch", "run"]).slide_vision is None
    )
    assert (
        cli.build_parser().parse_args(["scratch", "estimate"]).slide_vision
        is None
    )
