"""The researcher must fail loudly rather than return a plausible guess."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.research import (
    BATCH_UNSUPPORTED_KEYS,
    CACHE_CONTROL,
    FALLBACK_BETA,
    MAX_TOKENS,
    RESPONSE_SCHEMA,
    SIGNAL_WINDOW_MONTHS,
    SYSTEM_PROMPT,
    WEB_SEARCH_TOOL,
    MessagesClient,
    ResearchError,
    ResearchTarget,
    SignalDraft,
    Usage,
    batch_params,
    batch_requests,
    batch_status,
    build_prompt,
    collect_batch,
    compare_to_curated,
    draft_signals,
    parse_response,
    read_usage,
    request_kwargs,
    submit_batch,
    summarise_comparisons,
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
    "alternative_reading": "",
    "reviewer_note": "Check whether the studio was merged rather than reduced.",
}


def stub_message(text=None, stop_reason="end_turn"):
    """One finished message, as either path receives it."""
    blocks = [SimpleNamespace(type="text", text=text)] if text is not None else []
    return SimpleNamespace(stop_reason=stop_reason, content=blocks)


class _Stream:
    """What `client.beta.messages.stream(...)` gives back: a context manager
    whose `.get_final_message()` is the finished message."""

    def __init__(self, response, seen):
        self._response, self._seen = response, seen

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._response


def stub(*, stop_reason="end_turn", text=None):
    response = stub_message(text, stop_reason)
    seen: dict = {}
    client = SimpleNamespace(seen=seen)
    client.stream = lambda **kwargs: (seen.update(kwargs), _Stream(response, seen))[1]
    return client


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


def test_a_batch_entry_is_the_live_request_minus_what_a_batch_cannot_carry():
    """A batch that differs from what was smoke-tested answers a different
    question, so the two paths share one builder -- but they cannot be
    identical. The first live batch run returned:

        requests[0] (custom_id='243470'): The `fallbacks` parameter is not
        supported for batch requests.

    So an entry is the live request with exactly those keys removed, and
    nothing else."""
    entry = batch_requests([("570", TARGET)])[0]

    expected = request_kwargs(TARGET)
    for key in BATCH_UNSUPPORTED_KEYS:
        expected.pop(key, None)
    assert entry["params"] == expected


def test_the_live_path_keeps_the_fallback_the_batch_path_drops():
    """Stripping it for batch must not quietly disarm the synchronous path,
    where the fallback is supported and wanted."""
    live = request_kwargs(TARGET)
    assert live["fallbacks"] == "default"
    assert live["betas"] == [FALLBACK_BETA]

    batched = batch_params(TARGET)
    assert "fallbacks" not in batched
    assert "betas" not in batched


def test_stripping_the_fallback_changes_nothing_about_the_question_asked():
    """The prompt, tools, schema, thinking and caching are what the smoke test
    validated. Only reroute-on-refusal differs between the paths."""
    live, batched = request_kwargs(TARGET), batch_params(TARGET)
    for key in ("model", "system", "messages", "tools", "output_config", "thinking"):
        assert batched[key] == live[key], key


def test_every_target_is_queued_under_its_own_key():
    requests = batch_requests([("570", TARGET), ("440", OTHER)])

    assert [r["custom_id"] for r in requests] == ["570", "440"]
    assert (
        requests[0]["params"]["messages"][0]["content"]
        != (requests[1]["params"]["messages"][0]["content"])
    )


def test_submitting_sends_no_beta_header():
    """The only beta this module asks for authorises the server-side fallback,
    which a batch cannot use. Sending it anyway asks for a field the request is
    about to have rejected."""
    batches = Batches()

    assert submit_batch(batches, [("570", TARGET)]) == "msgbatch_test"
    assert "betas" not in batches.seen
    assert len(batches.seen["requests"]) == 1


def test_usage_is_read_off_a_finished_message():
    """The project threw this away, so a finished run could not say what it
    cost -- the only answer was a billing dashboard, which lags and reprices."""
    message = stub_message(json.dumps(VALID))
    message.usage = SimpleNamespace(
        input_tokens=120,
        output_tokens=5000,
        cache_read_input_tokens=53000,
        cache_creation_input_tokens=18300,
        server_tool_use=SimpleNamespace(web_search_requests=7),
    )

    u = read_usage(message)

    assert (u.input_tokens, u.output_tokens) == (120, 5000)
    assert (u.cache_read_tokens, u.cache_write_tokens) == (53000, 18300)
    assert u.web_searches == 7


def test_a_response_without_usage_reads_as_zero_rather_than_raising():
    """Not our shape to control, and a missing count must never take down a
    collect that has real drafts in it."""
    assert read_usage(stub_message("{}")).output_tokens == 0
    assert read_usage(object()).web_searches == 0

    partial = stub_message("{}")
    partial.usage = SimpleNamespace(input_tokens=10, output_tokens=20)
    assert read_usage(partial).web_searches == 0
    assert read_usage(partial).output_tokens == 20


def test_usage_totals_across_rows():
    """A run's cost is the sum over its rows, so Usage adds."""
    message = stub_message("{}")
    message.usage = SimpleNamespace(
        input_tokens=1,
        output_tokens=2,
        cache_read_input_tokens=3,
        cache_creation_input_tokens=4,
        server_tool_use=SimpleNamespace(web_search_requests=5),
    )

    total = Usage()
    for _ in range(3):
        total = total + read_usage(message)

    assert (total.output_tokens, total.web_searches) == (6, 15)


