"""Tests for company name normalization."""

from __future__ import annotations

import pytest

from app.companies import normalize, normalize_all


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Territory qualifiers are the same company.
        ("SEGA (Japan)", "SEGA"),
        ("Activision (Excluding Japan and Asia)", "Activision"),
        ("PlayStation Publishing LLC (excluding China)", "PlayStation Publishing"),
        # Corporate suffixes carry no signal.
        ("CAPCOM Co., Ltd.", "CAPCOM"),
        ("FromSoftware, Inc.", "FromSoftware"),
        ("Blizzard Entertainment, Inc.", "Blizzard Entertainment"),
        # Renames over the corpus's time span.
        ("Warner Bros. Interactive Entertainment", "Warner Bros. Games"),
        ("WB Games", "Warner Bros. Games"),
        ("Ubisoft Entertainment", "Ubisoft"),
        # Already clean.
        ("Larian Studios", "Larian Studios"),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["Feral Interactive (Mac)", "Feral Interactive (Linux)", "Aspyr (Linux)", "Nixxes Software"],
)
def test_porting_houses_are_dropped(raw):
    # They hold the rights to a Mac or Linux build; they neither fund
    # development nor set a budget tier. Counting them as publishers would
    # invent a company whose catalog is other studios' ports.
    assert normalize(raw) is None


def test_blank_and_qualifier_only_names_are_dropped():
    assert normalize("") is None
    assert normalize("   ") is None
    assert normalize("(Japan)") is None


def test_normalize_all_splits_and_deduplicates():
    # Steam lists rights-holders per territory, so the same company can
    # appear twice in one field.
    assert normalize_all("SEGA\nSEGA (Japan)\nFeral Interactive (Mac)") == ["SEGA"]


def test_normalize_all_handles_missing_field():
    assert normalize_all(None) == []
    assert normalize_all("") == []
