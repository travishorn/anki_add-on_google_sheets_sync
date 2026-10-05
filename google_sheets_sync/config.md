# Google Sheets Sync — Configuration

This add-on syncs Anki note types to tabs in Google Sheets.

## Setup

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and
   create (or pick) a project.
2. Enable the **Google Sheets API** and the **Google Drive API** for that project.
3. Configure the **OAuth consent screen** and add yourself as a test user.
4. Create an **OAuth client** of type **Desktop app**.
5. Open **Tools → Google Sheets Sync** and press **Connect**.
6. Paste the **Client ID** and **Client secret** into the two fields, then press
   **OK**. Nothing happens yet at this point — cancelling does nothing at all.
7. A Google sign-in link then appears in a new window. Press **Open in
   browser**, or **Copy link** and paste it into any browser yourself. The link
   is never opened automatically.

Until you are connected, the connections list stays disabled; it comes alive as
soon as sign-in succeeds. The **Google account** section then offers
**Disconnect** instead of **Connect**.

## Connections

A _connection_ links one Anki note type to one tab in one Google spreadsheet.
Several connections can exist at once, so different note types can be pushed to
different sheets. The spreadsheet and tab are chosen with a picker, and the
choice is remembered so that pushing again needs a single click.

Every connection in the list carries its own **Push**, **Pull**, and **Remove**
buttons, so an action always applies to the connection it sits next to — there is
no row to select first. The **Push all** and **Pull all** buttons process every
connection and show one combined result when all work is complete. Removing a
connection asks for confirmation; it only forgets the link, and neither your
notes nor the spreadsheet are touched.

## How pushing works

Pushing **replaces** the contents of the target tab. The add-on clears the tab
and rewrites it from Anki, so the sheet always mirrors the collection exactly.
Anything typed into the sheet by hand will be lost on the next push — edit the
note in Anki and push again instead.

The first row is a header:

```
guid | notetype | deck | <your field names...> | tags
```

- `guid` is Anki's stable identifier for the note. It is how a pull matches a
  sheet row back to a note, so **do not edit it**.
- `deck` lists every deck the note's cards are in, comma-separated. A single
  note can generate cards in several decks.
- Field contents are written **raw**, so any HTML in a field is stored exactly
  as it appears in Anki rather than being converted to display text.

## Pulling, and adding notes from the sheet

Pulling matches each row to a note by its `guid` cell:

- **Matching guid** — the note's fields and tags are updated from the row.
- **Blank or unrecognised guid** — the row is treated as a **new** note. So you
  can add cards by appending rows in the sheet, leaving the `guid` cell empty.

When a new note is created, Anki assigns its `guid`, and the add-on writes that
value back into the row's `guid` cell so the next pull updates the note instead
of creating a duplicate. You may leave `guid` blank when adding rows; the add-on
fills it in for you.

A brand-new note is placed in the deck named in the row's `deck` cell, and that
deck is created if it does not exist. Because the cell can hold several
comma-separated decks (for notes that already have cards in more than one), only
the first is used when creating a note.

Rows with no content at all are ignored, so spare rows from the sheet's
formatting do not become blank notes.