def test_collect_attaches_usage_to_each_outcome():
    """Collect is where the counts are still reachable: batch results keep for
    29 days, while the billing report is a lagging, repriced aggregate."""
    message = stub_message(json.dumps(VALID))
    message.usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=900,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=8000,
        server_tool_use=SimpleNamespace(web_search_requests=3),
    )

    outcomes = collect_batch(Batches([Result("570", message=message)]), "b", {"570": "A"})

    assert outcomes[0].draft is not None
    assert outcomes[0].usage.web_searches == 3
    assert outcomes[0].usage.output_tokens == 900


def test_a_draft_still_collects_when_the_response_reports_no_usage():
    """Usage is a measurement, never a gate on the row it measures."""
    outcomes = collect_batch(
        Batches([Result("570", message=stub_message(json.dumps(VALID)))]), "b", {"570": "A"}
    )

    assert outcomes[0].draft is not None
    assert outcomes[0].usage.web_searches == 0


def test_status_reads_the_counts_without_touching_results():
    """The whole point: learn a batch is unfinished without calling `results()`,
    which raises while it is still running."""

    class Counts:
        succeeded, errored, processing, canceled, expired = 41, 2, 25, 0, 0

    class Batch:
        processing_status, request_counts = "in_progress", Counts()

    class OnlyRetrieve:
        def retrieve(self, batch_id, /):
            return Batch()

        def results(self, batch_id, /):
            raise AssertionError("status must not read results")

    status = batch_status(OnlyRetrieve(), "msgbatch_x")

    assert status.ended is False
    assert status.done == 43
    assert "25 still processing" in status.summary()
    assert "41 succeeded, 2 errored" in status.summary()


def test_status_reports_ended_when_the_batch_has_ended():
    class Counts:
        succeeded, errored, processing, canceled, expired = 68, 0, 0, 0, 0

    class Batch:
        processing_status, request_counts = "ended", Counts()

    class Batches:
        def retrieve(self, batch_id, /):
            return Batch()

    status = batch_status(Batches(), "msgbatch_x")
    assert status.ended is True
    assert status.done == 68


