import pytest

from freqtrade.rpc.journal import normalize_journal


def test_normalize_journal_trims_and_dedupes():
    assert normalize_journal("  hi  ", ["FOMO", "fomo ", " late", ""]) == ("hi", ["FOMO", "late"])


def test_normalize_journal_accepts_persian_at_limit():
    note = "ی" * 2000
    assert normalize_journal(note, []) == (note, [])


@pytest.mark.parametrize(
    "note,tags",
    [
        ("x" * 2001, []),
        ("", [f"t{i}" for i in range(11)]),
        ("", ["x" * 31]),
    ],
)
def test_normalize_journal_limits(note, tags):
    with pytest.raises(ValueError):
        normalize_journal(note, tags)
