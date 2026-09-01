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
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

MODEL = "claude-opus-5"
MAX_TOKENS = 16000

# Enough searches to check the studio, the publisher and the game separately,
# without letting one row run away with the budget.
MAX_SEARCHES = 8

WEB_SEARCH_TOOL: dict[str, Any] = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": MAX_SEARCHES,
}

# A refusal costs one draft, and every draft is reviewed by a human anyway, so
# the local degradation (record `unknown`, move on) is already safe. The
# server-side fallback is on by default per Anthropic's guidance for Opus 5;
# set this False if the request shape is rejected.
USE_SERVER_FALLBACK = True
FALLBACK_BETA = "server-side-fallback-2026-07-01"

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
        "reviewer_note": {"type": "string"},
    },
    "required": [
        "studio_signal",
        "support_signal",
        "studio_evidence",
        "support_evidence",
        "sources",
        "confidence",
        "reviewer_note",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You research what happened to a game's developer and to the game's post-launch \
support, so a human can verify your findings and label a dataset. You report \
facts with sources. You do not assign an outcome tier — a separate rule does \
that from the two values you return.

Return `studio_signal`, one of:
- `grew` — the studio hired, expanded, or opened a new team after this launch.
- `continued` — the studio kept operating with no significant reduction.
- `severe_layoffs` — a substantial, reported reduction in headcount.
- `closed` — the studio was shut down.
- `unknown` — you could not establish which of the above is true.

Return `support_signal`, one of:
- `sustained` — the announced post-launch plan was delivered.
- `curtailed` — support continued but was cut short of what was announced.
- `abandoned` — support ended early, or the game was delisted.
- `unknown` — you could not establish which of the above is true.

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
what you would check next, or what made this one hard.\
"""


class SignalDraft(BaseModel):
    """One researched draft, pending human verification. Never a label."""

    studio_signal: Literal["grew", "continued", "severe_layoffs", "closed", "unknown"]
    support_signal: Literal["sustained", "curtailed", "abandoned", "unknown"]
    studio_evidence: str
    support_evidence: str
    sources: list[str]
    confidence: Literal["high", "medium", "low"]
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
    """The slice of the Anthropic client this module uses, so tests can stub it."""

    def create(self, **kwargs: Any) -> Any: ...


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


def _request_kwargs(target: ResearchTarget) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "thinking": {"type": "adaptive"},
        "tools": [WEB_SEARCH_TOOL],
        "output_config": {"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}},
        "messages": [{"role": "user", "content": build_prompt(target)}],
    }
    if USE_SERVER_FALLBACK:
        kwargs["betas"] = [FALLBACK_BETA]
        kwargs["fallbacks"] = "default"
    return kwargs


def draft_signals(client: MessagesClient, target: ResearchTarget) -> SignalDraft:
    """Research one game. Raises ResearchError rather than inventing a value."""
    response = client.create(**_request_kwargs(target))

    stop = getattr(response, "stop_reason", None)
    if stop == "refusal":
        raise ResearchError(f"{target.game_name}: the request was declined")
    if stop == "pause_turn":
        # A long web-search turn can pause. Nothing partial is trustworthy here,
        # so surface it rather than parsing half an answer.
        raise ResearchError(f"{target.game_name}: turn paused before completing")

    text = next(
        (block.text for block in response.content if getattr(block, "type", None) == "text"),
        None,
    )
    if not text:
        raise ResearchError(f"{target.game_name}: no text block in the response")

    try:
        return SignalDraft.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ResearchError(f"{target.game_name}: unparseable response — {exc}") from exc