def test_a_batch_missing_its_counts_still_reports_a_status():
    """A shape this module does not control. Reporting `unknown` beats an
    AttributeError when the only thing being asked is "is it done"."""

    class Bare:
        def retrieve(self, batch_id, /):
            return object()

    status = batch_status(Bare(), "msgbatch_x")
    assert status.status == "unknown"
    assert status.ended is False
    assert status.done == 0


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


# --- the window the signals are measured over -------------------------------
#
# Undefined until the first live run, where all four drafts independently asked
# for it. Unbounded, "after this launch" spans a decade for an older game, and
# the 2023-25 industry contraction would drag every pre-2020 row toward
# severe_layoffs -- which with non-sustained support is a hard route to Flop.


def test_the_prompt_states_the_window_in_months():
    assert f"{SIGNAL_WINDOW_MONTHS} months from the original release date" in SYSTEM_PROMPT


def test_the_window_anchors_on_the_original_release_not_the_steam_one():
    """Twelve corpus rows reach Steam between 264 and 1848 days after they
    first released. Anchoring on the Steam date puts a 16-month window years
    past every consequence being asked about -- Horizon Zero Dawn's window
    would open in August 2020 for a launch that decided Guerrilla's 2017."""
    assert "the window runs from the original release, \nnot from the Steam arrival.**".replace(
        "\n", ""
    ) in SYSTEM_PROMPT.replace("\n", " ").replace("  ", " ")


def test_a_delayed_port_is_given_both_dates_and_told_which_one_counts():
    prompt = build_prompt(
        ResearchTarget(
            game_name="Horizon Zero Dawn Complete Edition",
            developer="Guerrilla Games",
            publisher="Sony",
            steam_release_date=date(2020, 8, 7),
            original_release_date=date(2017, 2, 28),
        )
    )
    assert "Original release date: 2017-02-28" in prompt
    assert "Reached Steam: 2020-08-07, 1256 days later" in prompt
    assert "not from this date" in prompt


def test_a_day_one_release_is_not_given_a_second_date():
    """Most rows have the two dates within a day of each other. Printing both
    would put a distinction on every prompt that matters on twelve rows."""
    prompt = build_prompt(
        ResearchTarget(
            game_name="Concord",
            developer="Firewalk Studios",
            publisher="Sony",
            steam_release_date=date(2024, 8, 23),
            original_release_date=date(2024, 8, 23),
        )
    )
    assert "Original release date: 2024-08-23" in prompt
    assert "Reached Steam" not in prompt


def test_a_row_missing_its_curated_date_falls_back_to_the_steam_one():
    """Losing the window entirely is worse than anchoring it a little late."""
    prompt = build_prompt(
        ResearchTarget(
            game_name="Some Game",
            developer=None,
            publisher=None,
            steam_release_date=date(2022, 3, 1),
            original_release_date=None,
        )
    )
    assert "Original release date: 2022-03-01" in prompt
    assert "Reached Steam" not in prompt


def test_the_window_contains_every_dated_consequence_in_the_corpus():
    """16 months is not a round number picked for feel.

    These are the seven dated studio consequences the corpus records, and they
    fall in two clusters with nothing between them: immediate, and a fiscal-year
    lag. A 12-month window misses the whole second cluster -- Volition closed 8
    days past a year after Saints Row, Arkane Austin 5 days past a year after
    Redfall, and 343 Industries was cut 14 months after Halo Infinite.
    """
    gaps_in_months = {
        "Forspoken": 1.0,
        "Immortals of Aveum": 1.0,
        "Dragon Age: The Veilguard": 2.9,
        "Concord": 5.3,
        "Redfall": 12.2,
        "Saints Row": 12.3,
        "Halo Infinite": 14.1,
    }
    missed = [name for name, gap in gaps_in_months.items() if gap > SIGNAL_WINDOW_MONTHS]

    assert not missed, f"the window excludes real consequences: {missed}"
    # And it is not so wide that a decade of industry cycles leaks in.
    assert SIGNAL_WINDOW_MONTHS < 24


