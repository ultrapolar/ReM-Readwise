# rem-readwise

Two-way sync between **Readwise Reader** and a **reMarkable** tablet.

- 📥 Your Reader **PDFs** are pushed onto the reMarkable so you can read and
  highlight them on the device.
- ✍️ Highlights you make on the reMarkable are pulled back and recreated as
  **Readwise highlights**, anchored to the right page of the original document —
  so they show up in Reader (and flow into Readwise reviews/exports) just like
  highlights made anywhere else.

It runs as an always-on Docker service that performs a full sync on an interval.

```
 Readwise Reader                         reMarkable
 ┌───────────────┐   list + download    ┌───────────────┐
 │   PDFs        │ ───────────────────▶ │  /Readwise/    │
 │               │      (rmapi put)     │   your PDFs    │
 │               │                      │      │         │
 │               │                      │   highlight    │
 │  highlights   │ ◀─────────────────── │   on device    │
 └───────────────┘  text + page number  └───────────────┘
                    (rmscene + API)
```

## How it works

| Stage | What happens | Tech |
| --- | --- | --- |
| **Forward** | List Reader documents (`GET /api/v3/list/`), download each PDF, upload to a folder on the reMarkable. | Readwise Reader API + [`rmapi`](https://github.com/ddvk/rmapi) |
| **Highlight** | You highlight text on the device. The reMarkable stores the highlighted **text + color + position** as `GlyphRange` items in the page's `.rm` file. | reMarkable |
| **Reverse** | Download the annotated document, parse the highlights, recover the PDF **page number** for each, and create them in Readwise (`POST /api/v2/highlights/`) with `location_type=page`. | [`rmscene`](https://github.com/ricklupton/rmscene) + Readwise API |

State is tracked in a small JSON file so the sync is **idempotent**: each PDF is
uploaded once, and each highlight is pushed once (deduped by document + page +
normalized text), no matter how often the service runs.

## Quick start

### 1. Get your credentials

- **Readwise token** — https://readwise.io/access_token
- **reMarkable pairing code** — https://my.remarkable.com/device/desktop/connect
  (an 8-character one-time code; it expires quickly, so grab it right before
  step 3)

### 2. Configure

```bash
cp .env.example .env
# edit .env and set READWISE_TOKEN=...
```

### 3. Pair the reMarkable (one time)

```bash
docker compose run --rm rem-readwise auth-remarkable <one-time-code>
```

This writes a long-lived token to `./data/rmapi.conf`. Because `./data` is a
mounted volume, you never have to pair again.

### 4. Check everything is wired up

```bash
docker compose run --rm rem-readwise auth-check
```

### 5. Run the service

```bash
docker compose up -d
```

It now syncs every `SYNC_INTERVAL_SECONDS` (default 15 minutes). Watch it work:

```bash
docker compose logs -f
```

## Configuration

All settings come from environment variables (see [`.env.example`](.env.example)):

| Variable | Default | Description |
| --- | --- | --- |
| `READWISE_TOKEN` | — | **Required.** Readwise access token. |
| `READWISE_CATEGORY` | `pdf` | Which Reader category to send to the device. |
| `READWISE_LOCATION` | _(all)_ | Optional Reader location filter (`new`, `later`, `archive`, …). |
| `REMARKABLE_FOLDER` | `Readwise` | Folder on the tablet where PDFs are placed. |
| `SYNC_INTERVAL_SECONDS` | `900` | Seconds between full sync cycles. |
| `RMAPI_CONFIG` | `/data/rmapi.conf` | reMarkable token file (keep on a volume). |
| `STATE_PATH` | `/data/state.json` | Sync state (uploaded docs + pushed highlights). |
| `WORK_DIR` | `/data/work` | Scratch space for downloads. |
| `INBOX_DIR` | `/data/inbox` | Drop local PDFs here to push them to the device (see below). |
| `DRY_RUN` | `0` | `1` logs intended actions without changing anything. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

## CLI

The same commands are available without Docker if you install the package
(`pip install .`, plus the `rmapi` binary on your `PATH`):

```bash
rem-readwise auth-remarkable <code>   # pair with reMarkable Cloud
rem-readwise auth-check               # verify both credentials
rem-readwise sync                     # run one full cycle and exit
rem-readwise sync --dry-run           # show what would happen
rem-readwise run                      # run the forever loop (Docker default)
rem-readwise status                   # counts of synced docs / highlights
```

## Notes, scope & limitations

- **Reverse direction is at page granularity.** Each highlight is sent with its
  PDF page number (`location_type=page`) and its exact text. Readwise uses the
  text to anchor the snippet within that page, which is how it lands "in the
  right place" in Reader. Sub-page coordinate anchoring is not part of the
  public API.
- **Highlighted text must exist as a text layer.** reMarkable can only capture
  the underlying characters when the PDF has selectable text. Highlights drawn
  on a scanned/image-only PDF have no text and are skipped. Most Readwise Reader
  PDFs have a text layer.
- **Downloading Reader PDFs** uses each document's `source_url`:
  - PDFs you **saved from a public URL** download directly — these just work.
  - PDFs you **uploaded** into Reader (drag-and-drop / the `U` dialog) are the
    hard case: Readwise's public API exposes **no per-document file download**,
    and `source_url` for an upload is often empty or points at an HTML page. The
    bytes do exist server-side (Reader can *"Export Full Files and Articles"* as
    a ZIP), but there's no documented API to fetch one file.
  - The downloader therefore **validates** what it gets: if a `pdf` document's
    `source_url` doesn't return real PDF bytes, the doc is **skipped** (counted
    as "unretrievable") and left unmarked so it retries later — we never push a
    broken file to the device.

  **Workarounds for uploaded PDFs** (pick based on your workflow):
  1. **Inbox mode (recommended)** — drop the PDF into `INBOX_DIR` and the tool
     pushes it to the reMarkable itself, so highlights round-trip normally. See
     [Inbox mode](#inbox-mode-uploaded-pdfs) below.
  2. *Save by URL instead of uploading* — if a PDF lives at a public URL, save
     it to Reader from that URL; `source_url` is then set and sync just works.
  3. Ask Readwise to add a per-document file endpoint; the moment they do, this
     path becomes fully automatic.

### Inbox mode (uploaded PDFs)

For PDFs you uploaded into Reader (which Reader won't serve back), drop the
original file into the inbox folder instead:

```bash
cp ~/Downloads/some-paper.pdf ./data/inbox/
```

On the next cycle the tool uploads it to your reMarkable folder and registers it
in the sync state. Highlight it on the device and the highlights flow back to
Readwise titled after the file (e.g. `some-paper`). Because the tool is the
uploader, the on-device PDF is byte-identical to your file, so page numbers line
up exactly. Each file is uploaded once (tracked by name in the state).
- **Device names are kept unique.** The reMarkable document name is the only
  link from an annotated file back to its Reader document, so two documents
  that sanitize to the same name ("Notes", the same paper saved twice, an inbox
  file matching a Reader title) get `Notes`, `Notes (2)`, `Notes (3)`... on the
  device. The Readwise title is unaffected. If an older `state.json` already
  maps one name to several documents, that document is skipped with a warning
  rather than guessed at; remove the stale entries from `state.json` to fix it.
- **reMarkable software 3.x (`.rm` v6)** is the supported on-device format.
- This uses the **unofficial** reMarkable Cloud API via `rmapi`. Keep backups of
  important documents.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check .
pytest
```

The pure logic (highlight extraction, page mapping, dedup, payload building,
state) is covered by unit tests that need neither network nor a device. The
network-facing Readwise client is tested with mocked HTTP (`respx`).

## License

MIT — see [LICENSE](LICENSE).
