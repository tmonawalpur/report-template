# वार्षिक समीक्षा — Annual Review Deck (Flask + SQLite)

An interactive Nepali annual-review slide deck with a Flask/SQLite backend.
The presentation is a single HTML page (Chart.js) that reads and writes its
content through a small REST API backed by SQLite. Opened directly as a file
(without the backend) it still works stand-alone via `localStorage`.

## Run

```bash
pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:5000> for the presentation, or
<http://127.0.0.1:5000/admin> for the admin panel. On first run the database
(`deck.db`) is created and seeded from `seed_deck.json`.

The toolbar shows a **● सर्भर** pill when the page is talking to the API, or
**○ अफलाइन** when running stand-alone from `file://`.

## Database & deploy (Railway)

The app chooses its database from the environment (`app.py` → `_database_uri()`):

- **`DATABASE_URL` set** → use that managed database. A `postgres://` /
  `postgresql://` URL is rewritten to `postgresql+psycopg2://` automatically.
- **not set** (local dev) → a SQLite file, `deck.db`, overridable with
  `DATABASE_PATH`.

Use a managed PostgreSQL database in production. Its data lives **outside** the
container, so — unlike the local SQLite file — it survives every deploy. (The
container filesystem on Railway is ephemeral and rebuilt from the repo on each
deploy; `*.db` files are not committed, so never rely on SQLite there.)

**Railway setup:**

1. Add a **PostgreSQL** database to the project (New → Database → PostgreSQL).
2. In the web service's **Variables**, reference the DB URL — add
   `DATABASE_URL = ${{Postgres.DATABASE_URL}}` (Railway exposes the Postgres
   service's connection string as a variable).
3. Set a real **`SECRET_KEY`** (defaults to `dev-secret-change-me`; it signs
   login sessions, so change it in production).
4. Deploy. On first boot `db.create_all()` builds the tables and
   `seed_demo_if_empty()` seeds the demo deck from `seed_deck.json`.

`psycopg2-binary` is in `requirements.txt` for the Postgres driver. No schema
changes are needed — the `Text` (JSON) and `LargeBinary` (image) columns map to
unlimited `text` / `bytea` on PostgreSQL.

> **Migrating existing server data:** if a previous SQLite deployment already
> holds real data, dump it and load it into the new Postgres database before (or
> right after) switching `DATABASE_URL` — the two stores are separate, so simply
> setting the variable starts Postgres empty (freshly seeded).

## Data model (SQLAlchemy)

| Model  | Purpose |
|--------|---------|
| `Meta`  | Singleton row (`id=1`) with the deck header text (province, ministry, office, fiscal year, report title). |
| `Slide` | One row per slide, ordered by `position`. Core fields `type` / `title` are columns; all type-specific data (`paras`, `items`, `head`, `rows`, `chart`, …) is stored as JSON in `content`. |

This hybrid keeps the schema flexible (any slide shape) while positions, types
and ordering stay first-class and queryable.

## Presentation view

- **Fullscreen** (⛶ button) hides the top toolbar and uses the whole screen via
  CSS `:fullscreen` rules; the stage rescales automatically. Press **Esc** to exit.
- **Title-slide logo**: the front page shows the government emblem from
  `static/emblem.png` (the Emblem of Nepal), placed just above the province name.
  To use a different logo, replace that file — any square-ish PNG works; if it's
  missing the slide falls back to a placeholder icon.

## Admin panel (`/admin`)

A dedicated CMS page (`admin.html`) for editing the deck outside the slideshow:

- **Report metadata** — edit province, ministry, office, fiscal year, title.
- **Slides table** — every slide listed with type, title and a summary; add a
  slide of any type, and per-row **edit / duplicate / delete / move up-down**.
- **Slide editor (modal)** — a form tailored to each slide type:
  - *list*: add/remove/reorder bullet items, columns, big-font, start number
  - *intro*: title, photo caption, add/remove paragraphs
  - *table* / *twotable*: grid editor — edit any cell, add/remove rows and
    (for single-header tables) columns; toggle compact / total row / caption
  - *chart*: type, sub-title, stacked/horizontal/datalabels/dual-axis, plus a
    data grid to edit labels & series values and add/remove labels & datasets;
    tables can gain or drop an attached chart
  - *thanks*: the closing text

Everything is backed by the REST API below. Open it from the presentation
toolbar (**🗄 एडमिन**) or at `/admin`.

## REST API

| Method & path | Action |
|---|---|
| `GET /admin` | The admin-panel page. |
| `GET /api/deck` | Full deck: `{ meta, slides:[…] }` (each slide carries its DB `_id`). |
| `PUT /api/meta` | Update header metadata. |
| `POST /api/slides` | Add a slide. Body: `{ after:<id|null>, slide:{…} }`. |
| `PUT /api/deck` | **Save all**: bulk-update meta + every slide in one transaction (preserves ids, re-applies order, drops removed slides). |
| `PUT /api/slides/<id>` | Update a slide (title + content). |
| `DELETE /api/slides/<id>` | Delete a slide (positions re-packed). |
| `POST /api/slides/<id>/duplicate` | Duplicate a slide after itself. |
| `POST /api/slides/reorder` | Body: `{ order:[id,…] }` — set new order. |
| `POST /api/import` | Replace the whole deck with a posted deck JSON. |
| `POST /api/reset` | Reseed the deck from `seed_deck.json`. |

Text edits and chart-data edits in the UI are debounced and saved with
targeted `PUT`s; structural changes (add/duplicate/delete/move) hit the
matching endpoint and then re-sync from the server.

### Saving

Editing autosaves (debounced) to the database, and pending edits are flushed
before any structural change or when the page is hidden/closed. The toolbar
also has an explicit **🗄 डाटाबेसमा सेभ** button (shortcut **Ctrl/⌘+S**) that
force-commits the whole deck via `PUT /api/deck` in one atomic request. The
button shows status: **✔ सेभ भयो** (saved), a pulsing amber **🗄 सेभ गर्नुहोस् •**
(unsaved changes), or **⏳ सेभ हुँदै…** while saving. Offline it saves to
`localStorage` instead.

## Advanced charts

Charts are configured per slide under `slide.chart` and rendered with Chart.js
+ the datalabels plugin. In edit mode (✏️) each chart shows a control strip:

- **Type**: bar, line, area (gradient-filled), radar, doughnut, pie, polar area
- **स्ट्याक** — stacked axes
- **तेर्सो** — horizontal bars
- **लेबल** — value labels on points/segments

Extras: vertical gradient fills, dual-axis support (`dualAxis` + per-dataset
`axis:"y1"`), mixed charts (per-dataset `type`), and multi-colour single-series
pie/doughnut. All chart-config changes persist through the API.

## Files

```
app.py            Flask app: models, REST API, static serving, seeding
seed_deck.json    Original deck content (seed for the database)
presentation.html Frontend (served at /, also works stand-alone)
admin.html        Admin panel / CMS (served at /admin)
static/emblem.png Government emblem shown on the title slide (swap to rebrand)
requirements.txt  Python dependencies
railway.json      Railway deploy config (build + gunicorn start command)
deck.db           SQLite database (created at runtime; not committed — see Deploy)
```

> **Note:** the dev server runs with `use_reloader=False`. The Flask auto-reloader
> spawns a child process on file changes and can leave orphaned servers bound to
> the port (two servers on 5000 → inconsistent reads). Restart `python app.py`
> manually after editing `app.py`.