def test_the_prompt_says_what_to_do_with_events_outside_the_window():
    """Finding a later closure and silently ignoring it is not the ask —
    reporting it unscored is, so a reviewer can see it was considered."""
    assert "reviewer_note" in SYSTEM_PROMPT
    assert "does not change either value" in SYSTEM_PROMPT


def test_evidence_must_also_sit_inside_the_window():
    """Three of the first four drafts supported `grew` with a 2017 expansion
    announcement about a 2014 launch."""
    assert "Evidence *for* a value must also sit inside the window" in SYSTEM_PROMPT


def test_the_prompt_says_the_date_outranks_the_name():
    """Steam reports the store name today, not the name at launch, and a
    re-release renames the appid in place. The corpus has 17 such rows: appid
    1063730 reads `New World: Aeternum` against a 2021-09-28 date, but Aeternum
    is the October 2024 relaunch; 750920 reads `Definitive Edition` against
    2018-09-14, and that edition landed in November 2019. Without this the
    window can be anchored on the wrong event entirely."""
    assert "**The date is authoritative and the name is not.**" in SYSTEM_PROMPT
    assert "Research the game as it existed on the date you are given" in SYSTEM_PROMPT


# --- the cap and the transport are one decision ------------------------------


def test_the_synchronous_path_streams():
    """MAX_TOKENS=32000 makes the SDK refuse a non-streaming request outright —
    client-side, before anything is sent — so raising the cap without switching
    transport fails every row. The highest this model takes non-streaming is
    21,333 tokens, which is why 16000 worked and 32000 did not:

        ValueError: Streaming is required for operations that may take longer
        than 10 minutes.
    """
    client = stub(text=json.dumps(VALID))

    draft_signals(client, TARGET)

    assert client.seen["max_tokens"] == MAX_TOKENS
    assert not hasattr(client, "create"), "the sync path must not fall back to create()"


def test_the_client_protocol_asks_for_stream_not_create():
    assert hasattr(MessagesClient, "stream")
    assert not hasattr(MessagesClient, "create")


# --- measuring the researcher against known answers -------------------------


def draft_with(studio, support="sustained", alternative=""):
    return SignalDraft(
        studio_signal=studio,
        support_signal=support,
        studio_evidence="",
        support_evidence="",
        sources=["https://example.com/x"],
        confidence="high",
        alternative_reading=alternative,
        reviewer_note="",
    )


@pytest.mark.parametrize(
    ("drafted", "curated", "expected"),
    [
        ("closed", "closed", "agrees"),
        # The expensive miss: a gutted studio reported as fine buries a Flop.
        ("continued", "closed", "false_benign"),
        ("grew", "severe_layoffs", "false_benign"),
        # The miss the prompt was written against.
        ("closed", "continued", "false_alarm"),
        ("severe_layoffs", "grew", "false_alarm"),
        # An honest refusal is neither, and must not be averaged with either.
        ("unknown", "closed", "refused"),
        ("unknown", "continued", "refused"),
        # Wrong, but within the same direction.
        ("closed", "severe_layoffs", "differs"),
    ],
)
def test_a_miss_is_classified_by_direction_not_just_counted(drafted, curated, expected):
    """One accuracy figure hides the only thing that matters here.

    Reporting a gutted studio as fine and reporting a healthy one as gutted are
    opposite failures with opposite costs, and averaging them together would
    let a run that buries every Flop look like a run that is merely noisy.
    """
    row = compare_to_curated("1", "G", curated, "sustained", draft_with(drafted))

    assert row.verdict == expected


