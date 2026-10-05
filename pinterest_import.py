#!/usr/bin/env python3
"""
Build / update style.json from your Pinterest boards, so Dealwatch can boost deals
that match your taste (brands, materials, looks) and show a "For you" section.

OPTION A - works today, no Pinterest approval needed (your data export):
  1. Pinterest > Settings > Privacy and data > Request your data (choose JSON).
  2. When the file arrives, run:
       python tools/pinterest_import.py --export path/to/pinterest-export.zip
     (a .zip, a folder, or a single .json / .csv file all work)

SOCIAL EXPORTS (Facebook, Instagram, TikTok): add --social to read a "Download your information" file.
  It reads ads you interacted with, advertisers, liked and followed pages, and saved posts:
       python tools/pinterest_import.py --export path/to/instagram-export.zip --social --dry-run

OPTION B - Pinterest API (needs a Pinterest developer app; access may require approval):
  1. developers.pinterest.com > create an app > generate an access token with the
     scopes boards:read and pins:read.
  2. export PINTEREST_TOKEN=your-token
     python tools/pinterest_import.py --api

Add --dry-run to preview without changing style.json.
Nothing is uploaded anywhere. Your style.json stays in your own repo.
"""
import argparse, collections, csv, io, json, os, re, sys, zipfile
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STYLE_FILE = os.path.join(ROOT, "style.json")

TEXT_KEYS = {"title", "description", "note", "alt_text", "alt", "details", "text"}
BOARD_KEYS = {"board", "board_name", "boardname"}
LINK_KEYS = {"link", "url", "source", "source_url", "destination_url"}
SOCIAL_PATH_RE = re.compile(
    r"(^|[/_.\-])(ads?|advertis\w*|likes?|liked\w*|follow\w*|pages?|saved\w*|interests?|topics?|collections?|shop\w*|favou?rites?)([/_.\-]|$)", re.I)
SOCIAL_BLOCK_RE = re.compile(r"message|inbox|chat|conversation|comment|direct|(^|[/_])dms?([/_]|$)", re.I)  # never read private messages
SOCIAL_KEYS = {"name", "advertiser_name", "advertiser", "page_name", "value", "label", "author", "owner", "username"}

STOP = set("""a an and are as at be but by for from has have i in is it its me my of on or our so than that the their
them then there these this to us was we were what when where which who why will with you your just more most very
pin pins pinterest idea ideas board image images photo photos best top new cute easy diy inspo inspiration look looks
ways way how get via one two three four five http https www com net org html jpg png style styles style- aesthetic
women woman womens men mens girl girls summer winter spring fall outfit outfits""".split())

KNOWN_BRANDS = [
    "cb2", "west elm", "anthropologie", "aritzia", "reformation", "asos", "express", "topshop", "zara", "h&m", "cos",
    "arket", "mango", "madewell", "everlane", "free people", "urban outfitters", "pottery barn", "crate and barrel",
    "crate & barrel", "restoration hardware", "rh", "article", "ikea", "target", "amazon", "etsy", "nordstrom",
    "theory", "vince", "ganni", "staud", "zimmermann", "sandro", "maje", "rag & bone", "veronica beard",
    "cult gaia", "jacquemus", "the row", "khaite", "toteme", "loewe", "bottega veneta", "celine", "prada", "gucci",
    "chanel", "dior", "louis vuitton", "hermes", "saint laurent", "balenciaga", "burberry", "fendi", "chloe",
    "vivienne westwood", "miu miu", "coach", "kate spade", "tory burch", "longchamp", "mansur gavriel", "polene",
    "new balance", "nike", "adidas", "veja", "golden goose", "common projects", "ugg", "birkenstock", "hoka",
    "charlotte tilbury", "rare beauty", "fenty beauty", "nars", "tarte", "urban decay", "jo malone", "le labo",
    "byredo", "tom ford", "diptyque", "glossier", "kosas", "ilia", "drunk elephant", "tatcha",
    "dyson", "irobot", "tineco", "shark", "le creuset", "staub", "our place", "brooklinen", "parachute", "boll & branch",
]

def load_known_brands():
    brands = list(KNOWN_BRANDS)
    cfg = os.path.join(ROOT, "config.json")
    try:
        brands += [b.lower() for b in json.load(open(cfg)).get("BEAUTY_BRANDS", [])]
    except Exception:
        pass
    return sorted(set(brands), key=len, reverse=True)

# ---------- collecting text ----------
class Bag:
    def __init__(self, social=False):
        self.texts, self.links, self.boards, self.advertisers = [], [], [], []
        self.social, self.cur_ok = social, True

    def add_record(self, rec):
        if self.social:
            for item in (rec.get("string_map_data") or {}).values():
                if isinstance(item, dict) and isinstance(item.get("value"), str):
                    self.texts.append(item["value"])
            for item in rec.get("label_values") or []:
                if isinstance(item, dict) and isinstance(item.get("value"), str):
                    self.texts.append(item["value"])
        for k, v in rec.items():
            if not isinstance(v, str) or not v.strip():
                continue
            lk = str(k).lower()
            if self.social and lk in ("advertiser_name", "advertiser"):
                self.advertisers.append(v)
            if lk in TEXT_KEYS or (self.social and lk in SOCIAL_KEYS):
                self.texts.append(v)
            elif lk in BOARD_KEYS:
                self.boards.append(v)
                self.texts.append(v)
            elif lk in LINK_KEYS and v.startswith("http"):
                self.links.append(v)

    def walk(self, node):
        if self.social and not self.cur_ok:
            return
        if isinstance(node, dict):
            self.add_record(node)
            for v in node.values():
                self.walk(v)
        elif isinstance(node, list):
            for v in node:
                self.walk(v)

