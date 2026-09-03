"""Draft the two post-launch signals Steam cannot supply.

Separating Flop from Underperform needs `studio_signal` and `support_signal`,
and `app/rubric.py` refuses to guess when both are unknown. Neither is in any
Steam field: review scores will not tell you whether a studio survived. That is
why 71 day-one releases sit unlabeled while the upper tiers label themselves.

This module drafts those two values with Claude and web search, **for a human
to verify**. It writes nothing. The job that calls it writes a review file, not
the database, because the failure mode here is not a wrong tier — it is a wrong
tier with a plausible sentence attached.

**The bias this has to fight.** Studio closures are announced; quiet
absorptions, renames and reassignments are not. A model reading the same press
coverage a person would will therefore over-report `closed`, and `closed` is a
hard override straight to Flop in the rubric. The prompt below spends most of
its length on that one asymmetry: absence of news is not evidence of closure,
and `unknown` is the correct answer far more often than the dramatic reading.

Also note the circularity risk. These drafts become labels; labels train the
model and score the rubric. If the same system that drafts them later reasons
over them unchecked, the evaluation measures self-consistency rather than
accuracy. Human source-verification is what breaks that loop, and it is the
reason this returns a draft rather than a value.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

MODEL = "claude-opus-5"

# Adaptive thinking spends this budget too, and a row that thinks hard can leave
# too little for the answer. When that happens the JSON is cut off mid-string
# and the failure reads as a parser bug rather than a budget one — which is
# exactly what Just Cause 3 did on the first live run, truncating at character
# 2701 after the longest generation of the five. `parse_response` now names the
# cause; this gives it room not to happen.
#
# The cap and the transport are one decision, not two. The SDK estimates a
# non-streaming request's duration from max_tokens and raises ValueError before
# sending anything once that passes ten minutes. For this model the highest it
# accepts non-streaming is 21,333 — measured against the installed SDK, not
# guessed — so 16000 was fine and 32000 is refused on every row:
#
#     ValueError: Streaming is required for operations that may take longer
#     than 10 minutes.
#
# Hence `draft_signals` streams. The batch path has no such check and is
# unaffected either way.
MAX_TOKENS = 32000

# Enough searches to check the studio, the publisher and the game separately,
# without letting one row run away with the budget.
MAX_SEARCHES = 8

# `response_inclusion: "excluded"` drops the raw search blocks from the response
# once dynamic filtering has consumed them. Safe here specifically: `_parse` reads
# the first text block and nothing else, so the search blocks were only ever
# billed output tokens nobody looked at. It needs `_20260318` or later.
WEB_SEARCH_TOOL: dict[str, Any] = {
    "type": "web_search_20260318",
    "name": "web_search",
    "max_uses": MAX_SEARCHES,
    "response_inclusion": "excluded",
}

# The system prompt, tool definition and schema are byte-identical on every row
# — about 830 tokens re-billed 71 times. Caching reprices that at 0.1x after the
# first write. It clears Opus 5's 512-token minimum with room; on Sonnet 5 (1024)
# or Haiku 4.5 (4096) the same prefix would silently not cache at all, which is
# worth knowing before anyone swaps the model for a cheaper one.
CACHE_CONTROL: dict[str, Any] = {"type": "ephemeral"}

# A refusal costs one draft, and every draft is reviewed by a human anyway, so
# the local degradation (record `unknown`, move on) is already safe. The
# server-side fallback is on by default per Anthropic's guidance for Opus 5;
# set this False if the request shape is rejected.
USE_SERVER_FALLBACK = True
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# How long after launch a studio or support event still counts as a consequence
# *of this launch*. Undefined until now, and every draft in the first live run
# independently asked for it: for a 2014 game, "after this launch" spans twelve
# years, and Ubisoft Montreal both grew (2015-19) and cut staff (2023-25).
# Unbounded, the 2023-25 industry contraction would drag every older row toward
# `severe_layoffs`, and that plus non-sustained support is a hard route to Flop.
#
# 16 months contains all seven dated consequences in the corpus. They fall in
# two clusters and nothing lands between them — immediate (Forspoken and
# Immortals of Aveum at ~1 month, Veilguard 2.9, Concord 5.3) and fiscal-lag
# (Redfall 12.2, Saints Row 12.3, Halo Infinite 14.1), where a publisher takes a
# quarter or two of sales, runs a review, and restructures the next year. A
# 12-month window misses that whole second cluster, including the two clearest
# studio deaths of the era: Volition closed 8 days past a year after Saints Row,
# Arkane Austin 5 days past a year after Redfall.
SIGNAL_WINDOW_MONTHS = 16

STUDIO_VALUES = ("grew", "continued", "severe_layoffs", "closed", "unknown")
SUPPORT_VALUES = ("sustained", "curtailed", "abandoned", "unknown")

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "studio_signal": {"type": "string", "enum": list(STUDIO_VALUES)},
        "support_signal": {"type": "string", "enum": list(SUPPORT_VALUES)},
        "studio_evidence": {"type": "string"},
        "support_evidence": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "alternative_reading": {"type": "string"},
        "reviewer_note": {"type": "string"},
    },
    "required": [
        "studio_signal",
        "support_signal",
        "studio_evidence",
        "support_evidence",
        "sources",
        "confidence",
        "alternative_reading",
        "reviewer_note",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = f"""\
