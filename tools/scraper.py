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
import hashlib
import html
import json
import pathlib
import re
import sys
import time
import urllib.error
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
    # The query string before the page title is ~114 chars, so a plain
    # truncation would collapse every title to its first few characters and
    # make unrelated pages share one cache file. Append a hash of the full
    # key so each distinct URL gets its own file; keep a readable prefix.
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    safe = "".join(c if c.isalnum() else "_" for c in key)[:80]
    return CACHE / (safe + "_" + digest + ".cache")


def fetch(url, *, use_cache=True, retries=4):
    """GET a URL politely, with on-disk caching. Returns text.

    Retries transient failures (5xx, timeouts, connection resets) with an
    increasing backoff so one hiccup on the wiki doesn't abort a long import.
    """
    cp = _cache_path(url)
    if use_cache and cp.exists():
        return cp.read_text(encoding="utf-8")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(retries + 1):
        time.sleep(DELAY)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == retries:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == retries:
                raise
        backoff = DELAY * (2 ** attempt)
        print(f"  retry {attempt + 1}/{retries} after {backoff:.0f}s: {url[-60:]}",
              file=sys.stderr)
        time.sleep(backoff)
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


def _iter_templates(wikitext, name):
    """Return the body of every {{name ...}} template in order.

    Brace-depth counting keeps nested templates from terminating a body
    early; a name-boundary check keeps {{Quest}} from matching {{QuestNote}}.
    """
    needle = "{{" + name
    bodies, start, n = [], 0, len(wikitext)
    while True:
        i = wikitext.find(needle, start)
        if i == -1:
            break
        after = wikitext[i + len(needle):i + len(needle) + 1]
        if after and after not in "|}\n\r\t {":   # e.g. {{Quest vs {{QuestNote
            start = i + len(needle)
            continue
        j, depth = i + 2, 1
        while j < n and depth > 0:
            two = wikitext[j:j + 2]
            if two == "{{":
                depth += 1; j += 2
            elif two == "}}":
                depth -= 1; j += 2
            else:
                j += 1
        bodies.append(wikitext[i + 2 + len(name):j - 2])
        start = j
    return bodies


def _find_template(wikitext, name):
    """Return the raw body of the first {{name ...}} template, or None."""
    bodies = _iter_templates(wikitext, name)
    return bodies[0] if bodies else None


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


# ---------------- shared wikitext helpers for prose pages ------------------
def _wikiclean(s):
    """Flatten a chunk of wikitext to plain, readable text.

    [[target|label]] -> label, [[target]] -> target, strips simple templates,
    bold/italic marks, HTML tags and entities, and collapses whitespace.
    """
    s = s or ""
    s = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]|]+)\]\]", r"\1", s)   # links -> label
    s = re.sub(r"\{\{[^{}]*\}\}", " ", s)                      # simple templates
    s = re.sub(r"'{2,}", "", s)                                # bold / italic
    s = re.sub(r"<[^>]+>", " ", s)                             # html tags
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _links(s):
    """Return the link labels in a chunk, skipping File:/Image:/Category:."""
    out = []
    for raw in re.findall(r"\[\[([^\]]+)\]\]", s or ""):
        if re.match(r"(?i)\s*(file|image|category):", raw):
            continue
        label = raw.split("|")[-1].strip()
        if label:
            out.append(label)
    return out