def read_blob(name, data, bag):
    low = name.lower()
    if bag.social:
        bag.cur_ok = bool(SOCIAL_PATH_RE.search(name)) and not SOCIAL_BLOCK_RE.search(name)
    if low.endswith(".json"):
        try:
            bag.walk(json.loads(data.decode("utf-8", "ignore")))
        except Exception as e:
            print("skipped", name, "-", e)
    elif low.endswith(".csv"):
        for row in csv.DictReader(io.StringIO(data.decode("utf-8", "ignore"))):
            bag.add_record(row)

def from_export(path, social=False):
    bag = Bag(social)
    if os.path.isdir(path):
        for root, _, files in os.walk(path):
            for f in files:
                with open(os.path.join(root, f), "rb") as fh:
                    read_blob(f, fh.read(), bag)
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                read_blob(n, z.read(n), bag)
    else:
        with open(path, "rb") as fh:
            read_blob(path, fh.read(), bag)
    return bag

def from_api():
    import requests
    token = os.environ.get("PINTEREST_TOKEN")
    if not token:
        sys.exit("Set PINTEREST_TOKEN first (see the top of this file).")
    hdr = {"Authorization": "Bearer " + token}
    bag = Bag()
    def pages(url, params=None, limit=6):
        bookmark = None
        for _ in range(limit):
            q = dict(params or {}, page_size=100)
            if bookmark:
                q["bookmark"] = bookmark
            r = requests.get(url, headers=hdr, params=q, timeout=30)
            if r.status_code != 200:
                sys.exit(f"Pinterest API said {r.status_code}: {r.text[:200]}")
            j = r.json()
            for it in j.get("items", []):
                yield it
            bookmark = j.get("bookmark")
            if not bookmark:
                return
    for b in pages("https://api.pinterest.com/v5/boards"):
        bag.boards.append(b.get("name", ""))
        bag.texts.append(b.get("name", "") + " " + (b.get("description") or ""))
        for pin in pages(f"https://api.pinterest.com/v5/boards/{b['id']}/pins"):
            bag.add_record({"title": pin.get("title") or "", "description": pin.get("description") or "",
                            "alt_text": pin.get("alt_text") or "", "link": pin.get("link") or ""})
    return bag

# ---------- analysis ----------
def analyze(bag):
    blob = "\n".join(bag.texts)
    low = blob.lower()
    brands = [b for b in load_known_brands() if re.search(r"(?<![\w])" + re.escape(b) + r"(?![\w])", low)]
    words = [w for w in re.findall(r"[a-z][a-z'&+-]{2,}", low) if w not in STOP and not w.startswith("http")]
    uni = collections.Counter(words)
    bi = collections.Counter(" ".join(p) for p in zip(words, words[1:]))
    keywords = [w for w, c in bi.most_common(60) if c >= 3][:12]
    keywords += [w for w, c in uni.most_common(80) if c >= 4 and len(w) > 3
                 and not any(w in k.split() for k in keywords) and w not in brands][:18]
    caps = collections.Counter(re.findall(r"\b([A-Z][a-z]+(?: [A-Z][a-z]+){1,2})\b", blob))
    candidates = [c for c, n in caps.most_common(30)
                  if n >= 2 and c.lower() not in brands and c.split()[0].lower() not in STOP][:12]
    domains = collections.Counter(urlparse(u).netloc.replace("www.", "") for u in bag.links)
    retailers = [d for d, _ in domains.most_common(10) if d and "pinterest" not in d]
    adv = [a for a, _ in collections.Counter(bag.advertisers).most_common(15)]
    return {"brands": brands[:25], "keywords": keywords[:25], "retailers": retailers,
            "boards": sorted(set(b for b in bag.boards if b))[:30], "brand_candidates": candidates,
            "advertisers": adv}

def merge(existing, found):
    out = dict(existing)
    for key in ("brands", "keywords"):
        have = [str(x) for x in out.get(key, [])]
        low = {x.lower() for x in have}
        out[key] = have + [x for x in found[key] if x.lower() not in low]
    out.setdefault("avoid", [])
    out["retailers"] = found["retailers"]
    out["boards"] = found["boards"]
    return out

def main():
    ap = argparse.ArgumentParser(description="Build style.json from Pinterest")
    ap.add_argument("--export", help="Pinterest data export (.zip, folder, .json or .csv)")
    ap.add_argument("--social", action="store_true", help="treat --export as a Facebook/Instagram/TikTok data download")
    ap.add_argument("--api", action="store_true", help="read boards through the Pinterest API (needs PINTEREST_TOKEN)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not (a.export or a.api):
        ap.error("choose --export PATH or --api")
    bag = from_api() if a.api else from_export(a.export, a.social)
    if not bag.texts:
        sys.exit("No pin text found. Check the file, or try the other option.")
    found = analyze(bag)
    print(f"Read {len(bag.texts)} text fields from {len(set(bag.boards))} boards.\n")
    print("Brands found:      ", ", ".join(found["brands"]) or "-")
    print("Style keywords:    ", ", ".join(found["keywords"]) or "-")
    print("Stores you pin from:", ", ".join(found["retailers"]) or "-")
    if found.get("advertisers"):
        print("Advertisers you interacted with (review, not added):", ", ".join(found["advertisers"]))
    if found["brand_candidates"]:
        print("Possible brands to review (not added):", ", ".join(found["brand_candidates"]))
    try:
        existing = json.load(open(STYLE_FILE))
    except Exception:
        existing = {}
    merged = merge(existing, found)
    if a.dry_run:
        print("\n(dry run, style.json not changed)")
        return
    json.dump(merged, open(STYLE_FILE, "w"), indent=2)
    print("\nUpdated", STYLE_FILE, "- review it, remove anything that doesn't fit, then commit.")

if __name__ == "__main__":
    main()