You research what happened to a game's developer and to the game's post-launch \
support, so a human can verify your findings and label a dataset. You report \
facts with sources. You do not assign an outcome tier — a separate rule does \
that from the two values you return.

## The window: {SIGNAL_WINDOW_MONTHS} months from the Steam release date

Both signals describe consequences **of this launch**, so only events within \
{SIGNAL_WINDOW_MONTHS} months of the Steam release date you are given are \
scored. This is not a detail — for an older game, "after this launch" can span \
a decade, and a studio may have both expanded and contracted in that time.

- An event outside the window does not change either value, however dramatic. A \
studio that closed four years and two projects later did not close because of \
this launch.
- Report such events in `reviewer_note` instead, with their dates, so a human \
can see you found them and chose not to score them.
- Evidence *for* a value must also sit inside the window. Do not support `grew` \
with an expansion announced three years afterwards.
- If nothing inside the window settles a value, that value is `unknown`.

Return `studio_signal`, one of:
- `grew` — the studio hired, expanded, or opened a new team after this launch.
- `continued` — the studio kept operating with no significant reduction.
- `severe_layoffs` — a substantial, reported reduction in headcount.
- `closed` — the studio was shut down.
- `unknown` — you could not establish which of the above is true.

Return `support_signal`, one of:
- `sustained` — the announced post-launch plan ran its course.
- `curtailed` — the plan continued but was cut short of what was announced.
- `abandoned` — support stopped.
- `unknown` — you could not establish which of the above is true.

### Where the line between these actually falls

The two boundaries answer different questions, and conflating them collapses \
the scale. `sustained` against `curtailed` asks whether the **plan** ran its \
course. `curtailed` against `abandoned` asks whether support **stopped**.

**`abandoned` is about support ending, not about how much shipped before it \
ended.** A large final update, servers left running, and a game still on sale \
do not turn a termination into a curtailment. A statement that development has \
stopped, or updates simply ceasing with announced content undelivered, is \
`abandoned` however much was delivered first — Redfall shipped a substantial \
final update *after* Bethesda said development would not continue, and that is \
still support ending.

**A cancelled feature is not by itself a curtailment.** Ask whether the plan \
continued. If seasons or updates kept arriving on their announced cadence, that \
is `sustained`, and the cancelled item belongs in `reviewer_note` — Halo \
Infinite lost split-screen campaign co-op and kept shipping seasons, which is \
a delivered plan with a dropped feature, not a cut-short one.

Judge these against what was actually promised. A game with a season pass has a \
plan to fall short of; a game whose only stated commitment was patching does \
not, and you should not read a light update cadence as a curtailment when \
nothing more was ever announced.

**An announced item you cannot confirm shipped has not shipped.** `sustained` \
means the plan was delivered, so the burden is evidence of delivery, not \
evidence of cancellation. Where something was publicly announced and you can \
find no sign of it arriving, that is at least `curtailed`.