def test_the_summary_separates_the_two_directions():
    rows = [
        compare_to_curated("1", "A", "closed", "abandoned", draft_with("continued")),
        compare_to_curated("2", "B", "closed", "abandoned", draft_with("closed", "abandoned")),
        compare_to_curated("3", "C", "continued", "sustained", draft_with("severe_layoffs")),
        compare_to_curated("4", "D", "grew", "sustained", draft_with("unknown")),
    ]

    tally = summarise_comparisons(rows)

    assert tally["compared"] == 4
    assert tally["false_benign"] == 1
    assert tally["false_alarm"] == 1
    assert tally["refused"] == 1
    assert tally["studio_agrees"] == 1
    assert tally["both_agree"] == 1  # only B matches on both


def test_support_agreement_is_scored_separately_from_studio():
    """severe_layoffs plus curtailed is Flop; plus sustained it is Underperform.
    Getting the studio right and the support wrong is not a half-success."""
    row = compare_to_curated(
        "1", "G", "severe_layoffs", "curtailed", draft_with("severe_layoffs", "sustained")
    )

    assert row.studio_agrees
    assert not row.support_agrees


# --- flagging a doubt that would change the tier ----------------------------


def test_an_alternative_reading_is_flagged():
    """Marvel's Midnight Suns came back severe_layoffs/sustained and unflagged
    while its own note argued for curtailed — and severe_layoffs plus curtailed
    is Flop. The row closest to changing tier was invisible to triage."""
    draft = draft_with(
        "severe_layoffs",
        alternative="support_signal=curtailed — the Switch version was cancelled 2 May 2023",
    )

    assert "alternative" in draft.review_flags


def test_no_alternative_means_no_flag():
    """Filling it on every row would make the flag noise, so the empty case
    must stay silent."""
    assert draft_with("continued").review_flags == ""
    assert draft_with("continued", alternative="   ").review_flags == ""


def test_the_prompt_asks_for_a_near_miss_not_a_habit():
    assert "alternative_reading" in SYSTEM_PROMPT
    assert "empty string" in SYSTEM_PROMPT
    assert "alternative_reading" in RESPONSE_SCHEMA["required"]


# --- a studio can end while its people keep their jobs -----------------------


def test_dissolution_into_a_parent_counts_as_closed():
    """Luminous Productions was merged into Square Enix three months after
    Forspoken, never shipped again, and has no successor. The first validation
    run drafted `continued` against a curated `closed` and said why: the rule
    listed "merged" among the things absorption covers, so a dissolution read
    as a reorganisation. Staff retention is not studio continuity.
    """
    assert "Staff being retained does not make it `continued`" in SYSTEM_PROMPT
    assert "stopped existing as a development unit" in SYSTEM_PROMPT


def test_the_absorption_exception_requires_naming_the_successor():
    """Otherwise it swallows every dissolution: the test has to be something
    checkable, not the wording of the press release."""
    assert "persists as a working unit" in SYSTEM_PROMPT
    assert "name what the team became" in SYSTEM_PROMPT


def test_widening_closed_did_not_reopen_the_door_to_inference():
    """The asymmetry this prompt exists for is unchanged: a studio nobody has
    written about is `unknown`, not `closed`."""
    assert "Silence remains not evidence" in SYSTEM_PROMPT
    assert "Silence is not evidence" in SYSTEM_PROMPT
    assert "Never infer it" in SYSTEM_PROMPT


# --- support: two boundaries, two different questions ------------------------


def test_abandoned_is_termination_not_magnitude():
    """Redfall came back `curtailed` because a substantial final update shipped,
    servers stayed up and it was never delisted — all true, and all irrelevant
    to whether support ended. Bethesda had said development would not continue.
    """
    assert "about support ending, not about how much shipped before" in SYSTEM_PROMPT


def test_a_cancelled_feature_is_not_by_itself_a_curtailment():
    """Halo Infinite lost split-screen co-op and kept shipping seasons. If one
    dropped feature reads as `curtailed`, the value stops discriminating —
    Redfall and Halo Infinite both came back `curtailed` on the first run.
    """
    assert "cancelled feature is not by itself a curtailment" in SYSTEM_PROMPT
    assert "Ask whether the plan continued" in SYSTEM_PROMPT


