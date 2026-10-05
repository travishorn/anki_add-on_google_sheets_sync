"""Qt dialogs for configuring the add-on and managing connections.

All UI lives here so that ``__init__.py`` stays a thin entry point. Each dialog
is a plain :class:`QDialog`; results are returned to the caller rather than
written to the config directly, so the orchestration in ``__init__.py`` can
save only when something actually changed.
"""

from __future__ import annotations

from typing import Sequence

from aqt.qt import (
    QApplication,
    QComboBox,
    QDesktopServices,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTimer,
    QUrl,
    QVBoxLayout,
    QWidget,
    pyqtSignal,
    sip,
)

from .config import Connection, Settings

#: Longest tab title shown before eliding, to keep the picker readable.
_MAX_TITLE_CHARS = 60

#: ``Qt.ItemDataRole.UserRole`` is 32; Qt enums are not exposed as constants
#: through ``aqt.qt``'s star import, so the raw value is used.
USER_ROLE = 32

#: The note type id that a connection row's action buttons act on. Read back via
#: :meth:`MainWindow.note_type_at` when an action is triggered.
NOTE_TYPE_ROLE = USER_ROLE

#: A single-row widget that holds one connection's action buttons, stashed on its
#: list item so :meth:`MainWindow.set_actions_enabled` can reach it.
ACTIONS_ROLE = USER_ROLE + 1


def _elide(text: str) -> str:
    return text if len(text) <= _MAX_TITLE_CHARS else text[: _MAX_TITLE_CHARS - 1] + "…"