This is the inverse of the closure rule above, and the difference is where the \
event sits. There, silence must not manufacture an event that was never \
announced. Here the announcement *is* the event, and silence is the failure to \
fulfil it. Immortals of Aveum announced an Unreal Engine 5.2 upgrade in October \
2023, shipped a final patch in June 2024 that was not it, and stopped — an \
undelivered commitment, not a completed plan.

## The asymmetry you must correct for

Studio closures are announced and widely reported. Studios that were quietly \
absorbed, renamed, merged into a sibling team, or simply moved on to another \
project generate little or no coverage. If you weigh what you find by how much \
of it there is, you will systematically over-report `closed`. That single value \
forces the strongest possible conclusion downstream, so a false `closed` is the \
most expensive mistake available to you.

Rules that follow from this:
- Report `closed` **only** with a specific closure announcement — a date, and a \
source that says the studio shut down. Never infer it.

### What "shut down" covers

A studio is `closed` when it stopped existing as a development unit: shut down, \
dissolved, or merged into its parent with no continuing team identity and no \
successor studio. **Staff being retained does not make it `continued`.** A \
studio can end while its people keep their jobs; the question is whether the \
studio still exists, not whether anyone was laid off, and a press release \
describing a merger "to strengthen the group" is still describing an ending.

The absorption exception is narrower than it sounds. It covers a team that \
**persists as a working unit** — renamed, moved under a sibling, reorganised — \
and carries on developing. Apply it only when you can name what the team became \
and point to something it did afterwards. If you cannot, absorption is not what \
happened, and the studio is `closed`.

This still needs the same positive evidence as any other `closed`: a dated \
announcement that the studio was dissolved. Silence remains not evidence.
- Silence is not evidence. A studio you cannot find recent news about is \
`unknown`, not `closed`. No news most often means an ordinary studio working on \
an unannounced project.
- "Studio X had layoffs" reported about a parent publisher is not evidence about \
this studio unless the source names it.
- Distinguish the developer from the publisher. A publisher's restructuring says \
nothing about the studio that made this game unless the source connects them.
- `unknown` is a correct, useful answer. Prefer it over a guess. A human will \
research the unknowns; a plausible wrong answer may pass review unnoticed.

## Sources

Every value other than `unknown` needs at least one source URL in `sources` \
that a reviewer can open and check. If you cannot produce one, the value is \
`unknown`. Prefer the primary announcement over coverage of it.

Set `confidence` to `low` whenever you are extrapolating, whenever sources \
disagree, or whenever you found only one source. Use `reviewer_note` to say \
what you would check next, or what made this one hard.

## When you nearly chose differently

If a different value for either signal was genuinely arguable on what you \
found, put it in `alternative_reading` as the value and the reason, dated:

    support_signal=curtailed — the announced Switch version was cancelled on \
2 May 2023, and a platform was part of the post-launch roadmap

This is not the same as `reviewer_note`, which is what to check next. This is a \
value you weighed and did not pick, and it exists because a reviewer with \
dozens of rows triages by flag: a close call that would land the game in a \
different tier has to be visible without reading every note.

