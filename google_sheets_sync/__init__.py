"""Google Sheets Sync — push and pull Anki note types to Google Sheets.

This module is the add-on's entry point and stays deliberately thin: it wires
menu actions and hands real work to the other modules.

Threading rules the rest of the code depends on
----------------------------------------------
* Anything touching ``mw.col`` runs with ``uses_collection=True``. Those tasks
  are serialized by a single-worker executor that holds the collection lock.
* Anything touching the network runs with ``uses_collection=False``. Using the
  collection executor for a network call would block every other database
  operation until the request finished, freezing Anki's UI.
* ``run_in_background`` must be called from the Qt main thread, and ``on_done``
  is delivered back on the main thread.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable

from aqt import mw
from aqt.qt import QAction, qconnect, sip
from aqt.utils import askUser, showInfo, showText

from . import auth, sheets, sync
from .config import Connection, Settings, load_settings, save_settings
from .dialogs import (
    ConnectDialog,
    MainWindow,
    NoteTypePickerDialog,
    SignInDialog,
    SheetPickerDialog,
    SpreadsheetPickerDialog,
)

MENU_TITLE = "Google Sheets Sync"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_credentials(settings: Settings) -> bool:
    if not auth.is_connected():
        showInfo(
            "Connect to Google first, from Tools → %s → Connect." % MENU_TITLE
        )
        return False
    if not settings.client_id or not settings.client_secret:
        showInfo(
            "Add your Google OAuth client ID and secret first, via "
            "Tools → %s → Connect." % MENU_TITLE
        )
        return False
    return True


def _client(settings: Settings) -> sheets.GoogleClient:
    return sheets.GoogleClient(settings.client_id, settings.client_secret)


def _run(
    task: Callable[..., Any],
    on_done: Callable[[Future], None],
    *,
    uses_collection: bool,
    label: str | None = None,
) -> None:
    """Schedule a background task, optionally behind a modal progress dialog."""
    if label:
        mw.taskman.with_progress(task, on_done, label=label, uses_collection=uses_collection)
    else:
        mw.taskman.run_in_background(task, on_done, uses_collection=uses_collection)


def _fail(prefix: str) -> Callable[[Future], None]:
    """Build an ``on_done`` that reports any exception as a dialog."""

    def handler(future: Future) -> None:
        try:
            future.result()
        except Exception as exc:
            showInfo(f"{prefix}: {exc}")

    return handler


# ---------------------------------------------------------------------------
# Settings and authentication
# ---------------------------------------------------------------------------


def start_connect(
    parent=None,
    on_result: Callable[[bool, str], None] | None = None,
) -> None:
    """Ask for the OAuth client credentials, then run the sign-in flow.

    Two stages, in this order:

    1. :class:`ConnectDialog` collects the client id and secret. It is fully
       closed before stage 2 begins, so the two windows never stack and the
       user is never looking at a stale form while a sign-in runs.
    2. If the credentials were accepted, the sign-in link appears.

    Cancelling stage 1 means nothing happens at all — no listener is started and
    no network traffic is generated. ``on_result`` is called with
    ``(connected, account)`` only on a completed sign-in.
    """
    settings = load_settings()

    dialog = ConnectDialog(parent or mw, settings)
    if not dialog.exec():
        return  # cancelled; leave everything exactly as it was

    client_id, client_secret = dialog.credentials()
    if not client_id or not client_secret:
        showInfo("Enter both the client ID and the client secret first.")
        return

    if (client_id, client_secret) != (settings.client_id, settings.client_secret):
        settings.client_id = client_id
        settings.client_secret = client_secret
        save_settings(settings)

    # _connect() re-reads the config itself, so the credentials saved just now
    # are picked up rather than this now-stale settings object.
    _connect(parent, on_result=on_result)


def _disconnect_from(
    parent=None,
    on_result: Callable[[], None] | None = None,
) -> None:
    """Revoke the stored tokens and report the outcome to ``on_result``."""

    def done(future: Future) -> None:
        try:
            future.result()
        except Exception as exc:
            showInfo(f"Could not disconnect: {exc}")
            return
        if on_result is not None:
            on_result()

    _run(auth.disconnect, done, uses_collection=False, label="Disconnecting…")


def _connect(
    parent=None,
    on_result: Callable[[bool, str], None] | None = None,
) -> None:
    """Show the sign-in link and wait for the user to complete the flow.

    ``on_result`` is called with ``(connected, account)`` once the flow settles,
    so a host window can refresh its status without being reopened. It is not
    called at all if the user cancels or the flow fails.
    """
    settings = load_settings()
    if not settings.client_id or not settings.client_secret:
        showInfo("Add your Google OAuth client ID and secret first.")
        return

    cancelled = threading.Event()

    # Preparing the session starts the loopback server and builds the URL, so
    # the dialog can show a link that is guaranteed to be capturable by the
    # time the user clicks anything. Nothing is opened automatically.
    try:
        session = auth.ConsentSession(settings.client_id, settings.client_secret)
    except OSError as exc:
        showInfo(f"Could not start the local sign-in listener: {exc}")
        return

    dialog = SignInDialog(parent or mw, session.url)
    # Set once the flow finishes on its own, so the programmatic close below is
    # not mistaken for a user cancellation.
    settled = threading.Event()

    def task() -> None:
        session.run(cancelled=cancelled)

    def done(future: Future) -> None:
        # Mark as settled *before* closing: `finished` fires for any close,
        # including this programmatic one, and must not look like a cancel.
        settled.set()

        # The dialog is closed first so the user is not left staring at it while
        # a message box appears.
        if not sip.isdeleted(dialog):
            dialog.close()

        if cancelled.is_set():
            return  # deliberate cancellation; stay quiet

        try:
            future.result()
        except auth.AuthCancelled:
            return
        except Exception as exc:
            showInfo(f"Could not connect: {exc}")
            return

        # Fetch the account name so the status line can name the signed-in
        # user. This is cosmetic and returns "" when unavailable, so it must
        # never fail the connection.
        account = _client(settings).verify_access()

        if on_result is not None:
            on_result(True, account)

    # Only a *user-initiated* dismissal counts as cancelling. `finished` also
    # fires when `done()` closes the dialog programmatically, so the `settled`
    # guard is required.
    def on_finished(_code: int) -> None:
        if not settled.is_set():
            cancelled.set()

    dialog.finished.connect(on_finished)

    # No progress dialog: this window *is* the progress indicator, and unlike
    # Anki's it can be dismissed.
    _run(task, done, uses_collection=False)
    try:
        dialog.exec()
    finally:
        # If the user closed the dialog before the redirect arrived, the
        # background task still owns the loopback server; cancelling unblocks it
        # and it shuts the server down. abort() is a no-op if run() already took
        # ownership, so this is safe on every exit path.
        session.abort()


def _disconnect() -> None:
    _run(auth.disconnect, _fail("Disconnect failed"), uses_collection=False,
         label="Disconnecting…")


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------


def _unconnected_note_types(settings: Settings) -> list[tuple[int, str]]:
    """Note types that do not already have a connection."""
    already = {c.note_type_id for c in settings.connections}
    col = mw.col
    assert col is not None
    # Each entry is a protobuf NotetypeNameId with .id / .name, not a tuple.
    return [
        (entry.id, entry.name)
        for entry in col.models.all_names_and_ids()
        if entry.id not in already
    ]


def _label(connection: Connection) -> str:
    return f"{connection.note_type_name} → {connection.spreadsheet_title} / {connection.sheet_title}"


def _target_label(connection: Connection) -> str:
    """Where a connection points, with placeholders for missing metadata.

    A malformed entry from an older config can have empty titles, which would
    otherwise render as “ / ” in a confirmation dialog.
    """
    spreadsheet = connection.spreadsheet_title or "(unknown)"
    tab = connection.sheet_title or "(unknown)"
    return f"“{spreadsheet}” / {tab}"


def add_connection(parent=None, on_changed: Callable[[], None] | None = None) -> None:
    settings = load_settings()
    if not _has_credentials(settings):
        return

    available = _unconnected_note_types(settings)
    if not available:
        showInfo("Every note type in this collection already has a connection.")
        return

    picker = NoteTypePickerDialog(parent or mw, available)
    if not picker.exec():
        return
    chosen = picker.selection()
    if chosen is None:
        return
    note_type_id, note_type_name = chosen

    def list_spreadsheets() -> None:
        def task() -> list[tuple[str, str]]:
            files = _client(settings).list_spreadsheets()[:25]
            if not files:
                raise sheets.SheetsError(
                    "No spreadsheets were found in your Google Drive."
                )
            return files

        def done(future: Future) -> None:
            try:
                files = future.result()
            except Exception as exc:
                showInfo(f"{MENU_TITLE}: {exc}")
                return

            dialog = SpreadsheetPickerDialog(parent or mw, files)
            if not dialog.exec():
                return
            selection = dialog.selection()
            if selection is None:
                return
            spreadsheet_id, spreadsheet_title = selection
            list_tabs(spreadsheet_id, spreadsheet_title)

        _run(task, done, uses_collection=False, label="Reading your Drive…")

    def list_tabs(spreadsheet_id: str, spreadsheet_title: str) -> None:
        def task() -> list[tuple[int, str, int]]:
            return _client(settings).list_sheets(spreadsheet_id)

        def done(future: Future) -> None:
            try:
                tabs = future.result()
            except Exception as exc:
                showInfo(f"{MENU_TITLE}: {exc}")
                return
            if not tabs:
                showInfo(f"No tabs were found in “{spreadsheet_title}”.")
                return

            dialog = SheetPickerDialog(parent or mw, spreadsheet_title, tabs)
            if not dialog.exec():
                return
            selection = dialog.selection()
            if selection is None:
                return
            sheet_id, sheet_title = selection

            settings.connections.append(
                Connection(
                    note_type_id=note_type_id,
                    note_type_name=note_type_name,
                    spreadsheet_id=spreadsheet_id,
                    sheet_id=sheet_id,
                    sheet_title=sheet_title,
                    spreadsheet_title=spreadsheet_title,
                )
            )
            save_settings(settings)
            if on_changed is not None:
                on_changed()
            showInfo(
                f"“{note_type_name}” is now connected to “{sheet_title}”. "
                "Use Push to send it to the sheet."
            )

        _run(task, done, uses_collection=False, label="Reading spreadsheet tabs…")

    list_spreadsheets()


def remove_connection(
    parent=None,
    connection_id: int | None = None,
    on_changed: Callable[[], None] | None = None,
) -> None:
    settings = load_settings()
    if not settings.connections:
        showInfo("There are no connections yet. Use “Add connection…” first.")
        return

    # When the hub window supplies a selection, act on it directly.
    if connection_id is None:
        if len(settings.connections) == 1:
            connection_id = settings.connections[0].note_type_id
        else:
            picker = NoteTypePickerDialog(
                parent or mw, [(c.note_type_id, _label(c)) for c in settings.connections]
            )
            picker.setWindowTitle("Remove a connection")
            if not picker.exec():
                return
            chosen = picker.selection()
            if chosen is None:
                return
            connection_id = chosen[0]

    target = next(
        (c for c in settings.connections if c.note_type_id == connection_id), None
    )
    if target is None:
        return

    # Removing a connection is easy to do by accident and there is no undo, so
    # confirm first. The notes and the sheet itself are untouched — only the
    # link between them is forgotten.
    confirmed = askUser(
        f"Remove the connection for “{target.note_type_name or '(unnamed)'}”?\n\n"
        f"It points at {_target_label(target)}.\n\n"
        "Your notes and the spreadsheet are not deleted; this just stops syncing "
        "between them.",
        parent=parent or mw,
        title=MENU_TITLE,
        defaultno=True,
    )
    if not confirmed:
        return

    settings.connections = [
        c for c in settings.connections if c.note_type_id != connection_id
    ]
    save_settings(settings)
    if on_changed is not None:
        on_changed()
    showInfo(
        f"Removed the connection for “{target.note_type_name or '(unnamed)'}”."
    )


def _choose_connection(
    settings: Settings,
    parent=None,
    connection_id: int | None = None,
) -> Connection | None:
    """Resolve a specific connection, or prompt when no id was supplied.

    Per-row Push/Pull actions pass an explicit id and must never fall back to a
    chooser: if that connection disappeared between rendering and clicking,
    report it instead of silently targeting a different connection. The
    chooser remains available only to callers that deliberately omit an id.
    """
    if connection_id is not None:
        chosen = next(
            (c for c in settings.connections if c.note_type_id == connection_id), None
        )
        if chosen is not None:
            return chosen
        showInfo("That connection no longer exists. Refresh the window and try again.")
        return None

    if not settings.connections:
        showInfo("No connections yet. Use “Add connection…” first.")
        return None
    if len(settings.connections) == 1:
        return settings.connections[0]

    picker = NoteTypePickerDialog(
        parent or mw, [(c.note_type_id, _label(c)) for c in settings.connections]
    )
    picker.setWindowTitle(MENU_TITLE)
    if not picker.exec():
        return None
    selection = picker.selection()
    if selection is None:
        return None
    return next(c for c in settings.connections if c.note_type_id == selection[0])


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


def push_to_sheet(parent=None, connection_id: int | None = None) -> None:
    settings = load_settings()
    if not _has_credentials(settings):
        return
    connection = _choose_connection(settings, parent, connection_id)
    if connection is None:
        return

    state: dict[str, sync.NoteSnapshot] = {}

    def read_notes() -> None:
        col = mw.col
        assert col is not None
        state["snapshot"] = sync.snapshot_notes(col, connection.note_type_id)

    def write_sheet() -> int:
        snapshot = state["snapshot"]
        client = _client(settings)
        a1 = sheets.tab_a1_range(connection.sheet_title)
        client.clear_range(connection.spreadsheet_id, a1)
        return client.update_range(
            connection.spreadsheet_id, a1, [snapshot.header, *snapshot.rows]
        )

    def after_write(future: Future) -> None:
        try:
            updated = future.result()
        except Exception as exc:
            showInfo(f"Push failed: {exc}")
            return
        snapshot = state["snapshot"]
        showInfo(
            f"Pushed {len(snapshot.rows)} note(s) from "
            f"“{snapshot.note_type_name}” to “{connection.sheet_title}” "
            f"({updated} cells written)."
        )

    def after_read(future: Future) -> None:
        # The progress dialog opened for the read phase must be closed before
        # a second one is opened for the write phase.
        mw.progress.finish()
        try:
            future.result()
        except Exception as exc:
            showInfo(f"Could not read notes: {exc}")
            return
        _run(write_sheet, after_write, uses_collection=False, label="Pushing…")

    _run(read_notes, after_read, uses_collection=True, label="Reading notes…")


# ---------------------------------------------------------------------------
# Pull
# ---------------------------------------------------------------------------


def pull_from_sheet(parent=None, connection_id: int | None = None) -> None:
    settings = load_settings()
    if not _has_credentials(settings):
        return
    connection = _choose_connection(settings, parent, connection_id)
    if connection is None:
        return

    state: dict[str, list[list[str]]] = {}

    def read_sheet() -> list[list[str]]:
        client = _client(settings)
        a1 = sheets.tab_a1_range(connection.sheet_title)
        return client.read_range(connection.spreadsheet_id, a1)

    def apply_rows() -> sync.PullResult:
        # Runs on the collection executor. Both updates and creations are
        # batched, so a pull applies in one collection write.
        col = mw.col
        assert col is not None
        snapshot = sync.snapshot_notes(col, connection.note_type_id)
        return sync.apply_rows_to_notes(col, snapshot, state["rows"])

    def write_back_guids(result: sync.PullResult) -> None:
        """Record the guids Anki assigned for rows the sheet did not know about.

        Without this the next pull would see the same rows as unrecognised
        again and create duplicates.
        """
        if not result.created_guids:
            message = f"Updated {result.updated} note(s) from “{connection.sheet_title}”."
            if result.skipped:
                message += f" Skipped {result.skipped} empty row(s)."
            showInfo(message)
            return

        def task() -> int:
            client = _client(settings)
            # Row 1 holds the header, so the first data row is sheet row 2.
            return client.write_guids(
                connection.spreadsheet_id,
                connection.sheet_title,
                2,
                result.created_guids,
                result.guid_column_index,
            )

        def report(future: Future) -> None:
            try:
                future.result()
            except Exception as exc:
                showInfo(
                    f"Created {result.created} note(s), but writing guids back "
                    f"to the sheet failed: {exc}\n\nRunning Pull again may "
                    f"create duplicates for those new rows."
                )
                return
            message = (
                f"Created {result.created} and updated {result.updated} "
                f"note(s) from “{connection.sheet_title}”."
            )
            if result.skipped:
                message += f" Skipped {result.skipped} empty row(s)."
            showInfo(message)

        _run(task, report, uses_collection=False, label="Recording new guids…")

    def after_apply(future: Future) -> None:
        # The collection-phase progress dialog must close before the next one
        # opens, otherwise the first modal never goes away.
        mw.progress.finish()
        try:
            result = future.result()
        except Exception as exc:
            showInfo(f"Could not update notes: {exc}")
            return
        write_back_guids(result)

    def after_read(future: Future) -> None:
        mw.progress.finish()
        try:
            state["rows"] = future.result()
        except Exception as exc:
            showInfo(f"Pull failed: {exc}")
            return
        if not state["rows"]:
            showInfo(f"“{connection.sheet_title}” is empty; nothing to pull.")
            return
        _run(apply_rows, after_apply, uses_collection=True, label="Updating notes…")

    _run(read_sheet, after_read, uses_collection=False, label="Reading sheet…")


@dataclass
class _PushAllResult:
    notes: int = 0
    cells: int = 0
    completed: int = 0
    failures: list[str] = field(default_factory=list)


@dataclass
class _PullAllResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    empty_sheets: int = 0
    completed: int = 0
    failures: list[str] = field(default_factory=list)


def push_all_to_sheets(parent=None) -> None:
    """Push every configured connection, then show one combined result."""
    settings = load_settings()
    if not _has_credentials(settings):
        return
    connections = list(settings.connections)
    if not connections:
        showInfo("No connections yet. Use “Add connection…” first.")
        return

    snapshots: dict[int, sync.NoteSnapshot] = {}
    result = _PushAllResult()

    def read_all_notes() -> None:
        col = mw.col
        assert col is not None
        for connection in connections:
            try:
                snapshots[connection.note_type_id] = sync.snapshot_notes(
                    col, connection.note_type_id
                )
            except Exception as exc:
                result.failures.append(
                    f"{connection.note_type_name}: could not read notes ({exc})"
                )

    def write_all_sheets() -> _PushAllResult:
        for connection in connections:
            snapshot = snapshots.get(connection.note_type_id)
            if snapshot is None:
                continue
            try:
                client = _client(settings)
                a1 = sheets.tab_a1_range(connection.sheet_title)
                client.clear_range(connection.spreadsheet_id, a1)
                cells = client.update_range(
                    connection.spreadsheet_id,
                    a1,
                    [snapshot.header, *snapshot.rows],
                )
                result.notes += len(snapshot.rows)
                result.cells += cells
                result.completed += 1
            except Exception as exc:
                result.failures.append(
                    f"{connection.note_type_name}: push failed ({exc})"
                )
        return result

    def report(future: Future) -> None:
        try:
            batch = future.result()
        except Exception as exc:
            showInfo(f"Push all failed: {exc}")
            return
        message = (
            f"Push all complete: {batch.completed} of {len(connections)} "
            f"connection(s), {batch.notes} note(s), "
            f"{batch.cells} cells written."
        )
        if batch.failures:
            message += "\n\nIssues:\n• " + "\n• ".join(batch.failures)
        showInfo(message)

    def after_read(future: Future) -> None:
        mw.progress.finish()
        try:
            future.result()
        except Exception as exc:
            showInfo(f"Push all failed while reading notes: {exc}")
            return
        _run(write_all_sheets, report, uses_collection=False, label="Pushing all…")

    _run(read_all_notes, after_read, uses_collection=True, label="Reading notes…")


def pull_all_from_sheets(parent=None) -> None:
    """Pull every configured connection, then show one combined result."""
    settings = load_settings()
    if not _has_credentials(settings):
        return
    connections = list(settings.connections)
    if not connections:
        showInfo("No connections yet. Use “Add connection…” first.")
        return

    rows_by_id: dict[int, list[list[str]]] = {}
    pulls_by_id: dict[int, sync.PullResult] = {}
    result = _PullAllResult()

    def read_all_sheets() -> None:
        for connection in connections:
            try:
                client = _client(settings)
                rows_by_id[connection.note_type_id] = client.read_range(
                    connection.spreadsheet_id,
                    sheets.tab_a1_range(connection.sheet_title),
                )
            except Exception as exc:
                result.failures.append(
                    f"{connection.note_type_name}: could not read sheet ({exc})"
                )

    def apply_all_rows() -> _PullAllResult:
        col = mw.col
        assert col is not None
        for connection in connections:
            rows = rows_by_id.get(connection.note_type_id)
            if not rows:
                if connection.note_type_id in rows_by_id:
                    # Match a single Pull: an empty sheet is a successful
                    # no-op, not a failed connection.
                    result.empty_sheets += 1
                    result.completed += 1
                continue
            try:
                snapshot = sync.snapshot_notes(col, connection.note_type_id)
                pull_result = sync.apply_rows_to_notes(col, snapshot, rows)
                pulls_by_id[connection.note_type_id] = pull_result
                result.created += pull_result.created
                result.updated += pull_result.updated
                result.skipped += pull_result.skipped
                result.completed += 1
            except Exception as exc:
                result.failures.append(
                    f"{connection.note_type_name}: could not update notes ({exc})"
                )
        return result

    def write_back_all_guids() -> _PullAllResult:
        for connection in connections:
            pull_result = pulls_by_id.get(connection.note_type_id)
            if pull_result is None or not pull_result.created_guids:
                continue
            try:
                client = _client(settings)
                client.write_guids(
                    connection.spreadsheet_id,
                    connection.sheet_title,
                    2,
                    pull_result.created_guids,
                    pull_result.guid_column_index,
                )
            except Exception as exc:
                result.failures.append(
                    f"{connection.note_type_name}: created notes, but could not "
                    f"write their guids back ({exc}); pulling again may create duplicates"
                )
        return result

    def report(future: Future) -> None:
        try:
            batch = future.result()
        except Exception as exc:
            showInfo(f"Pull all failed: {exc}")
            return
        message = (
            f"Pull all complete: {batch.completed} of {len(connections)} "
            f"connection(s); created {batch.created}, updated {batch.updated}, "
            f"skipped {batch.skipped} empty row(s)."
        )
        if batch.empty_sheets:
            message += f" {batch.empty_sheets} sheet(s) were empty."
        if batch.failures:
            message += "\n\nIssues:\n• " + "\n• ".join(batch.failures)
        showInfo(message)

    def after_apply(future: Future) -> None:
        mw.progress.finish()
        try:
            future.result()
        except Exception as exc:
            showInfo(f"Pull all failed while updating notes: {exc}")
            return
        _run(
            write_back_all_guids,
            report,
            uses_collection=False,
            label="Recording new guids…",
        )

    def after_read(future: Future) -> None:
        mw.progress.finish()
        try:
            future.result()
        except Exception as exc:
            showInfo(f"Pull all failed while reading sheets: {exc}")
            return
        _run(apply_all_rows, after_apply, uses_collection=True, label="Updating notes…")

    _run(read_all_sheets, after_read, uses_collection=False, label="Reading sheets…")


# ---------------------------------------------------------------------------
# The single entry point
# ---------------------------------------------------------------------------

#: The one hub window. Kept so repeated clicks raise the existing window
#: instead of opening a second copy.
_window: MainWindow | None = None


def _refresh_window() -> None:
    """Reload the hub window from the stored config."""
    if _window is None or sip.isdeleted(_window):
        return
    _window.refresh_connections(load_settings())


def _show_main_window() -> None:
    global _window

    # A modal dialog's exec() returns when it is *accepted or rejected*, which
    # only hides it — the object stays alive and `sip.isdeleted` is still False.
    # Reusing that hidden instance would call raise_() on an invisible widget,
    # so the visibility has to be checked too. When it is gone, drop the stale
    # reference and build a fresh window.
    if _window is not None:
        if not sip.isdeleted(_window) and _window.isVisible():
            _window.raise_()
            _window.activateWindow()
            return
        _window = None

    settings = load_settings()
    connected = auth.is_connected()
    account = ""

    if connected and settings.client_id and settings.client_secret:
        def fetch(future: Future) -> None:
            nonlocal account
            try:
                account = _client(settings).verify_access()
            except Exception:
                account = ""  # cosmetic only

        # Cosmetic only, so no progress dialog.
        _run(fetch, lambda _f: None, uses_collection=False)

    window = MainWindow(mw, settings, connected, account)
    window.refresh_connections(settings)

    def on_account_button(_checked: bool = False) -> None:
        """Connect or disconnect, matching the window's current auth state.

        Dispatched on each click rather than wiring a separate handler per
        state, because the button relabels itself once sign-in succeeds.
        """
        if sip.isdeleted(window):
            return
        if window.is_connected():
            _disconnect_from(
                window, on_result=lambda: on_account_changed(False, "")
            )
        else:
            start_connect(window, on_result=on_account_changed)

    def on_account_changed(connected: bool, account: str) -> None:
        if not sip.isdeleted(window):
            window.set_connected(connected, account)
            # Signing in can make previously saved connections usable, and
            # signing out invalidates them, so the list is re-read either way.
            window.refresh_connections(load_settings())

    def on_action(note_type_id: int, action: str) -> None:
        """Run the action a connection row's button asked for.

        The note type id comes from the row that was clicked, so there is no
        reliance on a current selection that could be stale.
        """
        if sip.isdeleted(window):
            return
        if action == "push":
            push_to_sheet(window, note_type_id)
        elif action == "pull":
            pull_from_sheet(window, note_type_id)
        elif action == "remove":
            remove_connection(window, note_type_id, _refresh_window)

    window.account_button.clicked.connect(on_account_button)
    window.action_requested.connect(on_action)
    window.push_all_button.clicked.connect(
        lambda _checked=False: push_all_to_sheets(window)
    )
    window.pull_all_button.clicked.connect(
        lambda _checked=False: pull_all_from_sheets(window)
    )
    window.add_button.clicked.connect(
        lambda _c=False: add_connection(window, _refresh_window)
    )

    def on_window_finished(_code: int) -> None:
        # Drop the reference so the next menu click builds a fresh window
        # rather than trying to revive a hidden one.
        global _window
        if _window is window:
            _window = None
        window.deleteLater()

    window.finished.connect(on_window_finished)

    _window = window
    window.exec()


def _build_menu() -> None:
    """Add the add-on's single Tools-menu entry."""
    action = QAction(MENU_TITLE, mw)
    qconnect(action.triggered, _show_main_window)
    mw.form.menuTools.addAction(action)


def _dependency_guard() -> bool:
    """Report clearly if ``requests`` is missing from this Anki install."""
    try:
        import requests  # noqa: F401
    except ImportError:
        showText(
            "<p>This add-on needs the <code>requests</code> package, which this "
            "Anki installation does not provide.</p>"
            "<p>Updating Anki usually resolves this.</p>",
            title=f"{MENU_TITLE} cannot start",
            type="html",
        )
        return False
    return True


def _start() -> None:
    """Entry point, called once Anki's main window exists.

    Importing this module must stay side-effect free enough that the pure-logic
    submodules can be imported outside a running Anki (for tests). Touching
    ``mw.form`` requires a real main window, so the menu is registered from
    here rather than at import time.
    """
    if mw is None:  # imported outside a running Anki
        return
    if not _dependency_guard():
        return
    _build_menu()


_start()