class ConnectDialog(QDialog):
    """Enter the OAuth client credentials for the Google account.

    Deliberately minimal: just the two fields and OK/Cancel. There is no status
    line and no Connect/Disconnect button, because this screen has one job —
    collect credentials. The sign-in flow starts only once they are accepted
    with OK, so this dialog is always closed before the sign-in link appears
    rather than sitting open behind it.
    """

    def __init__(self, parent, settings: Settings) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect to Google")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)

        intro = QLabel(
            "To connect, create a Google Cloud project, enable the Google Sheets "
            "and Google Drive APIs, and create an OAuth client of type "
            "“Desktop app”. Paste its credentials below."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.client_id = QLineEdit(settings.client_id)
        self.client_id.setPlaceholderText("…apps.googleusercontent.com")
        self.client_secret = QLineEdit(settings.client_secret)
        self.client_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.client_secret.setPlaceholderText("Client secret")
        form.addRow("Client ID", self.client_id)
        form.addRow("Client secret", self.client_secret)
        layout.addLayout(form)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        # Always present, but the Optional return type means it must be narrowed.
        ok_button = box.button(QDialogButtonBox.StandardButton.Ok)
        assert ok_button is not None
        self.ok_button: QPushButton = ok_button
        self.ok_button.setEnabled(False)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

        # OK stays disabled until both fields are filled, so an incomplete
        # entry cannot start a sign-in that is guaranteed to fail.
        self.client_id.textChanged.connect(self._update_ok)
        self.client_secret.textChanged.connect(self._update_ok)
        self._update_ok()

    def _update_ok(self) -> None:
        # Both fields must be non-empty. Testing the tuple itself would always
        # be True, because a two-element tuple is truthy even when it holds
        # only empty strings.
        client_id, client_secret = self.credentials()
        self.ok_button.setEnabled(bool(client_id) and bool(client_secret))

    def credentials(self) -> tuple[str, str]:
        return self.client_id.text().strip(), self.client_secret.text().strip()


def connection_status(connected: bool, account: str) -> str:
    """Human-readable summary of the current authentication state."""
    if not connected:
        return "Not connected to Google."
    if account:
        return f"Connected as {account}."
    return "Connected to Google."


class SpreadsheetPickerDialog(QDialog):
    """Choose a spreadsheet, then a tab within it.

    Both lists are populated in the background; the caller passes in the
    already-fetched data so that this dialog performs no network calls and
    cannot freeze the UI.
    """

    def __init__(
        self,
        parent,
        spreadsheets: Sequence[tuple[str, str]],
        sheets_by_id: dict[str, list[tuple[int, str, int]]],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose a Google Sheet")
        self.setMinimumSize(560, 420)

        self._sheets_by_id = sheets_by_id
        self._chosen_id: str = ""

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Spreadsheet"))
        self.spreadsheet_list = QListWidget()
        for file_id, name in spreadsheets:
            item = QListWidgetItem(_elide(name))
            item.setData(USER_ROLE, file_id)
            item.setToolTip(name)
            self.spreadsheet_list.addItem(item)
        layout.addWidget(self.spreadsheet_list)

        layout.addWidget(QLabel("Tab"))
        self.sheet_list = QListWidget()
        layout.addWidget(self.sheet_list)

        self.spreadsheet_list.currentRowChanged.connect(self._on_spreadsheet_changed)
        self.sheet_list.currentRowChanged.connect(self._on_sheet_changed)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        # Keep a reference so the OK button can be enabled/disabled as the
        # selection changes.
        self.ok_button = box.button(QDialogButtonBox.StandardButton.Ok)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

        if self.spreadsheet_list.count():
            self.spreadsheet_list.setCurrentRow(0)
        else:
            self._update_ok_state()

    def _on_spreadsheet_changed(self, row: int) -> None:
        self.sheet_list.clear()
        self._chosen_id = ""
        item = self.spreadsheet_list.item(row) if row >= 0 else None
        if item is None:
            self._update_ok_state()
            return

        self._chosen_id = str(item.data(USER_ROLE))

        for sheet_id, title, rows in self._sheets_by_id.get(self._chosen_id, []):
            label = _elide(title) if title else "(unnamed)"
            entry = QListWidgetItem(f"{label}  ({rows} rows)")
            # Store the id and the real (un-elided) title for use downstream.
            entry.setData(USER_ROLE, sheet_id)
            entry.setData(USER_ROLE + 1, title)
            self.sheet_list.addItem(entry)

        if self.sheet_list.count():
            self.sheet_list.setCurrentRow(0)
        self._update_ok_state()

    def _on_sheet_changed(self, _row: int) -> None:
        self._update_ok_state()

    def _update_ok_state(self) -> None:
        if self.ok_button is not None:
            self.ok_button.setEnabled(self.selection() is not None)

    def selection(self) -> tuple[str, int, str, str] | None:
        """Return ``(spreadsheet_id, sheet_id, sheet_title, spreadsheet_title)``."""
        row = self.spreadsheet_list.currentRow()
        item = self.spreadsheet_list.item(row) if row >= 0 else None
        if item is None:
            return None

        sheet_row = self.sheet_list.currentRow()
        sheet_item = self.sheet_list.item(sheet_row) if sheet_row >= 0 else None
        if sheet_item is None:
            return None

        return (
            str(item.data(USER_ROLE)),
            int(sheet_item.data(USER_ROLE)),
            str(sheet_item.data(USER_ROLE + 1)),
            item.text(),
        )


class ConnectionDialog(QDialog):
    """Add or edit a single note type ↔ sheet connection."""

    def __init__(self, parent, connection: Connection) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connection")
        self.setMinimumWidth(480)
        self._connection = connection

        layout = QVBoxLayout(self)
        form = QFormLayout()

        form.addRow("Note type", QLabel(connection.note_type_name))
        form.addRow(
            "Spreadsheet",
            QLabel(_elide(connection.spreadsheet_title or "(unknown)")),
        )
        form.addRow("Tab", QLabel(_elide(connection.sheet_title)))
        layout.addLayout(form)

        note = QLabel(
            "Pushing replaces the whole tab with the current contents of this "
            "note type."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)


class SignInDialog(QDialog):
    """Modal shown while the user completes sign-in.

    The consent URL is displayed rather than opened automatically: some users
    need to paste it elsewhere, use a different browser or profile, or simply
    want to see where they are being sent before trusting it with their Google
    credentials. Two actions are offered — open it in the default browser, or
    copy it to the clipboard.

    Closing this dialog, pressing Escape, or pressing Cancel all mean the same
    thing: the user changed their mind.

    The dialog deliberately does not implement ``reject``/``accept`` itself
    beyond closing; the caller polls ``was_cancelled`` once the flow finishes.
    """

    def __init__(self, parent, url: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("Waiting for sign-in…")
        self.setMinimumWidth(520)
        # Allow Escape and the title-bar close button, both of which route to
        # reject() -> done(), the same path as the Cancel button.
        self.setModal(True)
        self._cancelled = False
        self._url = url
        self._opened = False

        layout = QVBoxLayout(self)

        message = QLabel(
            "Approve access to your Google account to let this add-on read and "
            "write your spreadsheets."
        )
        message.setWordWrap(True)
        layout.addWidget(message)

        layout.addWidget(QLabel("Sign-in link:"))

        # Read-only and selectable so the URL can be inspected, part-selected,
        # or copied with the keyboard even if the Copy button is not used.
        self.url_field = QLineEdit(url)
        self.url_field.setReadOnly(True)
        self.url_field.setCursorPosition(0)
        self.url_field.setToolTip(url)
        layout.addWidget(self.url_field)

        actions = QHBoxLayout()

        self.open_button = QPushButton("Open in browser")
        self.open_button.setDefault(True)
        self.open_button.setToolTip("Open this link in your default browser")
        actions.addWidget(self.open_button)

        self.copy_button = QPushButton("Copy link")
        self.copy_button.setToolTip("Copy the link to your clipboard")
        actions.addWidget(self.copy_button)

        actions.addStretch(1)
        layout.addLayout(actions)

        hint = QLabel(
            "Finish signing in, then return to Anki. If the browser asks to "
            "confirm, the page will say “Connected” and you can close the tab."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        box = QHBoxLayout()
        self.status = QLabel("Waiting for you to complete sign-in…")
        self.status.setWordWrap(True)
        box.addWidget(self.status)

        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self._on_cancel)
        box.addWidget(cancel)
        layout.addLayout(box)

        self.open_button.clicked.connect(self.open_in_browser)
        self.copy_button.clicked.connect(self.copy_link)

    def open_in_browser(self) -> None:
        """Open the consent URL in the system browser.

        Uses Qt's platform handler rather than ``webbrowser`` so the request
        goes through the same mechanism as every other link Anki opens, and
        reports failure to the user instead of silently doing nothing.
        """
        if not QDesktopServices.openUrl(QUrl(self._url)):
            self._show_open_failed()

    def _show_open_failed(self) -> None:
        """Tell the user the browser could not be launched, and offer a copy."""
        self.status.setText(
            "Could not open a browser. Copy the link above and paste it into "
            "your browser instead."
        )
        self.copy_link()
        self.open_button.setEnabled(False)

    def copy_link(self) -> None:
        """Copy the consent URL to the system clipboard.

        The clipboard is cleared by the OS only when the next copy happens, but
        the OAuth code is delivered to the loopback server rather than read back
        out of the clipboard, so a lingering URL is not a credential leak.
        """
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._url)
        self.copy_button.setText("Copied")
        # Restore the label shortly after so it still reads "Copy link" the next
        # time the dialog is opened. Timed against this widget so the timer is
        # cancelled automatically if the dialog is closed first.
        QTimer.singleShot(1500, self._restore_copy_label)

    def _restore_copy_label(self) -> None:
        if not sip.isdeleted(self):
            self.copy_button.setText("Copy link")

    def _on_cancel(self) -> None:
        self._cancelled = True
        self.done(0)

    def was_cancelled(self) -> bool:
        """Whether the user dismissed the dialog instead of completing sign-in."""
        return self._cancelled


class NoteTypePickerDialog(QDialog):
    """Choose an Anki note type to create a new connection for.

    Only note types that do not already have a connection are offered, so a
    note type cannot be bound to two different sheets by accident.
    """

    def __init__(
        self,
        parent,
        available: Sequence[tuple[int, str]],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect a note type")
        self.setMinimumSize(420, 320)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Note type"))

        self.combo = QComboBox()
        for note_type_id, name in available:
            self.combo.addItem(name, note_type_id)
        layout.addWidget(self.combo)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def selection(self) -> tuple[int, str] | None:
        index = self.combo.currentIndex()
        if index < 0:
            return None
        return int(self.combo.itemData(index)), self.combo.itemText(index)


class ConnectionRow(QWidget):
    """One connection's label plus its own Push / Pull / Remove buttons.

    Actions live next to the connection they affect rather than in a shared
    toolbar, so there is never an implicit "current row" that a button might
    act on by mistake. Every button is created disabled and wired by
    ``__init__.py``; this widget only reports what was clicked, via
    ``action_clicked``, together with the note type it belongs to.
    """

    #: Note type ids are large Anki integer ids (often milliseconds since epoch),
    #: so use ``object`` rather than Qt's signed 32-bit ``int`` signal type.
    action_clicked = pyqtSignal(object, str)

    def __init__(self, connection: Connection) -> None:
        super().__init__()
        self.note_type_id = connection.note_type_id

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(8)

        text = QVBoxLayout()
        text.setSpacing(0)
        name = QLabel(connection.note_type_name)
        name.setToolTip(connection.note_type_name)
        target = QLabel(
            f"{_elide(connection.spreadsheet_title or '(unknown)')} / "
            f"{_elide(connection.sheet_title)}"
        )
        target.setToolTip(
            f"{connection.spreadsheet_title} / {connection.sheet_title}"
        )
        # Dim the target line so the note type reads as the primary label.
        target.setStyleSheet("color: palette(mid);")
        text.addWidget(name)
        text.addWidget(target)
        layout.addLayout(text, 1)

        self.push_button = QPushButton("Push")
        self.pull_button = QPushButton("Pull")
        self.remove_button = QPushButton("Remove")
        for button, action in (
            (self.push_button, "push"),
            (self.pull_button, "pull"),
            (self.remove_button, "remove"),
        ):
            button.setEnabled(False)
            button.clicked.connect(
                lambda _checked=False, a=action, n=self.note_type_id: self.action_clicked.emit(n, a)
            )
            layout.addWidget(button)

    def set_actions_enabled(self, enabled: bool) -> None:
        """Enable or disable all three actions as a group."""
        self.push_button.setEnabled(enabled)
        self.pull_button.setEnabled(enabled)
        self.remove_button.setEnabled(enabled)


class MainWindow(QDialog):
    """The single window behind the one Tools-menu entry.

    It has two sections — Google account and connections. Each connection carries
    its own Push / Pull / Remove buttons instead of relying on a highlighted row,
    so there is no "current selection" to get out of sync with an action. The
    window itself performs no I/O: ``ConnectionRow`` emits ``action_clicked`` and
    ``__init__.py`` runs the flows, preserving the existing threading behaviour.
    """

    #: Emitted as ``(note_type_id, action)`` when any row's button is pressed.
    #: ``object`` preserves Anki's large integer note type ids without 32-bit
    #: truncation through Qt's ``int`` signal type.
    action_requested = pyqtSignal(object, str)

    def __init__(
        self,
        parent,
        settings: Settings,
        connected: bool,
        account: str,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Google Sheets Sync")
        self.setMinimumSize(640, 480)

        layout = QVBoxLayout(self)

        intro = QLabel(
            "Sync Anki note types to tabs in Google Sheets. Pushing replaces the "
            "target tab; pulling applies the sheet back to your notes. Use "
            "Push all or Pull all to process every connection."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(self._account_box(connected, account))
        layout.addWidget(self._connections_box(settings))

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        box.accepted.connect(self.accept)
        layout.addWidget(box)

        # One place decides which controls are usable, so the initial state is
        # never a special case that can drift from later updates.
        self._update_actions()

    # -- sections ---------------------------------------------------------

    def _account_box(self, connected: bool, account: str) -> QGroupBox:
        box = QGroupBox("Google account")
        row = QHBoxLayout(box)

        self.status = QLabel(connection_status(connected, account))
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)

        # A single button that is either Connect or Disconnect. There is no
        # separate Settings entry: the credentials are collected by Connect
        # itself, which is the only time they are needed.
        self.account_button = QPushButton("Disconnect" if connected else "Connect")
        row.addWidget(self.account_button)
        self._connected = connected
        return box

    def _connections_box(self, settings: Settings) -> QGroupBox:
        box = QGroupBox("Connections")
        layout = QVBoxLayout(box)

        self.connections_list = QListWidget()
        self.connections_list.setMinimumHeight(160)
        # Rows are now self-contained action groups, so there is no current row
        # to highlight and no selection to act on.
        self.connections_list.setSelectionMode(
            self.connections_list.SelectionMode.NoSelection
        )
        layout.addWidget(self.connections_list)

        self.empty_hint = QLabel("")
        self.empty_hint.setWordWrap(True)
        layout.addWidget(self.empty_hint)

        self.push_all_button = QPushButton("Push all")
        self.pull_all_button = QPushButton("Pull all")
        self.add_button = QPushButton("Add connection…")
        row = QHBoxLayout()
        row.addWidget(self.push_all_button)
        row.addWidget(self.pull_all_button)
        row.addWidget(self.add_button)
        row.addStretch(1)
        layout.addLayout(row)

        self._populate(settings)
        return box

    def _populate(self, settings: Settings) -> None:
        """Rebuild the connection rows from scratch.

        Rows are cheap and holding live buttons, so rebuilding is simpler and less
        error-prone than trying to update them in place. Any row a user was
        mid-click on is gone afterwards, but there is no selection state to lose.
        """
        self.connections_list.clear()
        for conn in settings.connections:
            row = ConnectionRow(conn)
            row.action_clicked.connect(self._on_action)

            item = QListWidgetItem()
            item.setData(NOTE_TYPE_ROLE, conn.note_type_id)
            item.setData(ACTIONS_ROLE, row)
            item.setSizeHint(row.sizeHint())
            self.connections_list.addItem(item)
            self.connections_list.setItemWidget(item, row)

    def _on_action(self, note_type_id: int, action: str) -> None:
        """Forward a row's button press to ``__init__.py``."""
        self.action_requested.emit(note_type_id, action)

    # -- state updates ----------------------------------------------------

    def set_connected(self, connected: bool, account: str = "") -> None:
        """Refresh the account section after sign-in or sign-out.

        Also re-gates every connection's actions: pushing, pulling, and removing
        all require a live Google account, so they become available only once
        sign-in succeeds.
        """
        self._connected = connected
        self.status.setText(connection_status(connected, account))
        self.account_button.setText("Disconnect" if connected else "Connect")
        self._update_actions()

    def is_connected(self) -> bool:
        """Whether the window currently believes a Google account is linked."""
        return self._connected

    def refresh_connections(self, settings: Settings) -> None:
        """Rebuild the connection list after one is added or removed."""
        self._populate(settings)
        self._update_actions()

    def _update_actions(self) -> None:
        """Gate the add button and every row's actions on auth state.

        Not being connected is the only reason these are disabled: each row's
        buttons always act on a known connection, so there is no selection to be
        missing.
        """
        connected = self._connected
        has_connections = self.connections_list.count() > 0
        self.add_button.setEnabled(connected)
        self.push_all_button.setEnabled(connected and has_connections)
        self.pull_all_button.setEnabled(connected and has_connections)

        for row in range(self.connections_list.count()):
            item = self.connections_list.item(row)
            widget = item.data(ACTIONS_ROLE) if item is not None else None
            if isinstance(widget, ConnectionRow):
                widget.set_actions_enabled(connected)

        if not connected:
            self.empty_hint.setText(
                "Connect to Google to manage connections and sync."
            )
        elif self.connections_list.count() == 0:
            self.empty_hint.setText("No connections yet — add one to enable syncing.")
        else:
            self.empty_hint.setText("")
