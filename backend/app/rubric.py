"""The outcome rubric, as code.

Turns a launched game's observed signals into one of the four ordered tiers,
with the reasoning attached. This is the Phase 1 artifact the spec asks for:
an explicit rubric that can be checked against hand labels before any model
is trained, and later the thing the resolution job uses to settle a
"Failed to Meet Expectations" row.

The procedure runs on two axes, because one is not enough:

1. **Did it meet expectations?** Cohort-normalized launch-window review
   volume, with review sentiment as a secondary check. Volume is normalized
   because raw counts are not comparable across years.
2. **If it fell short, how far?** Studio fate and post-launch support. The
   spec is explicit that launch-window numbers cannot separate Flop from
   Underperform, and the data agrees — Concord sits at 65.8% positive, above
   Forspoken and far above Redfall.

Note on what this can and cannot validate: axis 1 is falsifiable against the
hand labels, since nothing about a cohort percentile encodes the labeler's
judgment. Axis 2 is partly circular — the same studio-outcome evidence
informs both the rule and the label — so agreement there measures whether the
rule is *stated* correctly, not whether it is *right*.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models import Outcome, StudioSignal, SupportSignal

# --- Thresholds -----------------------------------------------------------
# Volume figures are cohort percentiles, never absolute counts: Steam's
# baseline keeps growing, so an absolute bar goes stale (PROJECT_SPEC, Cohort
# Normalization).
#
# These were fit against 31 hand-labeled day-one releases. That is a small
# set, and the thresholds were chosen after seeing it, so agreement measured
# on the same games is in-sample and optimistic. Treat the *structure* as the
# finding, not the exact numbers.

# Launch sentiment turned out to be the strongest single separator: on the
# labeled set, almost every release that met expectations cleared this and
# almost none that missed did.
SENTIMENT_BAR = 78.0

# A release can review well and still miss, if barely anyone showed up
# (Hellblade II: 88% positive, 18th-percentile volume).
VOLUME_FLOOR = 35.0

# Reviews accrued by three months, as a multiple of the first two weeks.
# Separates a launch spike that held from one that collapsed.
RETENTION_STRONG = 2.5
RETENTION_SUSTAINED = 2.0

# A launch can under-review and still have met expectations if it kept
# pulling players in — Helldivers 2 opened at 75% amid server failures and
# accrued nearly 6x its launch reviews by three months.
MOMENTUM_VOLUME = 85.0

# Breakout gates.
BREAKOUT_VOLUME = 90.0
BREAKOUT_VOLUME_WITH_MOMENTUM = 80.0


@dataclass(slots=True)
class RubricInput:
    """Everything the rubric looks at for one game."""

    volume_percentile: float | None
    positive_pct: float | None
    # Reviews at three months divided by reviews at two weeks. None when the
    # three-month window has not elapsed yet.
    retention_ratio: float | None = None
    studio_signal: StudioSignal = StudioSignal.UNKNOWN
    support_signal: SupportSignal = SupportSignal.UNKNOWN
    cohort_reliable: bool = True


@dataclass(slots=True)
class RubricResult:
    """A tier call, or an explicit refusal to make one."""

    outcome: Outcome | None
    confidence: str
    reasons: list[str] = field(default_factory=list)
    unresolved_reason: str | None = None

    @property
    def resolved(self) -> bool:
        return self.outcome is not None


def classify(signals: RubricInput) -> RubricResult:
    """Apply the rubric. Returns an unresolved result rather than guessing."""
    reasons: list[str] = []

    if signals.volume_percentile is None:
        return RubricResult(
            outcome=None,
            confidence="none",
            unresolved_reason=(
                "no cohort-normalized launch volume available"
                if signals.cohort_reliable
                else "cohort too small to rank this release against"
            ),
        )

    if _met_expectations(signals, reasons):
        return _resolve_upper(signals, reasons)
    return _resolve_lower(signals, reasons)


def _met_expectations(signals: RubricInput, reasons: list[str]) -> bool:
    """Did this release meet the expectations of its budget tier?

    Volume alone says no: it measures attention, not success. Battlefield
    2042 opened in the 89th percentile of its cohort at 34% positive. So the
    primary test is sentiment, with a volume floor to catch well-reviewed
    releases nobody bought, and a momentum rescue for launches that recovered.
    """
    pct = signals.volume_percentile
    sentiment = signals.positive_pct
    retention = signals.retention_ratio

    if (
        retention is not None
        and retention >= RETENTION_STRONG
        and pct is not None
        and pct >= MOMENTUM_VOLUME
    ):
        reasons.append(
            f"accrued {retention:.1f}x its launch-window reviews by three months "
            f"from {pct:.0f}th-percentile volume — a launch that kept growing"
        )
        return True

    if sentiment is None:
        reasons.append("no launch-window review sentiment available")
        return pct is not None and pct >= MOMENTUM_VOLUME

    if sentiment < SENTIMENT_BAR:
        reasons.append(
            f"{sentiment:.0f}% positive over the launch window, below the {SENTIMENT_BAR:.0f}% bar"
        )
        return False

    if pct is not None and pct < VOLUME_FLOOR:
        reasons.append(
            f"reviewed well ({sentiment:.0f}% positive) but drew "
            f"{pct:.0f}th-percentile volume for its cohort, under the "
            f"{VOLUME_FLOOR:.0f}th-percentile floor"
        )
        return False

    reasons.append(
        f"{sentiment:.0f}% positive on {pct:.0f}th-percentile volume for its cohort"
        if pct is not None
        else f"{sentiment:.0f}% positive over the launch window"
    )
    return True


def _resolve_upper(signals: RubricInput, reasons: list[str]) -> RubricResult:
    """Separate Breakout from Success among releases that met expectations."""
    pct = signals.volume_percentile
    retention = signals.retention_ratio

    if pct is not None and pct >= BREAKOUT_VOLUME:
        reasons.append(f"{pct:.0f}th-percentile launch volume, top of its release cohort")
        return RubricResult(Outcome.BREAKOUT, _confidence(signals, strong=True), reasons)

    if (
        pct is not None
        and pct >= BREAKOUT_VOLUME_WITH_MOMENTUM
        and retention is not None
        and retention >= RETENTION_SUSTAINED
    ):
        reasons.append(
            f"high launch volume ({pct:.0f}th percentile) that kept compounding "
            f"({retention:.1f}x reviews by three months)"
        )
        return RubricResult(Outcome.BREAKOUT, _confidence(signals, strong=True), reasons)

    reasons.append("cleared its cohort's bar without reaching breakout volume")
    return RubricResult(Outcome.SUCCESS, _confidence(signals, strong=False), reasons)


def _resolve_lower(signals: RubricInput, reasons: list[str]) -> RubricResult:
    """Separate Flop from Underperform using studio fate and support.

    Refuses to call it when neither signal is known — which is exactly the
    "Failed to Meet Expectations" state: the release clearly fell short, but
    the evidence that would settle how far has not arrived yet.
    """
    studio = signals.studio_signal
    support = signals.support_signal

    if studio is StudioSignal.UNKNOWN and support is SupportSignal.UNKNOWN:
        return RubricResult(
            outcome=None,
            confidence="none",
            reasons=reasons,
            unresolved_reason=(
                "fell short of its cohort, but no studio-outcome or post-launch "
                "support signal is available to separate Flop from Underperform"
            ),
        )

    if studio is StudioSignal.CLOSED:
        reasons.append("studio closed after launch")
        return RubricResult(Outcome.FLOP, "high", reasons)

    if support is SupportSignal.ABANDONED:
        reasons.append("game abandoned post-launch (delisted, or support ended early)")
        return RubricResult(Outcome.FLOP, "high", reasons)

    if studio is StudioSignal.SEVERE_LAYOFFS and support is not SupportSignal.SUSTAINED:
        reasons.append("severe layoffs at the studio and post-launch support cut short")
        return RubricResult(Outcome.FLOP, "medium", reasons)

    if studio is StudioSignal.SEVERE_LAYOFFS:
        reasons.append("severe layoffs, but the studio delivered its planned support")
        return RubricResult(Outcome.UNDERPERFORM, "medium", reasons)

    reasons.append(_survived_reason(studio, support))
    return RubricResult(Outcome.UNDERPERFORM, "high", reasons)


# Studio phrasing for the branch below. GREW and CONTINUED are the only values
# that reach it: CLOSED and SEVERE_LAYOFFS are handled above, and UNKNOWN falls
# through to the default.
_STUDIO_PHRASE = {
    StudioSignal.GREW: "the studio grew",
    StudioSignal.CONTINUED: "the studio continued operating",
}

_SUPPORT_PHRASE = {
    SupportSignal.SUSTAINED: "the game kept being supported",
    SupportSignal.CURTAILED: "post-launch support was curtailed",
}


def _survived_reason(studio: StudioSignal, support: SupportSignal) -> str:
    """Describe the signals that put a shortfall at Underperform rather than Flop.

    This is the fall-through branch, so it covers every combination the rules
    above did not claim — including `CURTAILED` support, which only decides an
    outcome when paired with severe layoffs. The sentence therefore has to read
    the signals rather than assert the common case: it previously said the game
    "kept being supported" for curtailed rows too, which the data contradicts.
    """
    return (
        f"{_STUDIO_PHRASE.get(studio, 'no studio-outcome signal')} and "
        f"{_SUPPORT_PHRASE.get(support, 'no post-launch support signal')}"
    )


def _confidence(signals: RubricInput, *, strong: bool) -> str:
    """Confidence in an at-or-above-expectation call.

    Downgraded when the post-launch picture is unknown, since a title can
    clear its launch bar and still be walked away from weeks later.
    """
    if signals.support_signal is SupportSignal.UNKNOWN:
        return "low"
    if signals.support_signal is SupportSignal.ABANDONED:
        return "low"
    return "high" if strong else "medium"
