"""Configuration and credential storage for the Google Sheets sync add-on.

Two separate stores are used, deliberately:

* **Settings** (OAuth client id/secret, the list of connections) live in the
  add-on's ``meta.json`` via :meth:`AddonManager.writeConfig`. That file
  survives AnkiWeb add-on upgrades, which is exactly what we want for
  user-owned settings.

* **Tokens** (the OAuth ``refresh_token``) live in our own JSON file inside the
  Anki *profile* folder. They are deliberately kept out of ``meta.json`` so
  that a refresh token is never written into a file that gets copied, shared,
  or included when packaging the add-on for distribution. Deleting the add-on
  from Anki also does not leak them.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any

from aqt import mw

#: Name of the folder in ``addons21`` that Anki imported this add-on from.
#:
#: Anki derives the module name from the *folder* name (see
#: ``AddonManager.allAddons``), not from the Python package name. Hardcoding a
#: guess here silently breaks ``writeConfig``, because it would point at a
#: directory that does not exist. Instead the real module is derived at runtime
#: from the loaded package, so a renamed junction keeps working.
#: ``__name__`` is e.g. ``myaddon`` or ``myaddon.submodule``; the config API
#: wants the first segment only.
MODULE = __name__.split(".", maxsplit=1)[0]

#: Name of the file inside the profile folder that holds OAuth tokens.
TOKEN_FILENAME = "google_sheets_sync_tokens.json"

#: OAuth scopes. ``drive.readonly`` (rather than ``drive.file``) is required
#: because ``drive.file`` only grants access to files the app itself created,
#: and we need to list the spreadsheets that already exist in the user's Drive.
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


@dataclass
class Connection:
    """A single link between one Anki note type and one Google Sheets tab.

    ``note_type_id`` is the Anki notetype id (``mid``). It is an integer and is
    the stable identifier, whereas the display name can be renamed by the user
    at any time, which would silently break a name-based lookup.
    """

    note_type_id: int
    note_type_name: str
    spreadsheet_id: str
    sheet_id: int
    sheet_title: str
    spreadsheet_title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Connection":
        return cls(
            note_type_id=int(data["note_type_id"]),
            note_type_name=str(data["note_type_name"]),
            spreadsheet_id=str(data["spreadsheet_id"]),
            sheet_id=int(data["sheet_id"]),
            sheet_title=str(data["sheet_title"]),
            spreadsheet_title=str(data.get("spreadsheet_title", "")),
        )


@dataclass
class Settings:
    """Everything persisted in ``meta.json``."""

    client_id: str = ""
    client_secret: str = ""
    connections: list[Connection] = field(default_factory=list)

    def connection_for_note_type(self, note_type_id: int) -> Connection | None:
        for conn in self.connections:
            if conn.note_type_id == note_type_id:
                return conn
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "connections": [c.to_dict() for c in self.connections],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        # Be tolerant of malformed entries so that one bad record cannot make
        # the whole add-on unusable; the user can always re-create it in the UI.
        connections: list[Connection] = []
        for raw in data.get("connections", []) or []:
            try:
                connections.append(Connection.from_dict(raw))
            except (KeyError, TypeError, ValueError):
                continue
        return cls(
            client_id=str(data.get("client_id", "")),
            client_secret=str(data.get("client_secret", "")),
            connections=connections,
        )


def _addon_manager():
    """Return the add-on manager, or ``None`` outside a running Anki."""
    return getattr(mw, "addonManager", None) if mw is not None else None


def _profile_folder() -> str:
    """Return the profile folder, or a temp dir outside a running Anki."""
    if mw is None:
        return tempfile.gettempdir()
    return mw.pm.profileFolder()


def load_settings() -> Settings:
    """Read settings from the add-on config, falling back to defaults.

    ``AddonManager.getConfig`` returns ``None`` when the add-on has no
    ``config.json`` defaults file, and only shallow-merges the user's stored
    values on top of the shipped defaults. We therefore treat every key as
    optional rather than relying on the defaults file to exist.
    """
    manager = _addon_manager()
    if manager is None:
        return Settings()
    try:
        raw = manager.getConfig(MODULE)
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return Settings()
    return Settings.from_dict(raw)


def save_settings(settings: Settings) -> None:
    """Write settings back to ``meta.json``.

    ``writeConfig`` replaces the whole ``config`` key rather than merging, so
    the full document must be passed every time.
    """
    manager = _addon_manager()
    if manager is None:
        return
    manager.writeConfig(MODULE, settings.to_dict())


# ---------------------------------------------------------------------------
# Token storage
# ---------------------------------------------------------------------------


def _token_path() -> str:
    return os.path.join(_profile_folder(), TOKEN_FILENAME)


def load_tokens() -> dict[str, Any]:
    path = _token_path()
    try:
        with open(path, encoding="utf8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_tokens(tokens: dict[str, Any]) -> None:
    path = _token_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf8") as handle:
        json.dump(tokens, handle, indent=2)


def clear_tokens() -> None:
    try:
        os.remove(_token_path())
    except FileNotFoundError:
        pass
