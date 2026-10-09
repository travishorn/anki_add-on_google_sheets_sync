"""Minimal Google Drive and Google Sheets client built on ``requests``.

Only the handful of endpoints this add-on needs are wrapped:

* ``drive.files.list`` to list spreadsheets in the user's Drive.
* ``spreadsheets.get`` to enumerate the tabs inside one spreadsheet.
* ``spreadsheets.values.clear`` / ``.update`` / ``.get`` to read and write a
  tab's cells.

Every call here performs network I/O and must therefore be made off the Qt main
thread.
"""

from __future__ import annotations

from typing import Any

import requests

from .auth import get_access_token

DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
SHEETS_BASE_URL = "https://sheets.googleapis.com/v4/spreadsheets"

SHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"

#: Google rejects very large single requests, so writes are chunked.
MAX_CELLS_PER_WRITE = 50_000

#: Network calls should never hang the add-on indefinitely.
DEFAULT_TIMEOUT = 60


class SheetsError(RuntimeError):
    """Raised when a Drive or Sheets API call fails."""


class GoogleClient:
    """Thin wrapper that attaches the OAuth bearer token to each request."""

    def __init__(self, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._session = requests.Session()

    def _headers(self) -> dict[str, str]:
        token = get_access_token(self._client_id, self._client_secret)
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            response = self._session.request(
                method,
                url,
                headers=self._headers(),
                timeout=DEFAULT_TIMEOUT,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise SheetsError(f"Could not reach Google: {exc}") from exc

        if response.status_code == 204 or not response.content:
            return {}

        try:
            payload = response.json()
        except ValueError:
            raise SheetsError(
                f"Google returned a non-JSON response (HTTP {response.status_code})."
            ) from None

        if not isinstance(payload, dict):
            raise SheetsError("Google returned an unexpected response shape.")

        if response.status_code >= 400:
            raise SheetsError(_describe_error(payload, response.status_code))

        return payload

    # -- Drive ------------------------------------------------------------

    def list_spreadsheets(self, page_size: int = 100) -> list[tuple[str, str]]:
        """Return ``(spreadsheet_id, title)`` for spreadsheets in the Drive.

        Results are ordered most-recently-modified first. The paging loop is
        explicit rather than using the Drive client library.
        """
        results: list[tuple[str, str]] = []
        page_token: str | None = None

        while True:
            params: dict[str, Any] = {
                "q": f"mimeType='{SHEET_MIME_TYPE}' and trashed=false",
                "orderBy": "modifiedTime desc",
                "pageSize": page_size,
                "fields": "nextPageToken, files(id, name)",
            }
            if page_token:
                params["pageToken"] = page_token

            payload = self._request("GET", DRIVE_FILES_URL, params=params)
            for entry in payload.get("files", []) or []:
                file_id = entry.get("id")
                name = entry.get("name")
                if file_id and name:
                    results.append((file_id, name))

            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        return results

    # -- Sheets metadata ---------------------------------------------------

    def list_sheets(self, spreadsheet_id: str) -> list[tuple[int, str, int]]:
        """Return ``(sheet_id, title, grid_size)`` for each tab in a spreadsheet.

        ``grid_size`` is the tab's current row count, which tells the caller
        whether the tab is empty and how large a clear is needed.
        """
        # The field mask must stop at ``gridProperties``. Selecting a field
        # *inside* that sub-object (``gridProperties.rowCount``) is a third
        # level of nesting, which the Sheets API rejects with
        # 400 INVALID_ARGUMENT. Requesting the whole sub-object is both valid
        # and cheaper than fetching the entire spreadsheet resource.
        payload = self._request(
            "GET",
            f"{SHEETS_BASE_URL}/{spreadsheet_id}",
            params={"fields": "sheets.properties(sheetId,title,gridProperties)"},
        )
        sheets: list[tuple[int, str, int]] = []
        for sheet in payload.get("sheets", []) or []:
            props = sheet.get("properties", {})
            sheet_id = props.get("sheetId")
            title = props.get("title")
            if sheet_id is None or not title:
                continue
            grid = props.get("gridProperties", {}) or {}
            sheets.append((int(sheet_id), title, int(grid.get("rowCount", 0))))
        return sheets

    def verify_access(self) -> str:
        """Return the signed-in user's email address, or ``""`` if unknown.

        Purely cosmetic: it lets the settings dialog say *which* account is
        connected. Reading the address needs the ``userinfo.email`` scope, which
        this add-on does not request — doing so would mean an extra consent
        prompt for no functional benefit. So a 401 here is expected and must be
        treated as "unknown", not as a failure. A successful Drive or Sheets
        call is the real proof that the token works.
        """
        try:
            response = self._session.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers=self._headers(),
                timeout=DEFAULT_TIMEOUT,
            )
        except requests.RequestException:
            return ""

        if response.status_code >= 400:
            return ""
        try:
            payload = response.json()
        except ValueError:
            return ""

        email = payload.get("email", "") if isinstance(payload, dict) else ""
        return email if isinstance(email, str) else ""

    # -- Values ------------------------------------------------------------

    def read_range(self, spreadsheet_id: str, a1_range: str) -> list[list[str]]:
        """Read a tab's cells, returning rows of strings.

        A completely empty range yields ``[]``.
        """
        payload = self._request(
            "GET",
            f"{SHEETS_BASE_URL}/{spreadsheet_id}/values/{a1_range}",
        )
        values = payload.get("values", [])
        if not isinstance(values, list):
            return []
        return [[("" if cell is None else str(cell)) for cell in row] for row in values]

    def clear_range(self, spreadsheet_id: str, a1_range: str) -> None:
        """Blank out a range without changing its formatting."""
        self._request(
            "POST",
            f"{SHEETS_BASE_URL}/{spreadsheet_id}/values/{a1_range}:clear",
            json={},
        )

    def update_range(
        self,
        spreadsheet_id: str,
        a1_range: str,
        values: list[list[str]],
    ) -> int:
        """Write ``values`` starting at ``a1_range``, returning cells updated.

        ``RAW`` input is used so that note field HTML is stored exactly as it
        appears in Anki instead of being interpreted as a formula or a date.
        """
        if not values:
            return 0

        params = {"valueInputOption": "RAW"}
        updated = 0
        chunk: list[list[str]] = []
        cells = 0

        for row in values:
            width = len(row)
            if chunk and cells + width > MAX_CELLS_PER_WRITE:
                updated += self._write_chunk(
                    spreadsheet_id, a1_range, chunk, params
                )
                chunk = []
                cells = 0
            chunk.append(row)
            cells += width

        if chunk:
            updated += self._write_chunk(spreadsheet_id, a1_range, chunk, params)

        return updated

    def write_guids(
        self,
        spreadsheet_id: str,
        sheet_title: str,
        first_data_row_number: int,
        guids: dict[int, str],
        guid_column_index: int = 0,
    ) -> int:
        """Write newly-assigned guids back into the sheet's guid column.

        ``guids`` maps a zero-based data-row index (excluding the header) to the
        guid Anki created for it. ``first_data_row_number`` is the 1-based sheet
        row of the first data row (2 when the header is in row 1).
        ``guid_column_index`` is the zero-based column containing the guid header.

        Only the individual guid cells are written, so a concurrent edit to a
        field column in the meantime is not clobbered. Every range is fully
        qualified with the tab name: a bare ``A2`` would be resolved against the
        wrong scope, and Google can answer 200 while writing nothing.
        """
        if not guids:
            return 0

        base = tab_a1_range(sheet_title)
        requests_: list[dict[str, Any]] = []

        column = _column_letter(guid_column_index + 1)
        for index, guid in sorted(guids.items()):
            cell = f"{base}!{column}{first_data_row_number + index}"
            requests_.append({"range": cell, "values": [[guid]]})

        payload = self._request(
            "POST",
            f"{SHEETS_BASE_URL}/{spreadsheet_id}/values:batchUpdate",
            json={"valueInputOption": "RAW", "data": requests_},
        )
        return int(payload.get("totalUpdatedCells", 0) or 0)

    def _write_chunk(
        self,
        spreadsheet_id: str,
        a1_range: str,
        values: list[list[str]],
        params: dict[str, str],
    ) -> int:
        payload = self._request(
            "PUT",
            f"{SHEETS_BASE_URL}/{spreadsheet_id}/values/{a1_range}",
            params=params,
            json={"range": a1_range, "majorDimension": "ROWS", "values": values},
        )
        return int(payload.get("updatedCells", 0) or 0)


def _describe_error(payload: dict[str, Any], status: int) -> str:
    """Turn a Google API error body into a single readable line.

    Google's 400 responses are frequently uninformative ("Request contains an
    invalid argument"), so the per-error ``location``/``field`` detail is
    included when present — that is usually the only clue about *which*
    parameter was rejected.
    """
    error = payload.get("error", {})
    if not isinstance(error, dict):
        return f"Google API request failed with HTTP {status}."

    message = str(error.get("message", "")).strip()
    status_text = str(error.get("status", "")).strip()

    if not message:
        return f"Google API request failed with HTTP {status}."

    detail = ""
    details = error.get("errors")
    if isinstance(details, list) and details:
        first = details[0]
        if isinstance(first, dict):
            where = " ".join(
                str(first[key]) for key in ("location", "field") if first.get(key)
            )
            if where:
                detail = f" (in {where})"

    prefix = f"Google API error ({status})"
    if status_text:
        prefix += f" {status_text}"
    return f"{prefix}: {message}{detail}"


def _column_letter(column_number: int) -> str:
    """Convert a one-based column number to its Google Sheets A1 letters."""
    letters = ""
    while column_number:
        column_number, remainder = divmod(column_number - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def tab_a1_range(sheet_title: str) -> str:
    """Return an A1 range covering a whole tab.

    The tab title is single-quoted and internal apostrophes are doubled, as
    required by A1 notation, so titles containing spaces or quotes work.
    """
    escaped = sheet_title.replace("'", "''")
    return f"'{escaped}'"
