"""The researcher must fail loudly rather than return a plausible guess."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.research import (
    CACHE_CONTROL,
    FALLBACK_BETA,
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    WEB_SEARCH_TOOL,
    ResearchError,
    ResearchTarget,
    SignalDraft,
    batch_requests,
    build_prompt,
    collect_batch,
    draft_signals,
    parse_response,
    request_kwargs,
    submit_batch,
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


def stub_message(text=None, stop_reason="end_turn"):
    """One finished message, as either path receives it."""
    blocks = [SimpleNamespace(type="text", text=text)] if text is not None else []
    return SimpleNamespace(stop_reason=stop_reason, content=blocks)


def stub(*, stop_reason="end_turn", text=None):
    response = stub_message(text, stop_reason)
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


# --- the batch path ---------------------------------------------------------
#
# Nobody waits on a 71-row backfill, so it goes through the Batches API at half
# the token cost. These pin the two things that would quietly ruin a batch: a
# request shape that drifts from the one smoke-tested live, and a result matched
# back to the wrong game.


class Result:
    """One entry from `batches.results()`."""

    def __init__(self, custom_id, kind="succeeded", message=None, error_type=None):
        self.custom_id = custom_id
        self.result = SimpleNamespace(
            type=kind,
            message=message,
            error=SimpleNamespace(type=error_type) if error_type else None,
        )


class Batches:
    def __init__(self, results=()):
        self._results = list(results)
        self.seen = {}

    def create(self, **kwargs):
        self.seen = kwargs
        return SimpleNamespace(id="msgbatch_test")

    def results(self, batch_id, /):
        self.seen["collected"] = batch_id
        return iter(self._results)


OTHER = ResearchTarget(
    game_name="Another Game",
    developer="Someone Else",
    publisher="Publisher B",
    steam_release_date=date(2023, 5, 1),
)


def test_a_batch_entry_is_the_same_request_the_live_path_sends():
    """A batch that differs from what was smoke-tested answers a different
    question, so the two paths must share one builder."""
    entry = batch_requests([("570", TARGET)])[0]

    assert entry["params"] == request_kwargs(TARGET)


def test_every_target_is_queued_under_its_own_key():
    requests = batch_requests([("570", TARGET), ("440", OTHER)])

    assert [r["custom_id"] for r in requests] == ["570", "440"]
    assert (
        requests[0]["params"]["messages"][0]["content"]
        != (requests[1]["params"]["messages"][0]["content"])
    )


def test_submitting_returns_the_id_and_carries_the_beta():
    batches = Batches()

    assert submit_batch(batches, [("570", TARGET)]) == "msgbatch_test"
    assert batches.seen["betas"] == [FALLBACK_BETA]
    assert len(batches.seen["requests"]) == 1


def test_submitting_nothing_raises_rather_than_creating_an_empty_batch():
    with pytest.raises(ResearchError):
        submit_batch(Batches(), [])


def test_results_are_matched_by_key_not_by_position():
    """The API returns entries in any order; position would silently mislabel."""
    batches = Batches(
        [
            Result("440", message=stub_message(json.dumps({**VALID, "confidence": "low"}))),
            Result("570", message=stub_message(json.dumps(VALID))),
        ]
    )

    outcomes = {o.key: o for o in collect_batch(batches, "b", {"570": "A", "440": "B"})}

    assert outcomes["570"].draft.confidence == VALID["confidence"]
    assert outcomes["440"].draft.confidence == "low"


@pytest.mark.parametrize(
    ("kind", "error_type"),
    [("errored", "invalid_request"), ("expired", None), ("canceled", None)],
)
def test_a_failed_entry_is_reported_not_dropped(kind, error_type):
    """A silently missing row reads as a game nobody needed to research."""
    batches = Batches([Result("570", kind=kind, error_type=error_type)])

    outcome = collect_batch(batches, "b", {"570": "Some Game"})[0]

    assert outcome.draft is None
    assert kind in outcome.error
    assert "Some Game" in outcome.error


def test_an_unparseable_success_becomes_an_error_not_an_exception():
    """One bad row must not take the other seventy down with it."""
    batches = Batches(
        [
            Result("570", message=stub_message("not json")),
            Result("440", message=stub_message(json.dumps(VALID))),
        ]
    )

    outcomes = collect_batch(batches, "b", {"570": "Bad", "440": "Good"})

    assert outcomes[0].draft is None and "Bad" in outcomes[0].error
    assert outcomes[1].draft is not None


def test_a_paused_turn_in_a_batch_is_an_error_not_a_partial_draft():
    """There is no way to continue a paused turn inside a batch."""
    paused = stub_message(json.dumps(VALID))
    paused.stop_reason = "pause_turn"

    outcome = collect_batch(Batches([Result("570", message=paused)]), "b", {"570": "G"})[0]

    assert outcome.draft is None
    assert "paused" in outcome.error


# --- the request shape itself ----------------------------------------------


def test_the_search_tool_drops_blocks_nobody_reads():
    """`parse_response` reads the first text block only, so returning the raw
    search blocks bills output tokens for content that is thrown away."""
    assert WEB_SEARCH_TOOL["response_inclusion"] == "excluded"
    assert WEB_SEARCH_TOOL["type"] >= "web_search_20260318"  # response_inclusion needs it


def test_the_shared_prefix_is_cached():
    """~830 tokens of system prompt, tool and schema, identical on all 71 rows."""
    assert request_kwargs(TARGET)["cache_control"] == CACHE_CONTROL


# --- does the SDK actually accept this shape? -------------------------------


def test_a_batch_entry_uses_only_fields_the_sdk_defines():
    """A misspelled parameter is a 400 at submit time, after the queue is built.

    The SDK's TypedDicts do not validate at runtime, so this checks the keys
    against them directly. Skipped where the optional `research` extra is not
    installed — CI installs `[dev]` only — so run the suite with it before
    submitting a batch that costs money.
    """
    pytest.importorskip("anthropic")
    import typing

    from anthropic.types.beta.messages.batch_create_params import (
        MessageCreateParamsNonStreaming,
        Request,
    )

    entry = batch_requests([("570", TARGET)])[0]
    assert set(entry) <= set(typing.get_type_hints(Request))

    allowed = set(typing.get_type_hints(MessageCreateParamsNonStreaming))
    unknown = set(entry["params"]) - allowed
    assert not unknown, f"not parameters of the beta batch request: {sorted(unknown)}"


# --- hitting the token cap ---------------------------------------------------


def test_a_truncated_answer_blames_the_budget_not_the_parser():
    """The first live run lost a row to this and reported it as malformed JSON.

    Structured output guarantees schema-valid JSON only if generation finishes;
    stopping at the cap yields a truncated string, and "unparseable response"
    sends the reader to the parser instead of to max_tokens.
    """
    truncated = stub_message('{"studio_signal": "grew", "studio_evid', "max_tokens")

    with pytest.raises(ResearchError, match="token cap"):
        parse_response(truncated, "Just Cause 3")


def test_an_unparseable_response_names_the_stop_reason():
    """So an unknown cause is still diagnosable from the log alone."""
    with pytest.raises(ResearchError, match="stop_reason=end_turn"):
        parse_response(stub_message("not json"), "Some Game")
