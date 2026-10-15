#!/usr/bin/env python3
"""
Sanity-check data/data.json before building/publishing.

Not a strict schema (the DB is intentionally extensible) — it flags the
things that actually break the app or point to data-entry mistakes:
valid JSON, categories are lists of objects, entries have a usable title,
verification tags are known, prices look well-formed.

Exit code 0 = clean, 1 = problems found. Stdlib only.
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "data.json"

KNOWN_VERIFIED = {"ingame", "official", "wiki", "derived"}
TITLE_FIELDS = ["name", "song", "ability", "item", "skill", "topic", "class", "variant"]
PRICE_RE = re.compile(r"^\d+s(\s*\d+cp)?$|^\d+cp$")


def title_of(obj):
    for f in TITLE_FIELDS:
        if obj.get(f) is not None:
            return str(obj[f])
    return None


def main():
    if not DATA.exists():
        sys.exit(f"missing {DATA}")
    try:
        data = json.loads(DATA.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"INVALID JSON: {e}")

    problems, warnings = [], []
    if "meta" not in data:
        warnings.append("no 'meta' block (game/stand/sources)")

    for cat, arr in data.items():
        if cat == "meta":
            continue
        if not isinstance(arr, list):
            problems.append(f"{cat}: expected a list, got {type(arr).__name__}")
            continue
        for i, obj in enumerate(arr):
            where = f"{cat}[{i}]"
            if not isinstance(obj, dict):
                problems.append(f"{where}: entry is not an object")
                continue
            if title_of(obj) is None:
                problems.append(f"{where}: no title field ({'/'.join(TITLE_FIELDS)})")
            v = obj.get("verified")
            if v:
                tokens = re.split(r"[^a-z]+", str(v).lower())
                if not any(t in KNOWN_VERIFIED for t in tokens):
                    warnings.append(f"{where}: unknown 'verified' value: {v!r}")
            else:
                warnings.append(f"{where} ({title_of(obj)}): no 'verified' tag")
            # price fields
            for key in ("price", "sell_price", "price_per_train"):
                p = obj.get(key)
                if isinstance(p, str) and p not in ("nicht erfasst",) and not PRICE_RE.match(p.strip()):
                    warnings.append(f"{where}: odd {key} format: {p!r}")

    total = sum(len(v) for v in data.values() if isinstance(v, list))
    print(f"{total} entries across {sum(1 for k,v in data.items() if isinstance(v,list))} categories")
    for w in warnings:
        print("  warn:", w)
    for p in problems:
        print("  ERROR:", p)
    if problems:
        print(f"\n{len(problems)} error(s), {len(warnings)} warning(s) — NOT ready to publish")
        sys.exit(1)
    print(f"\nclean ({len(warnings)} warning(s)) — ready to build")


if __name__ == "__main__":
    main()