def _dedupe(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _list_items(value):
    """Parse a bulleted / linked infobox field into a list of clean strings."""
    items = []
    for line in (value or "").splitlines():
        line = re.sub(r"^[\*\#:;]+\s*", "", line.strip())
        if not line:
            continue
        links = _links(line)
        if links:
            items.extend(links)
        else:
            text = _wikiclean(line)
            if text:
                items.append(text)
    return _dedupe(items)


# --------------------- named mobs / quests / vendors -----------------------
def parse_named_mob(title, wikitext):
    """Parse a {{Namedmobpage}} into our named_mobs schema, or None."""
    body = _find_template(wikitext, "Namedmobpage")
    if body is None:
        return None
    f = _parse_fields(body)
    entry = {"name": title.strip()}
    for src, dst in (("zone", "zone"), ("location", "location"),
                     ("level", "level"), ("respawn_time", "respawn"),
                     ("race", "race"), ("class", "class")):
        v = _wikiclean(f.get(src, ""))
        if v:
            entry[dst] = v
    drops = _dedupe(_list_items(f.get("known_loot", ""))
                    + _list_items(f.get("common_loot", "")))
    if drops:
        entry["drops"] = drops
    faction = _wikiclean(f.get("factions", ""))
    if faction:
        entry["faction"] = faction
    quests = _list_items(f.get("related_quests", ""))
    if quests:
        entry["related_quests"] = quests
    entry["verified"] = "wiki"
    return entry


def parse_quest(title, wikitext):
    """Parse a {{Quest}} infobox plus {{QuestNote}} steps, or None."""
    body = _find_template(wikitext, "Quest")
    if body is None:
        return None
    f = _parse_fields(body)
    entry = {"name": _wikiclean(f.get("name", "")) or title.strip()}
    giver = _wikiclean(f.get("quest giver", ""))
    if giver:
        entry["giver"] = giver
    zone = _wikiclean(f.get("start zone", ""))
    if zone:
        entry["giver_location"] = zone
    lvl = f.get("minimum level", "").strip()
    n = _int(lvl)
    if n is not None:
        entry["level"] = n
    elif _wikiclean(lvl):
        entry["level"] = _wikiclean(lvl)
    classes = _wikiclean(f.get("classes", ""))
    if classes:
        entry["class"] = classes
    races = _wikiclean(f.get("races", ""))
    if races:
        entry["races"] = races
    reward = _wikiclean(f.get("quest rewards", ""))
    if reward:
        entry["reward"] = reward
    steps = [_wikiclean(b.lstrip("|")) for b in _iter_templates(wikitext, "QuestNote")]
    steps = [s for s in steps if s]
    if steps:
        entry["steps"] = steps
    related = _list_items(f.get("related items", ""))
    if related:
        entry["related_items"] = related
    entry["verified"] = "wiki"
    return entry


def parse_merchant(title, wikitext):
    """Parse a {{Merchantpage}} into our vendors schema, or None.

    The wiki lists what a merchant sells/buys but not prices, so `sells`
    entries carry only the item name; we never invent a price.
    """
    body = _find_template(wikitext, "Merchantpage")
    if body is None:
        return None
    f = _parse_fields(body)
    entry = {"name": title.strip()}
    zone = _wikiclean(f.get("zone", ""))
    if zone:
        entry["zone"] = zone
    location = _wikiclean(f.get("location", ""))
    if location:
        entry["location"] = location
    race = _wikiclean(f.get("race", ""))
    if race:
        entry["race"] = race
    sells = [{"item": x} for x in _list_items(f.get("sells", ""))]
    if sells:
        entry["sells"] = sells
    buys = _list_items(f.get("buys", ""))
    if buys:
        entry["buys"] = buys
    entry["verified"] = "wiki"
    return entry


def _set_description(wikitext):
    """First real prose paragraph of an armour-set page (drops/where info)."""
    t = re.sub(r"\{\{[^{}]*\}\}", " ", wikitext)
    t = re.sub(r"<ul.*?</ul>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<li>.*?</li>", " ", t, flags=re.S | re.I)
    t = re.sub(r"\[\[Category:[^\]]*\]\]", " ", t)
    for para in re.split(r"\n\s*\n", t):
        clean = _wikiclean(para)
        if len(clean) > 20:
            return clean
    return None


def parse_armor_set(title, wikitext):
    """Parse an armour-set page into our armor_sets schema, or None.

    Set pages aren't a uniform template: they list the pieces in a
    <li>[[piece]]</li> list (or * bullets) with a prose line on where they
    come from. No pieces found -> not a real set page -> None.
    """
    pieces = []
    for li in re.findall(r"<li>(.*?)</li>", wikitext, re.S | re.I):
        pieces.extend(_links(li))
    if not pieces:
        for line in wikitext.splitlines():
            if line.strip().startswith("*"):
                pieces.extend(_links(line))
    pieces = _dedupe(pieces)
    if not pieces:
        return None
    entry = {"name": title.strip(), "pieces": pieces}
    desc = _set_description(wikitext)
    if desc:
        entry["desc"] = desc
    if re.search(r"\{\{\s*RemovedFromGame\s*\}\}", wikitext, re.I):
        entry["flags"] = "REMOVED"
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


def run_import(categories, parser, data_key, write):
    """Scrape pages from `categories`, parse each with `parser`, merge the
    results into data.json under `data_key`."""
    titles, seen = [], set()
    for cat in categories:
        for t in pages_in_category(cat):
            if t.lower().startswith("category:") or t in seen:
                continue
            seen.add(t)
            titles.append(t)
    print(f"{len(titles)} unique pages across {len(categories)} categor"
          f"{'y' if len(categories) == 1 else 'ies'}", file=sys.stderr)
    entries = []
    for t in titles:
        e = parser(t, page_wikitext(t))
        if e:
            e.setdefault("verified", "wiki")
            entries.append(e)
    if not entries:
        print("no entries parsed.")
    merge(data_key, entries, write=write)


# each import kind: (parser, data.json key, default wiki categories)
IMPORTS = {
    "items": (parse_item_page, "items",
              ["Weapon", "Armor", "Jewelry", "Bags & Containers", "Inventory Items"]),
    "mobs": (parse_named_mob, "named_mobs", ["Named Mobs"]),
    "quests": (parse_quest, "quests", ["Quests"]),
    "merchants": (parse_merchant, "vendors", ["Merchant"]),
    "sets": (parse_armor_set, "armor_sets", ["Armor Sets"]),
}


# -------------------------------- CLI --------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("categories", help="list wiki categories")
    p = sub.add_parser("pages", help="list pages in a wiki category"); p.add_argument("category")
    p = sub.add_parser("wikitext", help="dump a page's wikitext"); p.add_argument("title")
    for kind in IMPORTS:
        p = sub.add_parser(f"import-{kind}",
                           help=f"scrape {kind} into data.json (default categories if none given)")
        p.add_argument("category", nargs="*")
        p.add_argument("--write", action="store_true")
    args = ap.parse_args()

    if args.cmd == "categories":
        for c in list_categories():
            print(c)
    elif args.cmd == "pages":
        for t in pages_in_category(args.category):
            print(t)
    elif args.cmd == "wikitext":
        print(page_wikitext(args.title))
    elif args.cmd.startswith("import-"):
        parser, data_key, default_cats = IMPORTS[args.cmd[len("import-"):]]
        run_import(args.category or default_cats, parser, data_key, args.write)


if __name__ == "__main__":
    main()