Leave it as an empty string when there was no real second reading. Filling it \
on every row would make it useless, so only a genuine near-miss belongs here.\
"""


class SignalDraft(BaseModel):
    """One researched draft, pending human verification. Never a label."""

    studio_signal: Literal["grew", "continued", "severe_layoffs", "closed", "unknown"]
    support_signal: Literal["sustained", "curtailed", "abandoned", "unknown"]
    studio_evidence: str
    support_evidence: str
    sources: list[str]
    confidence: Literal["high", "medium", "low"]
    alternative_reading: str
    reviewer_note: str

    @property
    def unsourced_claim(self) -> bool:
        """A non-unknown value with nothing a reviewer can open."""
        claims = self.studio_signal != "unknown" or self.support_signal != "unknown"
        return claims and not self.sources

    @property
    def review_flags(self) -> str:
        """Why a reviewer should reach this row first. Empty means no flag."""
        flags = []
        if self.studio_signal == "closed":
            # A hard override straight to Flop, and the value the media
            # asymmetry pushes towards. Never let one through unread.
            flags.append("CLOSED-verify")
        if self.unsourced_claim:
            flags.append("no-sources")
        if self.confidence == "low":
            flags.append("low-confidence")
        if self.alternative_reading.strip():
            # Marvel's Midnight Suns came back severe_layoffs/sustained and
            # unflagged, while its own note argued for curtailed — and
            # severe_layoffs plus curtailed is Flop, not Underperform. The row
            # closest to changing tier was the one triage would not surface.
            flags.append("alternative")
        return " ".join(flags)


@dataclass(slots=True)
class ResearchTarget:
    """What the researcher is told about the game. Deliberately minimal.

    No outcome, no review scores, no percentile: the question is what happened
    to the people who made it, and handing over how the launch went invites a
    story that fits the numbers rather than the sources.
    """

    game_name: str
    developer: str | None
    publisher: str | None
    steam_release_date: date | None


class MessagesClient(Protocol):
    """The slice of the Anthropic client this module uses, so tests can stub it.

    `stream`, not `create`: the SDK derives a non-streaming timeout from
    `max_tokens` and refuses outright — client-side, before any request is sent
    — when the estimate passes ten minutes. At MAX_TOKENS=32000 it does.
    Streaming is the documented pairing for a raised cap, and
    `.get_final_message()` returns the same finished message
    `parse_response` already reads.
    """

    def stream(self, **kwargs: Any) -> Any: ...


class ResearchError(RuntimeError):
    """The model returned nothing usable for this game."""


def build_prompt(target: ResearchTarget) -> str:
    released = target.steam_release_date.isoformat() if target.steam_release_date else "unknown"
    return (
        f"Game: {target.game_name}\n"
        f"Developer: {target.developer or 'not recorded'}\n"
        f"Publisher: {target.publisher or 'not recorded'}\n"
        f"Steam release date: {released}\n\n"
        "Research what happened to the developer after this release, and what "
        "happened to the game's post-launch support. Return the two signals with "
        "sources a reviewer can open."
    )


def request_kwargs(target: ResearchTarget) -> dict[str, Any]:
    """The request for one game, shared by the live and batch paths.

    One builder deliberately: a batch that differs from the request it was smoke
    -tested with is a batch whose results answer a different question. The beta
    batch request type accepts `betas` and `fallbacks` inside each request's
    params, so this dict maps into a batch entry unchanged.
    """
    kwargs: dict[str, Any] = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "thinking": {"type": "adaptive"},
        "tools": [WEB_SEARCH_TOOL],
        "output_config": {"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}},
        "cache_control": CACHE_CONTROL,
        "messages": [{"role": "user", "content": build_prompt(target)}],
    }
    if USE_SERVER_FALLBACK:
        kwargs["betas"] = [FALLBACK_BETA]
        kwargs["fallbacks"] = "default"
    return kwargs


def parse_response(response: Any, game_name: str) -> SignalDraft:
    """Read one finished message, from either path. Never invents a value."""
    stop = getattr(response, "stop_reason", None)
    if stop == "refusal":
        raise ResearchError(f"{game_name}: the request was declined")
    if stop == "pause_turn":
        # A long web-search turn can pause. Nothing partial is trustworthy here,
        # so surface it rather than parsing half an answer. In a batch there is
        # no way to continue a paused turn, so the row is simply re-run.
        raise ResearchError(f"{game_name}: turn paused before completing")
    if stop == "max_tokens":
        # Structured output guarantees schema-valid JSON only if generation
        # finishes. Cut it off and the text is a truncated string, which the
        # parser reports as malformed — blaming the wrong thing entirely.
        raise ResearchError(
            f"{game_name}: hit the {MAX_TOKENS}-token cap before finishing the "
            "answer; thinking spends the same budget, so raise MAX_TOKENS or "
            "lower output_config.effort"
        )

    text = next(
        (block.text for block in response.content if getattr(block, "type", None) == "text"),
        None,
    )
    if not text:
        raise ResearchError(f"{game_name}: no text block in the response")

    try:
        return SignalDraft.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        # Carry the stop reason: the first unparseable row in production was a
        # truncation, and the message named neither the cause nor where to look.
        raise ResearchError(
            f"{game_name}: unparseable response (stop_reason={stop}) — {exc}"
        ) from exc


def draft_signals(client: MessagesClient, target: ResearchTarget) -> SignalDraft:
    """Research one game now. Raises ResearchError rather than inventing a value."""
    with client.stream(**request_kwargs(target)) as stream:
        response = stream.get_final_message()
    return parse_response(response, target.game_name)


# --- the batch path ---------------------------------------------------------
#
# Nobody waits on a 71-row backfill, and the Batches API takes 50% off every
# token including cache reads and writes. The per-search fee is *not*
# discounted, so the saving is real but bounded — at four searches a row the
# fee is a majority of what a cheap model would cost and a third of Opus 5's.
#
# Server tools are what make this possible. A batch request is single-shot, so
# a *client* tool loop cannot run inside one; web search runs server-side
# within the single turn, and the Batches API supports it explicitly at the
# same price. Nothing about the request shape changes — `request_kwargs` is
# shared, so a batch cannot drift from what was smoke-tested live.


@dataclass(slots=True)
class BatchOutcome:
    """One row's result, matched back to its key. Exactly one of draft/error."""

    key: str
    draft: SignalDraft | None = None
    error: str | None = None


