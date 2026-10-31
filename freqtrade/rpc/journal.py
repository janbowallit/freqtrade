"""
Trade journal: notes and tags the user writes on a trade, stored as trade custom data.
"""

JOURNAL_NOTE_KEY = "journal_note"
JOURNAL_TAGS_KEY = "journal_tags"
JOURNAL_KEYS = (JOURNAL_NOTE_KEY, JOURNAL_TAGS_KEY)

MAX_NOTE_LEN = 2000
MAX_TAGS = 10
MAX_TAG_LEN = 30


def normalize_journal(note: str, tags: list[str]) -> tuple[str, list[str]]:
    """
    Clean up a journal entry and check its limits.
    Strips the note, trims tags, drops empty tags and case-insensitive duplicates
    (keeping the first spelling).
    :raises ValueError: when the note or the tags break a limit
    """
    note = note.strip()
    if len(note) > MAX_NOTE_LEN:
        raise ValueError(f"Note is longer than {MAX_NOTE_LEN} characters.")

    clean_tags: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        tag = tag.strip()
        if not tag or tag.casefold() in seen:
            continue
        if len(tag) > MAX_TAG_LEN:
            raise ValueError(f"Tag '{tag[:MAX_TAG_LEN]}…' is longer than {MAX_TAG_LEN} characters.")
        seen.add(tag.casefold())
        clean_tags.append(tag)

    if len(clean_tags) > MAX_TAGS:
        raise ValueError(f"A trade can have at most {MAX_TAGS} tags.")
    return note, clean_tags
