#!/usr/bin/env python3
"""
Build the shareable single-file app.

Reads data/data.json, embeds it into app/template.html, and writes a
self-contained dist/MnM-Helper.html that runs by double-click with no
install. Also copies data.json and writes manifest.json so clients can
pull updates ("the patcher").

Usage:
    python tools/build.py                # bump build number, rebuild
    python tools/build.py --version 20261015.007   # force a version
    python tools/build.py --data-url https://raw.githubusercontent.com/you/mnm/main/data.json

Stdlib only.
"""
import argparse
import datetime as dt
import hashlib
import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "data.json"
TEMPLATE = ROOT / "app" / "template.html"
DIST = ROOT / "dist"
DOCS = ROOT / "docs"  # served by GitHub Pages (fixed link to open the app)
MANIFEST = ROOT / "manifest.json"


def load_manifest():
    if MANIFEST.exists():
        try:
            return json.loads(MANIFEST.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version": "", "build_number": 0}


def next_version(prev):
    today = dt.date.today().strftime("%Y%m%d")
    n = int(prev.get("build_number", 0)) + 1
    return f"{today}.{n:03d}", n


def main():
    ap = argparse.ArgumentParser(description="Build the M&M Helper single-file app.")
    ap.add_argument("--version", help="explicit version string (sortable, e.g. 20261015.007)")
    ap.add_argument("--data-url", help="URL where clients fetch data.json (stored in manifest)")
    ap.add_argument("--no-copy", action="store_true", help="do not copy data.json into dist/")
    args = ap.parse_args()

    if not DATA.exists():
        sys.exit(f"missing {DATA}")
    if not TEMPLATE.exists():
        sys.exit(f"missing {TEMPLATE}")

    raw = DATA.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"data.json is not valid JSON: {e}")

    prev = load_manifest()
    if args.version:
        version, build_no = args.version, prev.get("build_number", 0)
    else:
        version, build_no = next_version(prev)

    # embed into template
    tpl = TEMPLATE.read_text(encoding="utf-8")
    # compact JSON keeps the file small; </script> can't appear in our data but guard anyway
    embedded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = tpl.replace("__MNM_DATA__", embedded).replace("__MNM_VERSION__", version)

    DIST.mkdir(exist_ok=True)
    out_html = DIST / "MnM-Helper.html"
    out_html.write_text(html, encoding="utf-8")

    # GitHub Pages copy: index.html so the site root opens the app directly.
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(html, encoding="utf-8")

    sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if not args.no_copy:
        shutil.copy2(DATA, DIST / "data.json")
        shutil.copy2(DATA, DOCS / "data.json")

    manifest = {
        "version": version,
        "build_number": build_no,
        "stand": data.get("meta", {}).get("stand", ""),
        "game": data.get("meta", {}).get("game", "Monsters & Memories"),
        "data_url": args.data_url or prev.get("data_url") or "data.json",
        "sha256": sha,
        "built_at": dt.datetime.now().isoformat(timespec="seconds"),
        "entries": {k: len(v) for k, v in data.items() if isinstance(v, list)},
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy2(MANIFEST, DIST / "manifest.json")
    shutil.copy2(MANIFEST, DOCS / "manifest.json")

    total = sum(len(v) for v in data.values() if isinstance(v, list))
    print(f"built  {out_html}")
    print(f"version {version}  |  {total} entries  |  sha256 {sha[:12]}…")
    print(f"manifest data_url = {manifest['data_url']}")
    if manifest["data_url"] == "data.json":
        print("note: set --data-url once you know where you'll host data.json (e.g. GitHub raw).")


if __name__ == "__main__":
    main()