class BatchesClient(Protocol):
    """The slice of `client.beta.messages.batches` this module uses."""

    def create(self, **kwargs: Any) -> Any: ...
    def retrieve(self, batch_id: str, /) -> Any: ...
    def results(self, batch_id: str, /) -> Iterable[Any]: ...


def batch_requests(targets: Iterable[tuple[str, ResearchTarget]]) -> list[dict[str, Any]]:
    """The queue as batch entries, keyed so results can be matched back.

    Results come back in any order, so `custom_id` is the only way to know
    which game a row belongs to — never position.
    """
    return [{"custom_id": key, "params": request_kwargs(target)} for key, target in targets]


def submit_batch(batches: BatchesClient, targets: Iterable[tuple[str, ResearchTarget]]) -> str:
    """Queue the whole run and return the batch id. Does not wait."""
    requests = batch_requests(targets)
    if not requests:
        raise ResearchError("nothing to submit")
    # The beta goes in both places on purpose: at the batch level it sets the
    # anthropic-beta header that authorises the field, and inside each request's
    # params it is what actually applies the fallback to that message.
    batch = batches.create(requests=requests, betas=[FALLBACK_BETA])
    return batch.id


def collect_batch(
    batches: BatchesClient, batch_id: str, names: dict[str, str]
) -> list[BatchOutcome]:
    """Read a finished batch. Every entry comes back, failures included.

    A row that errored, expired or was cancelled is reported rather than
    dropped: the queue is the unit of work, and a silently missing row reads as
    a game nobody needed to research.
    """
    outcomes: list[BatchOutcome] = []
    for result in batches.results(batch_id):
        key = result.custom_id
        name = names.get(key, key)
        kind = getattr(result.result, "type", None)
        if kind != "succeeded":
            detail = getattr(getattr(result.result, "error", None), "type", kind)
            outcomes.append(BatchOutcome(key=key, error=f"{name}: batch entry {kind} ({detail})"))
            continue
        try:
            outcomes.append(
                BatchOutcome(key=key, draft=parse_response(result.result.message, name))
            )
        except ResearchError as exc:
            outcomes.append(BatchOutcome(key=key, error=str(exc)))
    return outcomes


# --- measuring the researcher against known answers -------------------------
#
# Every documented closure in the corpus is already labelled, so the research
# queue cannot contain one: it is unlabelled rows by construction. Two live runs
# produced zero `closed` values and there was no way to tell correct restraint
# from over-correction. The only ground truth available is the labelled set.
#
# This is measurement, not tuning. A poor result is a named reasoning error in a
# named row, to be fixed as such -- not a number to adjust wording against until
# it climbs. And a disagreement may be the label rather than the draft: some
# were curated early, and a draft that contradicts one with better sources is a
# finding about the corpus.

