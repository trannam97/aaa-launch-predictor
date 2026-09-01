"""The researcher must fail loudly rather than return a plausible guess."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.research import (
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    ResearchError,
    ResearchTarget,
    SignalDraft,
    build_prompt,
    draft_signals,
)

TARGET = ResearchTarget(
    game_name="Redfall",
    developer="Arkane Austin",
    publisher="Bethesda Softworks",
    steam_release_date=date(2023, 5, 2),
)

VALID = {
    "studio_signal": "severe_layoffs",
    "support_signal": "curtailed",
    "studio_evidence": "Reported reduction in headcount in May 2024.",
    "support_evidence": "Final update shipped, roadmap not completed.",
    "sources": ["https://example.com/report"],
    "confidence": "medium",
    "reviewer_note": "Check whether the studio was merged rather than reduced.",
}


def stub(*, stop_reason="end_turn", text=None):
    blocks = []
    if text is not None:
        blocks.append(SimpleNamespace(type="text", text=text))
    response = SimpleNamespace(stop_reason=stop_reason, content=blocks)
    return SimpleNamespace(create=lambda **kwargs: response)


def test_parses_a_well_formed_response():
    draft = draft_signals(stub(text=json.dumps(VALID)), TARGET)

    assert isinstance(draft, SignalDraft)
    assert draft.studio_signal == "severe_layoffs"
    assert draft.support_signal == "curtailed"
    assert draft.sources == ["https://example.com/report"]


@pytest.mark.parametrize("stop_reason", ["refusal", "pause_turn"])
def test_incomplete_turns_raise_rather_than_parse(stop_reason):
    # A paused or declined turn may still carry text. Half an answer about a
    # studio's fate is worse than none, because it reads like a whole one.
    client = stub(stop_reason=stop_reason, text=json.dumps(VALID))

    with pytest.raises(ResearchError):
        draft_signals(client, TARGET)


@pytest.mark.parametrize(
    "text",
    [
        "not json at all",
        json.dumps({**VALID, "studio_signal": "thriving"}),  # outside the enum
        json.dumps({k: v for k, v in VALID.items() if k != "sources"}),  # missing field
    ],
)
def test_unusable_payloads_raise(text):
    with pytest.raises(ResearchError):
        draft_signals(stub(text=text), TARGET)


def test_no_text_block_raises():
    with pytest.raises(ResearchError):
        draft_signals(stub(text=None), TARGET)


def test_unsourced_claim_is_flagged():
    claimed = SignalDraft.model_validate({**VALID, "sources": []})
    assert claimed.unsourced_claim

    unknown = SignalDraft.model_validate(
        {**VALID, "studio_signal": "unknown", "support_signal": "unknown", "sources": []}
    )
    assert not unknown.unsourced_claim, "admitting ignorance needs no source"


def test_the_prompt_withholds_how_the_launch_went():
    """Handing over the outcome invites a story that fits it."""
    prompt = build_prompt(TARGET)

    assert "Redfall" in prompt
    assert "Arkane Austin" in prompt
    for leak in ("flop", "underperform", "success", "breakout", "percentile", "positive"):
        assert leak not in prompt.lower()


def test_the_schema_and_the_prompt_offer_the_same_values():
    """A value the prompt describes but the schema rejects fails at parse time."""
    for field in ("studio_signal", "support_signal"):
        for value in RESPONSE_SCHEMA["properties"][field]["enum"]:
            assert f"`{value}`" in SYSTEM_PROMPT
    assert RESPONSE_SCHEMA["additionalProperties"] is False


def test_the_prompt_spends_its_weight_on_the_closure_asymmetry():
    """The one failure mode that is expensive downstream, so it must be argued."""
    assert "Silence is not evidence" in SYSTEM_PROMPT
    assert "`unknown`, not `closed`" in SYSTEM_PROMPT


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, ""),
        ({"studio_signal": "closed"}, "CLOSED-verify"),
        ({"sources": []}, "no-sources"),
        ({"confidence": "low"}, "low-confidence"),
        (
            {"studio_signal": "closed", "sources": [], "confidence": "low"},
            "CLOSED-verify no-sources low-confidence",
        ),
    ],
)
def test_review_flags_surface_what_needs_a_human(overrides, expected):
    assert SignalDraft.model_validate({**VALID, **overrides}).review_flags == expected
