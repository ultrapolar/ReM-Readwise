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
- **Downloading Reader PDFs** uses each document's `source_url`. PDFs saved from
  a public URL download directly; Readwise-hosted uploads are fetched with your
  token. If a document exposes no retrievable source, it is logged and skipped.
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
