"""Serve a forecast from the trained model, when there is one to serve.

Everything in this module is written around a single fact: **most of the
time there is no model.** The training job refuses to write an artifact that
has not beaten a constant guess, and at the current corpus size that refusal
is the expected outcome. So `forecast()` returning None is the normal path,
not the error path, and the endpoint falls back to the rule-based baseline
and says which one produced the number.

Three separate things can be missing, and none of them is a failure:

- **The libraries.** scikit-learn and joblib are an optional backend extra
  (`pip install -e './backend[ml]'`). A deployment that only serves the
  dashboard does not need the training stack installed.
- **The artifact.** No file means no model cleared the bar, or this is a
  backend-only deployment that does not ship `/ml`.
- **The features.** A game with no announced release date, an unknown
  publisher, and no listed price is three-quarters imputed. The forecast is
  still produced, but it carries the list of what was assumed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.models import Game, Outcome

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.ordinal import Artifact

logger = logging.getLogger(__name__)

# Cached because loading unpickles a set of gradient-boosted trees, and the
# artifact only changes when the biweekly job rewrites it. `reset_cache()`
# exists for tests and for a process that wants to pick up a fresh model
# without a restart.
_cache: tuple[Path | None, Artifact | None] | None = None


@dataclass(slots=True)
class ModelForecast:
    """A forecast from the trained model, with its provenance attached."""

    probabilities: dict[Outcome, float]
    predicted: Outcome
    confidence: str
    rationale: str
    basis: list[str]
    method: str


def reset_cache() -> None:
    global _cache
    _cache = None


def get_artifact(path: Path | None = None) -> Artifact | None:
    """Load the trained model, or None when there isn't one available."""
    global _cache
    if _cache is not None and _cache[0] == path:
        return _cache[1]

    artifact: Artifact | None = None
    try:
        from app.ordinal import load

        artifact = load(path) if path is not None else load()
    except ImportError:
        # numpy / scikit-learn / joblib not installed. Expected on a
        # dashboard-only deployment; the baseline covers it.
        logger.debug("model stack unavailable; serving the rule-based baseline")
    except Exception:  # pragma: no cover - corrupt or version-skewed artifact
        # A model saved by a different scikit-learn can fail to unpickle. That
        # is a reason to serve the baseline, not to fail the request.
        logger.warning("stored model could not be loaded; falling back to the baseline")

    _cache = (path, artifact)
    return artifact


def _confidence(artifact: Artifact, imputed: list[str], probability: float) -> str:
    """How much weight the dashboard should put on this number.

    Deliberately pessimistic. The model only exists at all because it beat a
    constant guess on a corpus of a few dozen games; "high" is not a level
    this project has earned yet, and a game whose features are mostly assumed
    should read lower still.
    """
    if imputed or artifact.n_rows < 60:
        return "low"
    return "medium" if probability < 0.55 else "high"


def _rationale(artifact: Artifact, predicted: Outcome, imputed: list[str]) -> str:
    parts = [
        f"Trained on {artifact.n_rows} resolved day-one Steam releases, "
        f"held out {artifact.cv_accuracy:.0%} accurate against "
        f"{artifact.constant_accuracy:.0%} for always guessing the most common tier."
    ]
    if imputed:
        parts.append(
            f"{len(imputed)} of {len(artifact.feature_names)} inputs were not available "
            "for this game and were filled with defaults, so this forecast rests on "
            "less evidence than the training figures suggest."
        )
    return " ".join(parts)


def forecast(session: Session, game: Game, path: Path | None = None) -> ModelForecast | None:
    """Predict a tier for one tracked game, or None when no model is available."""
    artifact = get_artifact(path)
    if artifact is None:
        return None

    from app.features import build_live_features
    from app.ordinal import RANK_TO_TIER

    live = build_live_features(session, game)
    if tuple(artifact.feature_names) != tuple(_feature_names()):
        # The feature set changed since the model was fitted. Serving the old
        # model against the new vector would silently misalign the columns.
        logger.warning("stored model was fitted on a different feature set; using the baseline")
        return None

    probabilities = artifact.model.predict_proba([live.values])[0]
    ranked = {RANK_TO_TIER[i]: round(float(p), 4) for i, p in enumerate(probabilities)}
    predicted = max(ranked, key=ranked.get)  # type: ignore[arg-type]

    basis = [
        f"Model {artifact.version}, fitted {artifact.trained_at:%Y-%m-%d} "
        f"on {artifact.n_rows} labeled releases.",
        f"Mean ordinal distance {artifact.cv_distance:.2f} tiers, against "
        f"{artifact.constant_distance:.2f} for a constant guess.",
    ]
    if live.imputed:
        basis.append("Assumed, not observed: " + ", ".join(live.imputed) + ".")

    return ModelForecast(
        probabilities=ranked,
        predicted=predicted,
        confidence=_confidence(artifact, live.imputed, ranked[predicted]),
        rationale=_rationale(artifact, predicted, live.imputed),
        basis=basis,
        method=artifact.method,
    )


def _feature_names() -> tuple[str, ...]:
    from app.features import FEATURE_NAMES

    return FEATURE_NAMES
