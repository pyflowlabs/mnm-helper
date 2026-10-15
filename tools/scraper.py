#!/usr/bin/env python3
"""
Scraper framework for Monsters & Memories community sources.

Design goals:
  * zero third-party dependencies (stdlib urllib + json),
  * polite: a real User-Agent, a delay between requests, on-disk cache,
  * safe: dry-run by default; only writes data.json with --write,
  * extensible: add a source by writing one function that returns a list
    of entries in our schema; merge() dedupes against what's already there.

What is implemented and working:
  * a cached, rate-limited fetcher,
  * a MediaWiki client (the miraheze wiki is MediaWiki): list the pages in
    a category and pull a page's wikitext,
  * merge-with-dedupe into data/data.json, tagging source entries
    verified="wiki".

What is a deliberate stub (marked TODO):
  * turning a specific wiki page into our exact item/quest/mob fields.
    That parsing differs per page type; fill it in per source as needed.
    The framework around it is done.

Examples:
    python tools/scraper.py categories                 # list wiki categories
    python tools/scraper.py pages "Items"              # list pages in a category
    python tools/scraper.py wikitext "Pasha's Blade"   # dump one page's source
    python tools/scraper.py import-items "Items" --write
"""
import argparse
import json
import pathlib
import sys
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "data.json"
CACHE = ROOT / "tools" / ".cache"

UA = "MnM-Helper/1.0 (personal community tool; contact: local)"
DELAY = 1.0  # seconds between live requests

# MediaWiki endpoint from data.json meta.sources
WIKI_API = "https://monstersandmemories.miraheze.org/w/api.php"


# ----------------------------- fetching ------------------------------------
def _cache_path(key):
    safe = "".join(c if c.isalnum() else "_" for c in key)[:120]
    return CACHE / (safe + ".cache")


def fetch(url, *, use_cache=True):
    """GET a URL politely, with on-disk caching. Returns text."""
    cp = _cache_path(url)
    if use_cache and cp.exists():
        return cp.read_text(encoding="utf-8")
    time.sleep(DELAY)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        text = r.read().decode("utf-8", "replace")
    CACHE.mkdir(exist_ok=True)
    cp.write_text(text, encoding="utf-8")
    return text


def api(params):
    params = {**params, "format": "json"}
    return json.loads(fetch(WIKI_API + "?" + urllib.parse.urlencode(params)))


# --------------------------- MediaWiki client ------------------------------
def list_categories(limit=500):
    out, cont = [], {}
    while True:
        r = api({"action": "query", "list": "allcategories", "aclimit": "500", **cont})
        out += [c["*"] for c in r["query"]["allcategories"]]
        if "continue" in r and len(out) < limit:
            cont = r["continue"]
        else:
            break
    return out


def pages_in_category(cat, limit=500):
    title = cat if cat.lower().startswith("category:") else "Category:" + cat
    out, cont = [], {}
    while True:
        r = api({"action": "query", "list": "categorymembers",
                 "cmtitle": title, "cmlimit": "500", **cont})
        out += [m["title"] for m in r["query"]["categorymembers"]]
        if "continue" in r and len(out) < limit:
            cont = r["continue"]
        else:
            break
    return out


def page_wikitext(title):
    r = api({"action": "query", "prop": "revisions", "rvprop": "content",
             "rvslots": "main", "titles": title})
    pages = r["query"]["pages"]
    page = next(iter(pages.values()))
    try:
        return page["revisions"][0]["slots"]["main"]["*"]
    except (KeyError, IndexError):
        return ""


# --------------------------- schema extraction -----------------------------
def parse_item_page(title, wikitext):
    """
    TODO: turn a wiki item page into our item schema, e.g.
        {"name":..., "slot":..., "stats":..., "source":..., "verified":"wiki"}
    Wiki pages use infobox templates ({{Item|slot=...|ac=...}}); parse those
    fields here. Returns a dict or None. Left as a stub on purpose so the
    scraper never invents data.
    """
    return None


# ------------------------------- merging -----------------------------------
def _norm(s):
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def title_of(obj):
    for f in ("name", "song", "ability", "item", "skill", "topic", "class"):
        if obj.get(f) is not None:
            return str(obj[f])
    return None


def merge(category, entries, write=False):
    data = json.loads(DATA.read_text(encoding="utf-8"))
    existing = data.setdefault(category, [])
    seen = {_norm(title_of(o)) for o in existing if title_of(o)}
    added = [e for e in entries if _norm(title_of(e)) not in seen and title_of(e)]
    print(f"{category}: {len(existing)} existing, {len(entries)} scraped, {len(added)} new")
    for e in added:
        print("  + " + title_of(e))
    if write and added:
        existing.extend(added)
        DATA.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {len(added)} new entries to {DATA}")
    elif not write:
        print("(dry-run — pass --write to save)")


# -------------------------------- CLI --------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("categories", help="list wiki categories")
    p = sub.add_parser("pages", help="list pages in a wiki category"); p.add_argument("category")
    p = sub.add_parser("wikitext", help="dump a page's wikitext"); p.add_argument("title")
    p = sub.add_parser("import-items", help="scrape an item category into data.json")
    p.add_argument("category"); p.add_argument("--write", action="store_true")
    args = ap.parse_args()

    if args.cmd == "categories":
        for c in list_categories():
            print(c)
    elif args.cmd == "pages":
        for t in pages_in_category(args.category):
            print(t)
    elif args.cmd == "wikitext":
        print(page_wikitext(args.title))
    elif args.cmd == "import-items":
        titles = pages_in_category(args.category)
        entries = []
        for t in titles:
            e = parse_item_page(t, page_wikitext(t))
            if e:
                e.setdefault("verified", "wiki")
                entries.append(e)
        if not entries:
            print("no entries parsed — fill in parse_item_page() to extract fields.")
        merge("items", entries, write=args.write)


if __name__ == "__main__":
    main()
