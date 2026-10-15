# Monsters & Memories Helper

A small, shareable companion wiki for Monsters & Memories. One self-contained
HTML file holds the whole database — items, quests, crafting, vendors, bard
songs, named mobs, mechanics, builds and class guides — with full-text search,
source badges (in-game / official / wiki / derived), and a built-in updater so
everyone can pull the latest data from the master.

The database is just a JSON file, so it is easy to extend: add a field, add a
category, and the app renders it without any code changes.

## Links

- App (always current): https://pyflowlabs.github.io/mnm-helper/
- Update feed (paste into the app's Updates box): https://raw.githubusercontent.com/pyflowlabs/mnm-helper/main/manifest.json

## For players (clients)

Open the link above, or double-click a saved copy of `dist/MnM-Helper.html`.
Either way there's no install, it works offline, and it runs on any machine
with a browser.

- Type in the search box to find anything across every category.
- Use the source dropdown to show only in-game verified data, etc.
- Click **Updates**, paste the master's `manifest.json` link once, and hit
  **Check now**. The app downloads the newest data and remembers it. Next time
  just open Updates and check again.

## For the master (maintaining and publishing data)

Everything lives in `data/data.json`. Edit it by hand or let the scraper append
to it. Each entry carries a `verified` tag (`ingame`, `official`, `wiki`,
`derived`). Prices are strings like `6s 60cp`.

Workflow after changing the data:

    python tools/validate.py          # sanity-check before publishing
    python tools/build.py             # rebuild the single-file app + manifest

The first time, tell the build where clients will fetch the data from:

    python tools/build.py --data-url https://raw.githubusercontent.com/<you>/<repo>/main/data.json

`build.py` writes three things into `dist/`:

- `MnM-Helper.html` — the shareable app with the data embedded (share this once).
- `data.json` — the raw data clients download on update.
- `manifest.json` — version info clients check against.

Publish `data.json` and `manifest.json` to your host (a GitHub repo is the
simplest: its raw URLs send the right CORS headers, so the in-app updater can
read them directly). Bump happens automatically — every build gets a new,
sortable version number, and clients only download when yours is newer.

## Scraping

`tools/scraper.py` is a dependency-free framework for pulling data from the
community sources listed in `data.json` (`meta.sources`). It can already talk to
the Miraheze wiki (list categories, list pages, fetch page source) and merge new
entries into `data.json` with de-duplication, tagging them `verified: "wiki"`.

    python tools/scraper.py categories
    python tools/scraper.py pages "Items"
    python tools/scraper.py wikitext "Pasha's Blade"

Turning a specific wiki page into exact item/quest/mob fields is left as a
clearly marked function (`parse_item_page`) so the scraper never invents data —
fill it in per page type as you go. Scraped entries only hit `data.json` with
`--write`.

## Layout

    data/data.json      the whole database (source of truth)
    app/template.html   the app shell; build embeds the data into it
    tools/build.py      embed data -> dist/MnM-Helper.html + manifest
    tools/validate.py   checks the data before publishing
    tools/scraper.py    community-source scraper framework
    dist/               the built, shareable output
    manifest.json       current version (also copied into dist/)
