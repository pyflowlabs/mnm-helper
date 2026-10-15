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
import html
import json
import pathlib
import re
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
#
# The miraheze wiki describes each item with an {{ItemBox}} infobox template
# and an {{Itempage}} block for where it comes from. We parse those two
# templates and map only the fields that are actually filled in — a blank
# field on the wiki stays absent in our data; we never guess a value.
#
TRUTHY = {"true", "yes", "1"}

# attribute / regen / resist / instrument fields and how we label them.
# (wiki_key, our_label, is_percent)
_STAT_FIELDS = [
    ("str", "STR", False), ("sta", "STA", False), ("agi", "AGI", False),
    ("dex", "DEX", False), ("int", "INT", False), ("wis", "WIS", False),
    ("cha", "CHA", False),
    ("hp", "HP", False), ("mana", "MANA", False),
    ("hp_regen", "HP Regen", False), ("mana_regen", "Mana Regen", False),
    ("haste", "Haste", True),
    ("mr", "MR", False), ("fr", "FR", False), ("cr", "CR", False),
    ("pr", "PR", False), ("dr", "DR", False), ("cor", "COR", False),
    ("er", "ER", False), ("hr", "HR", False),
    ("brass", "Brass", True), ("percussion", "Percussion", True),
    ("singing", "Singing", True), ("stringed", "Stringed", True),
    ("wind", "Wind", True),
]
# boolean infobox flags -> our flag tokens
_FLAG_FIELDS = [("magic", "MAGIC"), ("lore", "LORE"), ("unique", "UNIQUE"),
                ("nodrop", "NODROP"), ("norent", "NORENT")]


def _find_template(wikitext, name):
    """Return the raw body of the first {{name ...}} template, or None.

    Uses brace-depth counting so nested templates inside the body don't
    terminate it early.
    """
    start = wikitext.find("{{" + name)
    if start == -1:
        return None
    i, depth, n = start + 2, 1, len(wikitext)
    while i < n and depth > 0:
        two = wikitext[i:i + 2]
        if two == "{{":
            depth += 1; i += 2
        elif two == "}}":
            depth -= 1; i += 2
        else:
            i += 1
    # body is everything after the template name, up to the closing "}}"
    return wikitext[start + 2 + len(name):i - 2]


def _parse_fields(body):
    """Parse a template body's `| key = value` pairs into a dict.

    Splits on newline-pipe boundaries so multi-line values (e.g. flavour
    text) survive intact. Keys are lower-cased; values are stripped.
    """
    fields = {}
    for part in re.split(r"\n\s*\|", "\n" + body):
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        key = key.strip().lower()
        if key:
            fields[key] = val.strip()
    return fields


def _clean(v):
    """Strip wiki/HTML markup from a text value: some names and descriptions
    wrap letters in <span> styling tags. Returns plain, unescaped text."""
    v = re.sub(r"<[^>]+>", "", v or "")   # drop HTML tags
    v = html.unescape(v)
    return re.sub(r"\s+", " ", v).strip()


def _int(v):
    v = (v or "").strip()
    try:
        return int(float(v))
    except ValueError:
        return None


def _float(v):
    v = (v or "").strip()
    try:
        return float(v)
    except ValueError:
        return None


def _signed(v):
    v = v.strip()
    return v if v.startswith(("+", "-")) else "+" + v


def _build_stats(f):
    parts = []
    for key, label, pct in _STAT_FIELDS:
        v = f.get(key, "").strip()
        if not v or _float(v) is None or _float(v) == 0:
            continue
        parts.append(f"{label} {_signed(v)}" + ("%" if pct else ""))
    return ", ".join(parts)


def _build_flags(f):
    return " ".join(tok for key, tok in _FLAG_FIELDS
                    if f.get(key, "").strip().lower() in TRUTHY)


def _item_source(wikitext):
    """Build a 'where it comes from' string from the {{Itempage}} block.

    We only read the confirmed `dropsfrom` field: the first [[link]] is the
    zone, any following ones are the mobs. If it isn't present we return
    None rather than guess an acquisition method.
    """
    body = _find_template(wikitext, "Itempage")
    if body is None:
        return None
    drops = _parse_fields(body).get("dropsfrom", "")
    links = [l.strip() for l in re.findall(r"\[\[([^\]|]+)", drops) if l.strip()]
    if not links:
        return None
    zone, mobs = links[0], links[1:]
    return f"{', '.join(mobs)} ({zone})" if mobs else zone


def parse_item_page(title, wikitext):
    """Turn a wiki item page into our item schema, or None.

    Reads the {{ItemBox}} infobox and maps only the filled-in fields onto
    our schema (name, slot, dmg, delay, ac, stats, effect, flags, weight,
    size, classes, race, source). Pages without an ItemBox (categories,
    redirects, stubs) return None so the scraper never invents data.
    """
    body = _find_template(wikitext, "ItemBox")
    if body is None:
        return None
    f = _parse_fields(body)
    name = _clean(f.get("item_name", "")) or title.strip()
    if not name:
        return None

    entry = {"name": name}
    if f.get("slot"):
        entry["slot"] = f["slot"].strip()
    for key in ("dmg", "delay", "ac"):
        n = _int(f.get(key))
        if n is not None and n != 0:
            entry[key] = n

    stats = _build_stats(f)
    if stats:
        entry["stats"] = stats

    effects = [_clean(f.get(k, "")) for k in ("effect", "effect1", "effect2", "effect3")]
    effects = [e for e in effects if e]
    if effects:
        entry["effect"] = ", ".join(effects)

    flags = _build_flags(f)
    if flags:
        entry["flags"] = flags

    weight = _float(f.get("weight"))
    if weight is not None and weight != 0:
        entry["weight"] = weight
    if f.get("size"):
        entry["size"] = f["size"].strip().upper()
    if f.get("class"):
        entry["classes"] = f["class"].strip()
    if f.get("race"):
        entry["race"] = f["race"].strip()

    desc = _clean(f.get("item_stats", ""))
    if desc:
        entry["desc"] = desc

    source = _item_source(wikitext)
    if source:
        entry["source"] = source

    entry["verified"] = "wiki"
    return entry


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
    added = []
    for e in entries:
        key = _norm(title_of(e))
        if not title_of(e) or key in seen:
            continue   # skip dupes against existing data AND within this batch
        seen.add(key)
        added.append(e)
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
    p = sub.add_parser("import-items", help="scrape item categories into data.json")
    p.add_argument("category", nargs="+"); p.add_argument("--write", action="store_true")
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
        titles, seen = [], set()
        for cat in args.category:
            for t in pages_in_category(cat):
                if t.lower().startswith("category:") or t in seen:
                    continue
                seen.add(t)
                titles.append(t)
        print(f"{len(titles)} unique pages across {len(args.category)} categor"
              f"{'y' if len(args.category) == 1 else 'ies'}", file=sys.stderr)
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
