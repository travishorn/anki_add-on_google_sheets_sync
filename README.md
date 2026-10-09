# Anki Add-on: Google Sheets Sync

Google Sheets Sync links Anki note types to tabs in Google spreadsheets. Use it
to publish notes from Anki to a sheet, edit or add notes in the sheet, and pull
those changes back into Anki.

> **Warning:** Pushing replaces the target tab's contents. Any values entered
> there are overwritten. Pulling can update existing notes and create new ones,
> so review sheet edits before pulling.

## Install

This repository does not currently provide a packaged AnkiWeb download. To
install from source:

1. Close Anki.
2. Copy the `google_sheets_sync` folder from this repository into Anki's
   `addons21` folder. Keep the folder name `google_sheets_sync`.
3. Start Anki. Open **Tools → Google Sheets Sync**.

To find Anki's add-ons folder, use **Tools → Add-ons → View Files** in Anki,
then close Anki before copying the add-on.

## Set up Google access

The add-on uses Google's Sheets and Drive APIs and signs in with your own OAuth
client. Follow the detailed instructions in [Configuration and
usage](google_sheets_sync/config.md) to enable the APIs, create a Desktop OAuth
client, and connect your Google account. The consent screen may require your
account to be listed as a test user.

## Use the add-on

1. In Anki, open **Tools → Google Sheets Sync**, then choose **Connect** and
   complete Google sign-in.
2. Choose **Add connection…**, select an Anki note type, then select a
   spreadsheet and tab. Each note type can have one connection; you can create
   connections for other note types as well.
3. Use **Push** on a connection to replace that tab with the note type's current
   notes, or **Pull** to import sheet edits. **Push all** and **Pull all** run
   the action for every connection.
4. To add notes from Sheets, append rows with the matching columns and leave
   `guid` blank. Pull creates the notes and writes their GUIDs back to the sheet
   so later pulls update them instead of creating duplicates.

The sheet's header is `guid`, `notetype`, `deck`, `added`, the note type's
fields, and `tags`. Pull maps columns by header name, so extra or reordered
columns are safe. Keep existing GUIDs unchanged: they identify which Anki note
a row updates. See [Configuration and usage](google_sheets_sync/config.md) for
the full column behavior, examples, and important sync details.

## Development

The project uses [uv](https://docs.astral.sh/uv/) for its Python environment and
dependency management. From the repository root, run `uv sync` to install the
project and development dependencies. The package metadata requires Python 3.13
or later. Runtime dependencies provided by Anki itself are noted in
[pyproject.toml](pyproject.toml).
