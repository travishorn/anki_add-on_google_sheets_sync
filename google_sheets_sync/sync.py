"""Reading notes out of Anki and turning them into sheet rows.

This module only touches the collection, so everything here runs on the
collection executor (``uses_collection=True``). The network side lives in
``sheets.py`` and must be scheduled separately with ``uses_collection=False``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from anki.collection import AddNoteRequest, Collection
from anki.decks import DeckId
from anki.notes import Note

#: Columns written before the note type's own fields.
META_COLUMNS = ["guid", "notetype", "deck"]


class SyncError(RuntimeError):
    """Raised when notes cannot be read from the collection."""


@dataclass
class NoteSnapshot:
    """A flat, immutable view of one note type, safe to pass between threads."""

    note_type_id: int
    note_type_name: str
    field_names: list[str]
    rows: list[list[str]]

    @property
    def header(self) -> list[str]:
        return [*META_COLUMNS, *self.field_names, "tags"]


def build_note_query(note_type_id: int) -> str:
    """Return a search string matching every note of one note type.

    Using ``mid`` (the notetype id) rather than the display name means the
    query keeps working after the user renames the note type.
    """
    return f"mid:{note_type_id}"


def get_field_names(col: Collection, note_type_id: int) -> list[str]:
    """Return the field names of a note type, in display order.

    ``col.models`` is keyed by name rather than id, so the id is resolved to a
    name first via ``all_names_and_ids()``. Note that each entry is a protobuf
    ``NotetypeNameId`` message with ``.id`` / ``.name`` fields, not a tuple.
    """
    for entry in col.models.all_names_and_ids():
        if entry.id == note_type_id:
            notetype = col.models.by_name(entry.name)
            if notetype is None:
                raise SyncError("That note type no longer exists in this collection.")
            fields = notetype.get("flds", []) or []
            return [str(field.get("name", "")) for field in fields]
    raise SyncError("That note type no longer exists in this collection.")


def deck_names_for_note(col: Collection, note: Note) -> list[str]:
    """Return the names of every deck this note's cards live in.

    A single note can generate cards in several decks (or none, if it is a
    cloze note with no cards yet), so the result is a set of full deck names.
    """
    names: list[str] = []
    for card in note.cards():
        name = col.decks.name(card.did)
        if name and name not in names:
            names.append(name)
    return names


def snapshot_notes(col: Collection, note_type_id: int) -> NoteSnapshot:
    """Read every note of one note type into rows.

    Fields are written raw, so any HTML stored in a note field is preserved
    exactly rather than being stripped to display text.
    """
    note_type_name = _note_type_name(col, note_type_id)
    field_names = get_field_names(col, note_type_id)
    field_count = len(field_names)

    rows: list[list[str]] = []
    for note_id in col.find_notes(build_note_query(note_type_id)):
        note = col.get_note(note_id)
        if note is None:
            continue
        # Guard against a field-count mismatch (e.g. the note type changed shape).
        fields = list(note.fields)[:field_count]
        fields += [""] * (field_count - len(fields))
        rows.append(
            [
                note.guid,
                note_type_name,
                ", ".join(deck_names_for_note(col, note)),
                *fields,
                " ".join(note.tags),
            ]
        )

    return NoteSnapshot(
        note_type_id=note_type_id,
        note_type_name=note_type_name,
        field_names=field_names,
        rows=rows,
    )


def _note_type_name(col: Collection, note_type_id: int) -> str:
    for entry in col.models.all_names_and_ids():
        if entry.id == note_type_id:
            return entry.name
    raise SyncError("That note type no longer exists in this collection.")


@dataclass
class PullResult:
    """Outcome of applying sheet rows back to the collection.

    ``created_guids`` maps a zero-based *data row index* (i.e. excluding the
    header) to the guid Anki assigned, so the caller can write the real guid
    back into the sheet. Using the row index rather than the sheet row number
    keeps the caller independent of the A1 range's starting row.
    """

    updated: int = 0
    created: int = 0
    skipped: int = 0
    created_guids: dict[int, str] = field(default_factory=dict)


def _resolve_deck_id(col: Collection, deck_names: str) -> DeckId:
    """Return the deck id for a row's ``deck`` cell, defaulting to Default.

    The cell may hold a comma-separated list, because one Anki note can have
    cards in several decks. Only the first entry is used when *creating* a note,
    since a new note is placed in exactly one deck. ``decks.id`` creates the
    deck if it does not exist, which lets a sheet drive deck creation.
    """
    first = deck_names.split(",")[0].strip() if deck_names.strip() else ""
    deck_id = col.decks.id(first) if first else None
    if deck_id is None:
        deck_id = col.decks.id("Default")
    assert deck_id is not None  # "Default" always exists
    return deck_id


def apply_rows_to_notes(
    col: Collection,
    snapshot: NoteSnapshot,
    rows: list[list[str]],
) -> PullResult:
    """Apply sheet rows onto the collection: update matches, create the rest.

    Matching is by ``guid``. A row whose guid is not recognised — an empty
    cell, or one the user typed in — is treated as a **new** note rather than
    being skipped, which is what makes the sheet a genuine two-way source.
    The guid Anki assigns is recorded in ``PullResult.created_guids`` so the
    caller can write it back into the sheet, keeping subsequent pulls able to
    match the row.

    Updates are written with one batched ``update_notes`` call; creations with
    one batched ``add_notes`` call.
    """
    result = PullResult()
    header = snapshot.header

    guid_index = header.index("guid")
    deck_index = header.index("deck")
    tags_index = header.index("tags")
    first_field = len(META_COLUMNS)
    field_count = len(snapshot.field_names)

    by_guid: dict[str, Note] = {}
    for note_id in col.find_notes(build_note_query(snapshot.note_type_id)):
        existing = col.get_note(note_id)
        if existing is not None:
            by_guid[existing.guid] = existing

    notetype = col.models.by_name(snapshot.note_type_name)
    if notetype is None:
        raise SyncError("That note type no longer exists in this collection.")

    dirty: list[Note] = []
    to_add: list[AddNoteRequest] = []

    for offset, row in enumerate(rows[1:]):  # row 0 is the header
        if not row:
            result.skipped += 1
            continue

        values = list(row[first_field : first_field + field_count])
        if len(values) < field_count:
            values += [""] * (field_count - len(values))

        tags = _row_cell(row, tags_index)
        guid = _row_cell(row, guid_index).strip()
        note = by_guid.get(guid) if guid else None

        if note is not None:
            changed = note.fields != values or list(note.tags) != tags.split()
            if not changed:
                continue
            note.fields = values
            note.tags = tags.split()
            dirty.append(note)
            continue

        # Unrecognised (or blank) guid => the user added this row. A row with no
        # content at all is padding left by the sheet's formatting, not a note
        # the user wants, so it is skipped rather than turned into a blank note.
        if not any(cell.strip() for cell in values) and not tags.strip():
            result.skipped += 1
            continue

        note = col.new_note(notetype)
        note.fields = values
        note.tags = tags.split()
        to_add.append(
            AddNoteRequest(
                note=note, deck_id=_resolve_deck_id(col, _row_cell(row, deck_index))
            )
        )
        # Recorded before the write, keyed by position; the guid is already
        # assigned by the backend when the Note is constructed.
        result.created_guids[offset] = note.guid

    if dirty:
        col.update_notes(dirty)
    if to_add:
        col.add_notes(to_add)

    result.updated = len(dirty)
    result.created = len(to_add)
    return result


def _row_cell(row: list[str], index: int) -> str:
    """Return a cell, tolerating short rows."""
    return row[index] if index < len(row) else ""