def test_the_two_boundaries_are_stated_as_different_questions():
    assert "whether the **plan** ran its course" in SYSTEM_PROMPT
    assert "whether support **stopped**" in SYSTEM_PROMPT


def test_a_game_with_no_announced_plan_is_not_judged_against_one():
    """Evil West's only stated commitment was patching. A light cadence there
    is not a shortfall, because nothing more was ever promised."""
    assert "does \\\nnot, and you should not read a light update cadence" in SYSTEM_PROMPT or (
        "nothing more was ever announced" in SYSTEM_PROMPT
    )


# --- per-signal agreement is not the question; the tier is -------------------


def compared(drafted_studio, curated_studio, drafted_tier, curated_tier):
    return compare_to_curated(
        "1",
        "G",
        curated_studio,
        "sustained",
        draft_with(drafted_studio),
        drafted_tier,
        curated_tier,
    )


def test_a_row_can_agree_on_both_signals_and_still_move_the_label():
    """The five-row run agreed on studio 5/5 and moved two rows anyway.

    `severe_layoffs` reads as Underperform beside `sustained` and as Flop beside
    anything else; `abandoned` overrides on its own. Studio-only counters cannot
    see either, and reported zero problems.
    """
    moved = compared("severe_layoffs", "severe_layoffs", "underperform", "flop")

    assert moved.verdict == "agrees"  # the studio counter is content
    assert not moved.tier_agrees  # the label is not


@pytest.mark.parametrize(
    ("drafted_tier", "curated_tier", "expected"),
    [
        ("flop", "flop", "agrees"),
        # Immortals of Aveum: drafted sustained where the label says abandoned,
        # turning a Flop into an Underperform.
        ("underperform", "flop", "softer"),
        # Halo Infinite: drafted curtailed where the label says sustained.
        ("flop", "underperform", "harsher"),
    ],
)
def test_the_tier_miss_is_reported_by_direction(drafted_tier, curated_tier, expected):
    """Softer buries a Flop; harsher only costs a false alarm. Not the same."""
    assert compared("closed", "closed", drafted_tier, curated_tier).tier_direction == expected


def test_a_row_without_tiers_is_unscored_rather_than_agreeing():
    """An empty tier must not be counted as a match."""
    row = compare_to_curated("1", "G", "closed", "abandoned", draft_with("closed"))

    assert not row.tier_agrees
    assert row.tier_direction == "unscored"


def test_the_summary_carries_both_tier_directions():
    rows = [
        compared("closed", "closed", "flop", "flop"),
        compared("severe_layoffs", "severe_layoffs", "underperform", "flop"),
        compared("severe_layoffs", "severe_layoffs", "flop", "underperform"),
    ]

    tally = summarise_comparisons(rows)

    assert tally["tier_agrees"] == 1
    assert tally["tier_softer"] == 1
    assert tally["tier_harsher"] == 1
    assert tally["studio_agrees"] == 3  # and the studio counter sees nothing wrong


def test_an_unconfirmed_announcement_is_not_a_delivered_one():
    """Immortals of Aveum came back `sustained` on an announced Unreal Engine
    5.2 upgrade the draft could not confirm ever shipped — it flagged the fact
    and defaulted to the benign reading anyway.

    `sustained` means delivered, so the burden is evidence of delivery. Silence
    about an announced item is a shortfall, not a completion.
    """
    assert "cannot confirm shipped has not shipped" in SYSTEM_PROMPT
    assert "burden is evidence of delivery" in SYSTEM_PROMPT


def test_the_two_silence_rules_are_distinguished_not_contradictory():
    """One says silence cannot invent an event; the other says silence about an
    announced item is a failure to deliver it. The prompt has to say why both
    are true, or the second reads as licence to infer."""
    assert "the difference is where the" in SYSTEM_PROMPT
    assert "the announcement *is* the event" in SYSTEM_PROMPT