BENIGN_STUDIO = frozenset({"grew", "continued"})
NEGATIVE_STUDIO = frozenset({"severe_layoffs", "closed"})


@dataclass(slots=True)
class SignalComparison:
    """One drafted row set against its curated answer.

    The tiers are supplied by the caller rather than computed here: they come
    from the real rubric, and this module has no business importing it.
    """

    key: str
    game_name: str
    curated_studio: str
    curated_support: str
    draft: SignalDraft
    drafted_tier: str = ""
    curated_tier: str = ""

    @property
    def tier_agrees(self) -> bool:
        return bool(self.drafted_tier) and self.drafted_tier == self.curated_tier

    @property
    def tier_direction(self) -> str:
        """Which way a tier disagreement went — the number that actually matters.

        Per-signal agreement can look fine while the label lands somewhere else
        entirely. The first five-row run agreed on studio 5/5 and still moved
        two rows: `severe_layoffs` reads as Underperform beside `sustained` and
        as Flop beside anything else, and `abandoned` is a hard override on its
        own. Softer is the expensive direction — it buries a Flop.
        """
        if not self.drafted_tier or not self.curated_tier:
            return "unscored"
        if self.drafted_tier == self.curated_tier:
            return "agrees"
        return "softer" if self.curated_tier == "flop" else "harsher"

    @property
    def studio_agrees(self) -> bool:
        return self.draft.studio_signal == self.curated_studio

    @property
    def support_agrees(self) -> bool:
        return self.draft.support_signal == self.curated_support

    @property
    def verdict(self) -> str:
        """Which *direction* the studio signal missed in, not merely whether.

        A single accuracy figure hides the thing that matters. Reporting a
        studio as fine when it was gutted buries a Flop as an Underperform;
        reporting the reverse is the failure the prompt was written against.
        They are not interchangeable and must not average together.
        """
        drafted, curated = self.draft.studio_signal, self.curated_studio
        if drafted == curated:
            return "agrees"
        if drafted == "unknown":
            # An honest refusal, not a wrong answer. Counted apart because too
            # many of them means the window is starving the search.
            return "refused"
        if curated in NEGATIVE_STUDIO and drafted in BENIGN_STUDIO:
            return "false_benign"
        if curated in BENIGN_STUDIO and drafted in NEGATIVE_STUDIO:
            return "false_alarm"
        return "differs"


def compare_to_curated(
    key: str,
    game_name: str,
    curated_studio: str,
    curated_support: str,
    draft: SignalDraft,
    drafted_tier: str = "",
    curated_tier: str = "",
) -> SignalComparison:
    return SignalComparison(
        key=key,
        game_name=game_name,
        curated_studio=curated_studio,
        curated_support=curated_support,
        draft=draft,
        drafted_tier=drafted_tier,
        curated_tier=curated_tier,
    )


def summarise_comparisons(rows: list[SignalComparison]) -> dict[str, int]:
    """Counts a reviewer can act on, keyed by what each one means."""
    tally = {
        "compared": len(rows),
        "studio_agrees": sum(1 for r in rows if r.studio_agrees),
        "support_agrees": sum(1 for r in rows if r.support_agrees),
        "both_agree": sum(1 for r in rows if r.studio_agrees and r.support_agrees),
        "false_benign": 0,
        "false_alarm": 0,
        "refused": 0,
        "differs": 0,
        "tier_agrees": sum(1 for r in rows if r.tier_agrees),
        "tier_softer": sum(1 for r in rows if r.tier_direction == "softer"),
        "tier_harsher": sum(1 for r in rows if r.tier_direction == "harsher"),
        "flagged_alternative": sum(1 for r in rows if r.draft.alternative_reading.strip()),
    }
    for row in rows:
        verdict = row.verdict
        if verdict in tally and verdict != "agrees":
            tally[verdict] += 1
    return tally
