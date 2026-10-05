#!/usr/bin/env python3
"""
dealwatch.py v2 - flags mispriced items & extreme discounts, pushes alerts via ntfy.sh

Sources:
  1. Deal feeds (Slickdeals, Reddit)            - no key needed
  2. Best Buy official API (electronics)        - free key: developer.bestbuy.com
  3. Amazon watchlist via Keepa                 - paid key: keepa.com/api
  4. Any product URL you list (JSON-LD prices)  - works on sites without bot protection
  5. Home goods via Slickdeals searches (with per-roll / per-load targets)
  6. Tampa flights: free deal feeds + Google Flights via SerpApi (key: serpapi.com)

Env vars: NTFY_TOPIC, BESTBUY_KEY, KEEPA_KEY, SERPAPI_KEY, EBAY_CLIENT_ID/SECRET (all optional)
Outputs : state.json (dedupe + price history), alerts.json (feeds the dashboard)
"""
import base64, calendar, collections, datetime, json, os, re, smtplib, ssl, statistics, time
from email.message import EmailMessage
from html import escape, unescape
from urllib.parse import quote_plus, urljoin
import feedparser, requests

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
TOP_FILE = os.path.join(HERE, "top.json")
STYLE_FILE = os.path.join(HERE, "style.json")
ALERTS_FILE = os.path.join(HERE, "alerts.json")

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
KEEPA_KEY = os.environ.get("KEEPA_KEY", "")
BESTBUY_KEY = os.environ.get("BESTBUY_KEY", "")
UA = {"User-Agent": "Mozilla/5.0 (compatible; dealwatch/2.0; personal use)"}

# ---- Tunables -------------------------------------------------------------
PCT_THRESHOLD = 60          # feeds: alert at >= this % off (extreme only)
LUXURY_PCT_THRESHOLD = 60   # feeds: luxury brands
BESTBUY_PCT = 60            # Best Buy: alert at >= this % off
BESTBUY_MIN_REGULAR = 50    # any price, but skip tiny accessories under this ($)
AMAZON_VS_AVG_PCT = 50      # Amazon: alert if this % below 90-day average
URL_DROP_PCT = 40           # URL watch: alert if this % below its own median
APPLE_PCT_THRESHOLD = 25    # feeds: Apple rarely goes 60% off, so 25% counts as extreme
BESTBUY_APPLE_PCT = 20      # Best Buy: ANY Apple product >= this % off
BRAND_PCT_THRESHOLD = 50    # feeds: brands from MY_BRANDS below
# Every Apple product line (matched against deal titles)
APPLE_RE = re.compile(
    r"\bapple\b|iphone|ipad|macbook|mac ?mini|mac ?studio|mac ?pro|imac|"
    r"airpods|airtag|apple ?watch|apple ?tv|homepod|vision ?pro|"
    r"apple ?pencil|magsafe|magic (keyboard|mouse|trackpad)|studio display|"
    r"pro display xdr|beats (studio|solo|fit|flex|powerbeats)", re.I)
# Brands/companies you already buy from or subscribe to (found in your inbox).
# Deals that mention them get the lower BRAND_PCT_THRESHOLD. Add more here.
MY_BRANDS = ["classpass", "masterclass"]
BRANDS_RE = re.compile("|".join(re.escape(b) for b in MY_BRANDS), re.I) if MY_BRANDS else None
ERROR_WORDS = re.compile(
    r"pric(e|ing)[ -]?(error|mistake|glitch)|mis-?priced|glitch|"
    r"lowest ever|all[- ]time low|clearance error", re.I)
LUXURY_BRANDS = re.compile(
    r"gucci|prada|miu miu|balenciaga|burberry|moncler|canada goose|saint laurent|ysl|"
    r"loewe|fendi|versace|valentino|bottega|celine|dior|chanel|herm[eè]s|louis vuitton|"
    r"chlo[eé]|mulberry|jimmy choo|louboutin|manolo|stuart weitzman|alexander mcqueen|"
    r"isabel marant|max mara|marc jacobs|tory burch|kate spade|longchamp|"
    r"golden goose|veja|ugg|cartier|tiffany|van cleef|bulgari|david yurman|mejuri|"
    r"monica vinader|pandora|rolex|tag heuer|omega|tissot|ray-ban|oakley|"
    r"coach|michael kors|ferragamo|tom ford|maison margiela|acne studios|hogan", re.I)

# Electronics categories you picked: computers/PC parts, phones/tablets/audio, TVs/home theater
ELECTRONICS_RE = re.compile(
    r"laptop|notebook|chromebook|desktop|monitor|gpu|graphics card|rtx|radeon|"
    r"ryzen|intel core|cpu|motherboard|ram\b|ddr[45]|ssd|nvme|hard drive|"
    r"router|keyboard|mouse|webcam|"
    r"phone|galaxy|pixel|tablet|kindle|headphone|earbuds|airpods|speaker|soundbar|"
    r"sonos|bose|sony|samsung|lg |tcl|hisense|vizio|oled|qled|4k|8k|\btv\b|"
    r"projector|receiver|subwoofer", re.I)
GENERAL_FEEDS = {"Slickdeals", "r/deals"}  # noisy feeds: only keep relevant items

# name -> (feed url, category)
FEEDS = {
    "Slickdeals": ("https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1", "electronics"),
    "r/buildapcsales": ("https://www.reddit.com/r/buildapcsales/new/.rss", "electronics"),
    "r/deals": ("https://www.reddit.com/r/deals/new/.rss", "electronics"),
    "r/FrugalFemaleFashion": ("https://www.reddit.com/r/FrugalFemaleFashion/new/.rss", "fashion"),
}

# ---- Home products (toilet paper, towels, bedding, cleaning, laundry) -------
HOME_PCT_THRESHOLD = 35     # household goods rarely go 60% off, so the bar is lower
HOME_EVERY_HOURS = 2        # these feeds are checked less often than the rest
HOME_SEARCH_TERMS = [
    "toilet paper", "paper towels", "laundry detergent", "laundry pods",
    "cleaning supplies", "disinfecting wipes", "dish soap", "trash bags",
    "bed sheets", "comforter", "duvet", "pillows", "bath towels", "mattress topper",
]
_ROLLS = re.compile(r"(\d+)\s*(?:mega |double |super |family |giant |jumbo |ultra )*rolls?", re.I)
# (item regex, count regex, unit label, max $ per unit that counts as a deal) - tune these!
HOME_UNIT_TARGETS = [
    (re.compile(r"toilet (paper|tissue)|bath tissue", re.I), _ROLLS, "roll", 0.45),
    (re.compile(r"paper towels?", re.I), _ROLLS, "roll", 0.80),
    (re.compile(r"detergent|laundry|pods", re.I),
     re.compile(r"(\d+)\s*(?:loads?|wash(?:es)?)", re.I), "load", 0.12),
]

# ---- Flights out of Tampa (international; nonstop or 1 stop under 2h layover)
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")   # serpapi.com (Google Flights data)
FLIGHT_ORIGIN = "TPA"
MAX_LAYOVER_MIN = 120       # 1 stop allowed only if the layover is shorter than this
FLIGHT_DROP_PCT = 40        # alert if >= this % below the route's typical price
FLIGHT_DEPART_OFFSETS = [30, 60, 90, 150]   # days from today to sample departures
FLIGHT_TRIP_DAYS = 7
FLIGHT_DAILY_BUDGET = 3     # searches per day (~90/month fits SerpApi's free tier)
FLIGHT_MAX_PER_RUN = 2
FLIGHT_RECHECK_HOURS = 20
# Starter list of international destinations. Edit freely: "CODE": "City"
FLIGHT_DESTINATIONS = {
    "LHR": "London", "CDG": "Paris", "AMS": "Amsterdam", "FRA": "Frankfurt",
    "MAD": "Madrid", "BCN": "Barcelona", "FCO": "Rome", "LIS": "Lisbon",
    "DUB": "Dublin", "KEF": "Reykjavik", "ZRH": "Zurich", "IST": "Istanbul",
    "ATH": "Athens", "CUN": "Cancun", "PUJ": "Punta Cana", "NAS": "Nassau",
    "MBJ": "Montego Bay", "SJO": "San Jose, Costa Rica", "PTY": "Panama City",
    "BOG": "Bogota", "LIM": "Lima", "GRU": "Sao Paulo", "EZE": "Buenos Aires",
    "MEX": "Mexico City", "YYZ": "Toronto", "NRT": "Tokyo", "ICN": "Seoul",
    "DOH": "Doha", "DXB": "Dubai",
}
# Free flight-deal feeds (cover ALL destinations; only Tampa posts are kept)
FLIGHT_FEEDS = {
    "r/FlightDeals": "https://www.reddit.com/r/flightdeals/new/.rss",
    "Secret Flying": "https://www.secretflying.com/feed/",
    "The Flight Deal": "https://www.theflightdeal.com/feed/",
}
TPA_RE = re.compile(r"\bTPA\b|\bTampa\b", re.I)
DOMESTIC_RE = re.compile(r"\b(USA|United States)\b", re.I)

# ---- Shoes: women's US size 8 / 8.5 ---------------------------------------
SHOE_SIZES = [8, 8.5]
SHOE_PCT_THRESHOLD = 50     # insane drops only; lower than the general bar since size-filtered
SHOE_EVERY_HOURS = 1        # sizes sell out fast, so check hourly
SHOE_SEARCH_TERMS = [
    "womens sneakers", "womens boots", "womens heels", "womens sandals",
    "womens running shoes", "designer shoes", "womens loafers", "womens flats",
]
SHOE_RE = re.compile(
    r"sneakers?|\bshoes?\b|boots?\b|booties|\bheels?\b|\bpumps?\b|sandals?|loafers?|"
    r"ballet flats?|\bflats\b|mules?\b|slippers?|clogs?|trainers?|running shoes?|"
    r"\bslides?\b|oxfords?|wedges?|stilettos?|espadrilles?|"
    r"nike|adidas|new balance|asics|hoka|brooks|converse|\bvans\b|birkenstock|ugg\b|"
    r"veja|golden goose|louboutin|jimmy choo|manolo|stuart weitzman|common projects|"
    r"saucony|puma|reebok|skechers|dr\.? martens|timberland|sorel|on cloud|hogan", re.I)
MENS_RE = re.compile(r"\bmen'?s\b|\bmens\b|\bboys?\b|\bkids?\b|\bgirls?\b|toddler|youth", re.I)
WOMENS_RE = re.compile(r"women|ladies|\bfemale\b|unisex", re.I)
SIZE_RANGE = re.compile(r"sizes?\s*:?\s*(\d{1,2}(?:\.\d)?)\s*(?:-|\u2013|\u2014|to)\s*(\d{1,2}(?:\.\d)?)", re.I)
SIZE_LIST = re.compile(r"sizes?\s*:?\s*((?:\d{1,2}(?:\.5)?\s*(?:,|/|&|and|or)?\s*)+)", re.I)

SHOE_SIZES_EU = [38.5, 39, 39.5]   # US 8 / 8.5 in European sizing (Hogan, Gucci...). Check each brand's size chart.

def size_status(text):
    """'ok' if the post says your size is available, 'no' if sizes are listed without it, else 'unknown'.
    Numbers of 33 or more are read as European sizes."""
    text = re.sub(r"<[^>]+>", " ", text)
    found = False
    for lo, hi in SIZE_RANGE.findall(text):
        found = True
        mine = SHOE_SIZES_EU if float(lo) >= 33 else SHOE_SIZES
        if any(float(lo) <= sz <= float(hi) for sz in mine):
            return "ok"
    for lst in SIZE_LIST.findall(text):
        nums = [float(x) for x in re.findall(r"\d{1,2}(?:\.5)?", lst)]
        found = found or bool(nums)
        mine = SHOE_SIZES_EU if any(n >= 33 for n in nums) else SHOE_SIZES
        if any(sz in nums for sz in mine):
            return "ok"
    return "no" if found else "unknown"

AMAZON_WATCHLIST = {
    # "B0BDHWDR12": "Sony WH-1000XM5",
}

URL_WATCHLIST = {
    # "https://www.example.com/product/123": "Label for this product",
}


# ---- Your favorites: Apple, cats, handbags, jewelry, underwear, vacuums, luxury clothing
APPLE_FAV_RE = re.compile(r"airpods ?max|airport (express|extreme|time capsule)|time capsule", re.I)
APPLE_FAV_PCT = 20          # AirPods Max / AirPort: alert at a much lower discount

CAT_RE = re.compile(
    r"cat (tree|tower|condo|furniture|bed|hammock|perch|shelf|shelves|scratcher|scratching|house)|"
    r"catio|mau lifestyle|refined feline|tuft ?(\+|&|and)? ?paw|catastrophic creations|"
    r"hauspanther|modern cat", re.I)
CAT_LUX_RE = re.compile(
    r"mau lifestyle|refined feline|tuft ?(\+|&|and)? ?paw|catastrophic creations|hauspanther|"
    r"modern cat|designer|luxury|solid wood|wooden|wall[- ]mounted|hexagon", re.I)
CAT_PCT_THRESHOLD = 55
CAT_LUXURY_PCT_THRESHOLD = 35

DESIGNER_RE = re.compile(
    r"\btheory\b|\bvince\b|rag ?(&|and)? ?bone|veronica beard|reformation|\bsandro\b|\bmaje\b|ba&sh|"
    r"zimmermann|ganni|staud|cinq [a\u00e0] sept|a\.l\.c|ulla johnson|self-portrait|"
    r"brunello cucinelli|loro piana|khaite|toteme|nili lotan|alice ?(\+|&|and) ?olivia|"
    r"diane von furstenberg|\bdvf\b|l'?agence|club monaco|eileen fisher|st\.? john knits?|escada|"
    r"akris|lafayette 148|elie tahari|helmut lang|jacquemus|cult gaia|mansur gavriel|polene|"
    r"strathberry|cuyana|furla|rebecca minkoff|dooney|stella mccartney|anya hindmarch|telfar|"
    r"longchamp|marc jacobs|kate spade|tory burch|michael kors|\bcoach\b", re.I)

HANDBAG_RE = re.compile(r"handbag|\bpurses?\b|\btotes?\b|crossbody|clutch|satchel|shoulder bag|"
                        r"bucket bag|hobo|belt bag|wristlet|\bbags?\b", re.I)
HANDBAG_PCT_THRESHOLD = 50

# Jewelry: REAL only (karat gold, sterling, platinum, diamonds, genuine gems)
JEWELRY_RE = re.compile(
    r"necklace|earrings?|bracelet|pendant|bangle|anklet|brooch|wedding band|engagement ring|"
    r"(gold|silver|diamond|platinum|sapphire|emerald|ruby|pearl) rings?|\bstuds\b", re.I)
REAL_RE = re.compile(
    r"\b(10|14|18|22|24)\s?k(t|arat)?\b|\bsterling\b|\b925\b|solid gold|real gold|platinum|"
    r"\bdiamonds?\b|lab[- ](grown|created)|genuine|sapphire|emerald|\bruby\b|cultured pearl|"
    r"akoya|freshwater pearl|tanzanite|\bopal\b|topaz|amethyst|aquamarine", re.I)
FAKE_RE = re.compile(
    r"plated|faux|costume|imitation|simulated|gold[- ]?tone|silver[- ]?tone|vermeil|"
    r"gold[- ]filled|fashion jewelry|stainless|brass|alloy|resin|zinc|cubic zirconia|\bcz\b|moissanite", re.I)
JEWELRY_PCT_THRESHOLD = 40

UNDERWEAR_RE = re.compile(r"underwear|panties|panty|\bthongs?\b|boy ?shorts?|hipsters?|bikini briefs?", re.I)
UNDERWEAR_PCT_THRESHOLD = 40

APPLIANCE_RE = re.compile(
    r"roomba|irobot|tineco|dyson|roborock|bissell|braava|robot vacuum|robovac|cordless vacuum|"
    r"stick vacuum|steam mop|vacuum cleaner|shark[\w\s-]{0,30}(vacuum|robot|cordless|mop)|"
    r"eufy[\w\s-]{0,20}(vacuum|robovac|clean)|\bsmeg\b|nespresso|kitchen ?aid|"
    r"\bninja\b[\w\s-]{0,25}(air fryer|blender|creami|foodi|pressure cooker|grill|coffee|slushi|ice cream|processor|oven|toaster|cooker|smoothie|speedi|crispi)", re.I)
APPLIANCE_PCT_THRESHOLD = 40

# Luxury women's clothing: blouses size S, pants size 4/6
CLOTHING_RE = re.compile(
    r"blouse|\btops?\b|shirt|dress(es)?\b|pants|trousers|jeans|skirt|sweater|cardigan|jacket|"
    r"coat\b|blazer|jumpsuit|\bknit\b|\bsilk\b", re.I)
CLOTHING_SIZES_ALPHA = ["S"]
CLOTHING_SIZES_NUM = [4, 6]
CLOTHING_PCT_THRESHOLD = 50
_ALPHA = ["XXS", "XS", "S", "M", "L", "XL", "XXL"]
_A = "XXS|XS|S|M|L|XL|XXL"
SEG_RE = re.compile(r"sizes?\s*:?\s*([^.;|\n]{0,60})", re.I)

def clothing_size_status(text):
    """'ok' if your size (S, 4, 6) is listed, 'no' if sizes are listed without it, else 'unknown'."""
    text = re.sub(r"<[^>]+>", " ", text)
    found = False
    for seg in SEG_RE.findall(text):
        for lo, hi in re.findall(rf"\b({_A})\s*(?:-|\u2013|to)\s*({_A})\b", seg, re.I):
            found = True
            i, j = _ALPHA.index(lo.upper()), _ALPHA.index(hi.upper())
            if any(i <= _ALPHA.index(a) <= j for a in CLOTHING_SIZES_ALPHA):
                return "ok"
        toks = ["S" if t.upper() == "SMALL" else t.upper()
                for t in re.findall(rf"\b({_A}|small)\b", seg, re.I)]
        found = found or bool(toks)
        if any(a in toks for a in CLOTHING_SIZES_ALPHA):
            return "ok"
        for lo, hi in re.findall(r"\b(\d{1,2})\s*(?:-|\u2013|to)\s*(\d{1,2})\b", seg):
            found = True
            if any(int(lo) <= n <= int(hi) for n in CLOTHING_SIZES_NUM):
                return "ok"
        nums = [int(x) for x in re.findall(r"\b\d{1,2}\b", seg)]
        found = found or bool(nums)
        if any(n in nums for n in CLOTHING_SIZES_NUM):
            return "ok"
    return "no" if found else "unknown"

# More household items: hand soap and candles (per-oz / per-candle targets)
HOME_SEARCH_TERMS += ["hand soap", "foaming hand soap", "candles", "3 wick candle", "luxury candle"]
HOME_UNIT_TARGETS += [
    (re.compile(r"hand soap|foaming soap|soap refill", re.I),
     re.compile(r"(\d+(?:\.\d+)?)\s*(?:fl\.?\s*)?oz", re.I), "oz", 0.10),
    (re.compile(r"3[- ]wick|three[- ]wick", re.I), None, "candle", 10.00),
]

CAT_SEARCH_TERMS = ["cat tree", "cat tower", "designer cat furniture", "modern cat tree",
                    "cat wall shelves", "cat condo"]
HANDBAG_SEARCH_TERMS = ["designer handbag", "coach handbag", "kate spade purse",
                        "michael kors bag", "luxury handbag", "tory burch bag"]
JEWELRY_SEARCH_TERMS = ["14k gold", "sterling silver necklace", "diamond earrings", "diamond ring",
                        "lab grown diamond", "gold bracelet", "gold necklace", "pearl earrings"]
UNDERWEAR_SEARCH_TERMS = ["womens underwear", "panties", "hanky panky", "cosabella",
                          "skims underwear", "victoria's secret panties"]
APPLIANCE_SEARCH_TERMS = ["roomba", "irobot", "tineco", "cordless vacuum", "robot vacuum",
                          "stick vacuum", "dyson vacuum", "shark cordless vacuum"]
CLOTHING_SEARCH_TERMS = ["designer dress", "womens designer clothing", "theory womens", "vince womens",
                         "veronica beard", "reformation", "zimmermann", "ganni", "sandro maje",
                         "rag and bone womens", "silk blouse"]

# How often each Slickdeals search group is checked (hours)
THROTTLE_HOURS = {"home": 2, "shoes": 1, "cats": 2, "jewelry": 1, "underwear": 2,
                  "appliances": 1, "handbags": 1, "clothing": 1, "pets": 1,
                  "decor": 2, "clearance": 1, "makeup": 1, "perfume": 1, "personalcare": 2, "favorites": 1}
NO_RESALE = {"home", "flights", "jewelry", "underwear", "pets", "decor", "personalcare"}   # no eBay profit estimate for these

def build_search_feeds():
    groups = {"home": HOME_SEARCH_TERMS, "shoes": SHOE_SEARCH_TERMS, "cats": CAT_SEARCH_TERMS,
              "handbags": HANDBAG_SEARCH_TERMS, "jewelry": JEWELRY_SEARCH_TERMS,
              "underwear": UNDERWEAR_SEARCH_TERMS, "appliances": APPLIANCE_SEARCH_TERMS,
              "pets": globals().get("PET_SEARCH_TERMS", []),
              "decor": globals().get("DECOR_SEARCH_TERMS", []),
              "clearance": globals().get("CLEARANCE_SEARCH_TERMS", []),
              "favorites": globals().get("FAVORITE_SEARCH_TERMS", []),
              "makeup": globals().get("MAKEUP_SEARCH_TERMS", []),
              "perfume": globals().get("PERFUME_SEARCH_TERMS", []),
              "personalcare": globals().get("PERSONALCARE_SEARCH_TERMS", []),
              "clothing": CLOTHING_SEARCH_TERMS}
    for k in [k for k in FEEDS if k.startswith("Slickdeals ")]:
        del FEEDS[k]
    for cat, terms in groups.items():
        for t in terms:
            FEEDS[f"Slickdeals {cat}: {t}"] = (
                "https://slickdeals.net/newsearch.php?q=" + quote_plus(t) +
                "&searcharea=deals&searchin=first&rss=1", cat)

build_search_feeds()


# ---- Email digest (morning + evening) ---------------------------------------
SMTP_HOST = os.environ.get("SMTP_HOST") or "smtp.gmail.com"      # GitHub passes unset secrets as empty text, so use "or"
SMTP_PORT = int(os.environ.get("SMTP_PORT") or "465")
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "") or SMTP_USER
DIGEST_TZ = "America/New_York"      # Tampa
DIGEST_HOURS = [7, 18]              # local hours: 7 AM and 6 PM
DIGEST_MAX_DEALS = 10
DIGEST_MIN_SCORE = 50               # only include deals scoring at least this
DIGEST_SEND_EMPTY = False

# ---- Mispricing scan: finds items far below what every store charges --------
OUTLIER_QUERIES = [
    "AirPods Max", "AirPods Pro 2", "Apple Watch Series 10", "MacBook Air M3", "iPad Air",
    "Dyson V15 Detect", "iRobot Roomba j7+", "Tineco Pure One S11", "Sony WH-1000XM5",
    "LG C4 OLED 65 inch", "Samsung S90D OLED 65 inch", "Bose QuietComfort Ultra",
]
OUTLIER_PCT = 35                    # flag listings this % below the median price
OUTLIER_DAILY_BUDGET = 5            # searches per day (1 SerpApi credit each)
OUTLIER_RECHECK_HOURS = 20

# ---- Extra RSS feeds you add yourself (Google Alerts RSS, DealNews, Woot...) --
EXTRA_FEEDS = {}                    # "Name": "https://feed-url"


# ---- Bulk buying: pet food, litter, and household staples by UNIT price ------
# Max price per unit that counts as a deal. Tune these to what you normally pay.
UNIT_TARGETS = {
    "toilet_paper_per_roll": 0.45, "paper_towel_per_roll": 0.80,
    "laundry_per_load": 0.12, "laundry_pods_per_ct": 0.17, "dishwasher_per_ct": 0.15,
    "hand_soap_per_oz": 0.10, "body_wash_per_oz": 0.12, "bar_soap_per_bar": 0.50,
    "candle_each": 10.00,
    "dry_dog_food_per_lb": 1.00, "dry_cat_food_per_lb": 1.40,
    "wet_food_per_oz": 0.15, "cat_litter_per_lb": 0.30,
}
PET_PCT_THRESHOLD = 40
PET_SEARCH_TERMS = [
    "dog food", "cat food", "cat litter", "wet cat food", "dry dog food", "bulk dog food",
    "Purina Pro Plan", "Blue Buffalo", "Fancy Feast", "Tidy Cats", "dog treats",
]
HOME_SEARCH_TERMS += ["body wash", "bar soap", "dishwasher detergent", "dishwasher pods",
                      "bulk toilet paper", "bulk paper towels"]
PET_RE = re.compile(
    r"dog food|cat food|pet food|kibble|cat litter|\blitter\b|dog treats?|cat treats?|"
    r"purina|blue buffalo|fancy feast|friskies|pro plan|science diet|royal canin|\biams\b|"
    r"wellness (core|complete)|orijen|acana|merrick|nutro|tidy cats|fresh step|"
    r"world'?s best cat|sheba|temptations|greenies|milk-bone|pedigree|beneful|nutrish|"
    r"kirkland (signature )?(dog|cat|nature)|chewy", re.I)
_DOG = re.compile(r"\bdogs?\b|puppy|canine|pedigree|beneful|milk-bone|greenies", re.I)
_CATF = re.compile(r"\bcats?\b|kitten|feline|fancy feast|friskies|sheba|temptations|tidy cats|fresh step", re.I)
_WET = re.compile(r"\bcans?\b|p[a\u00e2]t[e\u00e9]|\bwet\b|pouch|gravy|\btrays?\b|fancy feast|sheba", re.I)
_FOOD = re.compile(r"food|kibble|formula|chow|pro plan|science diet|blue buffalo|nutro|iams|orijen|"
                   r"acana|wellness|royal canin|purina one|pedigree|beneful|nutrish|fancy feast|friskies|sheba", re.I)
_LITTER = re.compile(r"litter", re.I)
_UNITS_OZ = {"lb": 16, "lbs": 16, "pound": 16, "pounds": 16, "oz": 1, "ounce": 1, "ounces": 1}
_MULT_W = re.compile(r"(\d+)\s*[x\u00d7]\s*(\d+(?:\.\d+)?)\s*[- ]?(lbs?|pounds?|oz|ounces?)\b", re.I)
_ONE_W = re.compile(r"(\d+(?:\.\d+)?)\s*[- ]?(lbs?|pounds?|oz|ounces?)\b", re.I)
_PACK = re.compile(r"(?:pack of|case of|set of)\s*(\d+)|(\d+)\s*[- ]?(?:pack|count|ct)\b", re.I)

def total_weight_oz(title):
    """Total ounces in a listing: handles '35 lb', '24 x 5.5 oz', '3 oz cans, 24 count'."""
    m = _MULT_W.search(title)
    if m:
        return float(m.group(1)) * float(m.group(2)) * _UNITS_OZ[m.group(3).lower()]
    m = _ONE_W.search(title)
    if not m:
        return None
    oz = float(m.group(1)) * _UNITS_OZ[m.group(2).lower()]
    pk = _PACK.search(title)
    if pk:
        n = float(pk.group(1) or pk.group(2))
        if 1 < n <= 200:
            oz *= n
    return oz

def pet_unit_hit(title):
    """(price per unit, unit, ratio vs target) when pet food / litter beats your target."""
    price = title_cost(title)
    oz = total_weight_oz(title)
    if not price or not oz:
        return None
    lb = oz / 16
    if _LITTER.search(title):
        per, unit, target = price / lb, "lb", UNIT_TARGETS["cat_litter_per_lb"]
    elif _WET.search(title) and _FOOD.search(title):
        per, unit, target = price / oz, "oz", UNIT_TARGETS["wet_food_per_oz"]
    elif _FOOD.search(title):
        if _DOG.search(title):
            per, unit, target = price / lb, "lb", UNIT_TARGETS["dry_dog_food_per_lb"]
        elif _CATF.search(title):
            per, unit, target = price / lb, "lb", UNIT_TARGETS["dry_cat_food_per_lb"]
        else:
            return None
    else:
        return None
    return (per, unit, per / target) if per <= target else None

def build_unit_targets():
    """Rebuilds per-unit rules for household items from UNIT_TARGETS."""
    global HOME_UNIT_TARGETS
    T = UNIT_TARGETS
    rolls = re.compile(r"(\d+)\s*(?:mega |double |super |family |giant |jumbo |ultra )*rolls?", re.I)
    ct = re.compile(r"(\d+)\s*(?:ct|count|pods?|tabs?|tablets?)\b", re.I)
    oz = re.compile(r"(\d+(?:\.\d+)?)\s*(?:fl\.?\s*)?oz", re.I)
    HOME_UNIT_TARGETS = [
        (re.compile(r"toilet (paper|tissue)|bath tissue", re.I), rolls, "roll", T["toilet_paper_per_roll"]),
        (re.compile(r"paper towels?", re.I), rolls, "roll", T["paper_towel_per_roll"]),
        (re.compile(r"dishwasher|cascade|finish (quantum|powerball|ultimate)", re.I), ct, "pod", T["dishwasher_per_ct"]),
        (re.compile(r"detergent|laundry|tide|persil|gain|pods", re.I),
         re.compile(r"(\d+)\s*(?:loads?|wash(?:es)?)", re.I), "load", T["laundry_per_load"]),
        (re.compile(r"laundry|tide|persil|gain|pods", re.I), ct, "pod", T["laundry_pods_per_ct"]),
        (re.compile(r"hand soap|foaming soap|soap refill", re.I), oz, "oz", T["hand_soap_per_oz"]),
        (re.compile(r"body wash|shower gel", re.I), oz, "oz", T["body_wash_per_oz"]),
        (re.compile(r"bar soap|soap bars?|\bbars\b", re.I), re.compile(r"(\d+)\s*(?:bars?|ct|count)\b", re.I), "bar", T["bar_soap_per_bar"]),
        (re.compile(r"3[- ]wick|three[- ]wick", re.I), None, "candle", T["candle_each"]),
    ]

build_unit_targets()
build_search_feeds()


# ---- Open box / clearance + your favorite home & fashion retailers ----------
OPENBOX_PCT = 35            # Best Buy open box: alert at >= this % below the regular price
OPENBOX_PAGES = 3           # pages of 100 open-box listings to scan
BESTBUY_CLEARANCE_PCT = 50
CLEARANCE_SEARCH_TERMS = ["open box", "clearance", "floor model", "warehouse deal", "open-box"]

DECOR_PCT_THRESHOLD = 45
DECOR_BRAND_RE = re.compile(r"\bcb2\b|west elm|perigold", re.I)
HOME_WORDS_RE = re.compile(
    r"\brugs?\b|bedding|duvet|comforter|quilt|sheets?|pillows?|throw|curtains?|drapes?|lamp|lighting|"
    r"chandelier|sconce|mirror|vase|candle|planter|shelf|shelves|bookcase|sofa|sectional|couch|"
    r"chair|stool|table|desk|dresser|nightstand|bed frame|headboard|ottoman|console|bar cart|"
    r"wall art|frame|decor|storage|basket|cookware|dinnerware|glassware|kitchen|towels?|home", re.I)
DECOR_SEARCH_TERMS = ["west elm", "cb2", "anthropologie home", "anthropologie furniture", "anthropologie rug",
                      "amazon home", "amazon kitchen finds", "amazon bedding"]
RETAIL_FASHION_RE = re.compile(
    r"anthropologie|aritzia|\basos\b|top ?shop|reformation|"
    r"\bexpress\b(?!\s+(shipping|delivery|checkout|lane|pickup))", re.I)
CLOTHING_SEARCH_TERMS += ["anthropologie", "express womens", "aritzia", "asos", "topshop"]

build_search_feeds()


# ---- Beauty: premium makeup & perfume only, plus wipes and deodorant ---------
MAKEUP_PCT_THRESHOLD = 40
PERFUME_PCT_THRESHOLD = 30          # perfume rarely goes past ~40% off at real retailers
PERSONALCARE_PCT_THRESHOLD = 35
# Only these brands count for makeup/perfume. Edit freely.
BEAUTY_BRANDS = [
    "charlotte tilbury", "dior", "chanel", "yves saint laurent", "ysl", "tom ford", "armani beauty",
    "giorgio armani", "hourglass", "pat mcgrath", "natasha denona", "fenty beauty", "rare beauty",
    "tarte", "urban decay", "nars", "mac cosmetics", "laura mercier", "bobbi brown", "estee lauder",
    "est\u00e9e lauder", "lancome", "lanc\u00f4me", "clinique", "too faced", "anastasia beverly hills",
    "huda beauty", "makeup by mario", "westman atelier", "merit beauty", "kosas", "ilia", "saie",
    "sisley", "la mer", "guerlain", "givenchy", "valentino beauty", "herm\u00e8s", "hermes beauty",
    "milk makeup", "smashbox", "bareminerals", "jo malone", "maison francis kurkdjian", "le labo",
    "byredo", "creed", "gucci", "prada", "versace", "viktor & rolf", "viktor and rolf", "marc jacobs",
    "jimmy choo", "burberry", "carolina herrera", "narciso rodriguez", "dolce & gabbana",
    "dolce and gabbana", "chlo\u00e9", "chloe", "kilian", "parfums de marly", "initio", "xerjoff",
    "diptyque", "margiela", "miu miu", "bvlgari", "bulgari", "cartier", "mugler", "paco rabanne",
    "issey miyake", "tory burch", "kayali", "sol de janeiro", "juliette has a gun", "ex nihilo",
    "penhaligon", "amouage", "atelier cologne", "acqua di parma", "frederic malle", "nest fragrances",
    "benefit cosmetics", "glow recipe", "drunk elephant", "tatcha", "sunday riley", "augustinus bader",
]
# Anything with these terms is skipped (dupes, inspired-by, drugstore and mass-market lines)
BEAUTY_EXCLUDE = [
    "dupe", "inspired by", "impression", "smells like", "smell like", "our version", "alternative to",
    "knock off", "knockoff", "designer inspired", "type fragrance", "fragrance oil", "body spray",
    "body mist", "perfume oil", "unbranded", "generic", "e.l.f", "elf cosmetics", "maybelline",
    "l'oreal", "loreal", "l\u2019or\u00e9al", "nyx", "revlon", "wet n wild", "milani", "covergirl",
    "sephora collection", "essence", "colourpop", "nivea", "jovan", "axe", "avon", "bath & body",
    "bath and body", "calvin klein", "adidas", "body fantasies", "lattafa", "armaf",
]
MAKEUP_SEARCH_TERMS = ["charlotte tilbury", "dior makeup", "nars", "urban decay", "tarte", "fenty beauty",
                       "rare beauty", "too faced", "anastasia beverly hills", "hourglass", "makeup sale"]
PERFUME_SEARCH_TERMS = ["perfume", "eau de parfum", "designer perfume", "chanel perfume", "dior perfume",
                        "ysl libre", "jo malone", "tom ford perfume", "gucci perfume", "fragrance set"]
PERSONALCARE_SEARCH_TERMS = ["makeup remover wipes", "micellar wipes", "deodorant", "native deodorant",
                             "dove deodorant", "secret deodorant"]
UNIT_TARGETS.update({"makeup_wipes_per_ct": 0.12, "deodorant_per_oz": 1.00})

PERFUME_RE = re.compile(r"perfume|parfum|eau de (parfum|toilette|cologne)|cologne|\bedp\b|\bedt\b|fine fragrance|fragrance (set|gift)", re.I)
MAKEUP_RE = re.compile(
    r"makeup|make-up|lipstick|lip (gloss|oil|liner|stain|tint|kit)|mascara|concealer|blush|bronzer|"
    r"highlighter|eye ?shadow|eyeliner|setting (spray|powder)|face powder|liquid foundation|skin tint|"
    r"beauty (set|kit|advent)", re.I)
WIPES_RE = re.compile(r"(makeup|make-up|face|facial|cleansing|micellar|eye makeup) (remover )?wipes|makeup remover", re.I)
DEOD_RE = re.compile(r"deodorant|antiperspirant", re.I)
PREMIUM_BEAUTY_RE = CHEAP_BEAUTY_RE = None

def build_beauty_regex():
    global PREMIUM_BEAUTY_RE, CHEAP_BEAUTY_RE
    PREMIUM_BEAUTY_RE = re.compile(r"(?<![\w])(?:" + "|".join(re.escape(b) for b in BEAUTY_BRANDS) + r")(?![\w])", re.I)
    CHEAP_BEAUTY_RE = re.compile(r"(?<![\w])(?:" + "|".join(re.escape(b) for b in BEAUTY_EXCLUDE) + r")(?![\w])", re.I)

build_beauty_regex()

_CT = re.compile(r"(\d+)\s*(?:ct|count|wipes?)\b", re.I)
_NXM = re.compile(r"(\d+)\s*[x\u00d7]\s*(\d+)\s*(?:ct|count|wipes?)\b", re.I)

def care_unit_hit(title):
    """Makeup wipes per count and deodorant per oz, against your targets."""
    price = title_cost(title)
    if not price:
        return None
    if WIPES_RE.search(title):
        m = _NXM.search(title)
        n = int(m.group(1)) * int(m.group(2)) if m else (int(_CT.search(title).group(1)) if _CT.search(title) else 0)
        per, unit, target = (price / n if n else None), "wipe", UNIT_TARGETS.get("makeup_wipes_per_ct", 0.12)
    elif DEOD_RE.search(title):
        oz = total_weight_oz(title)
        per, unit, target = (price / oz if oz else None), "oz", UNIT_TARGETS.get("deodorant_per_oz", 1.00)
    else:
        return None
    return (per, unit, per / target) if per and per <= target else None

build_search_feeds()


# ---- Favorite brands: alert at a lower discount than the general bar ---------
FAV_BRAND_PCT = {"sonos": 20, "smeg": 25, "ninja": 30, "nespresso": 30, "kitchenaid": 30, "kitchen aid": 30,
                 "hogan": 35, "perigold": 40, "tuft and paw": 30, "tuft & paw": 30, "tuft + paw": 30}
FAVORITE_SEARCH_TERMS = ["hogan", "hogan sneakers", "perigold", "tuft and paw", "sonos", "smeg", "ninja",
                         "ninja creami", "nespresso", "kitchenaid", "kitchenaid stand mixer"]
CAT_SEARCH_TERMS += ["tuft and paw", "mau lifestyle", "refined feline"]
SHOE_SEARCH_TERMS += ["hogan"]
DECOR_SEARCH_TERMS += ["perigold"]

def fav_bar(title):
    t = title.lower()
    hits = [pct for brand, pct in FAV_BRAND_PCT.items() if brand in t]
    return min(hits) if hits else None


# ---- Keep it live: ignore old posts and expired deals -------------------------
FEED_MAX_AGE_HOURS = 48         # ignore deal posts older than this
FLIGHT_MAX_AGE_HOURS = 96       # flight deals last a bit longer
ALERT_MAX_AGE_HOURS = 72        # alerts older than this are dropped from the app
STATS = collections.Counter()     # what the last scan did (shown in the app when no deals qualify)
EXPIRED_RE = re.compile(r"\b(expired|sold out|out of stock|deal (?:has )?ended|no longer available|dead deal)\b", re.I)

def entry_ts(e):
    """When a feed post was published (unix time), or None."""
    t = e.get("published_parsed") or e.get("updated_parsed")
    try:
        return calendar.timegm(t) if t else None
    except Exception:
        return None

# ---- State ----------------------------------------------------------------
def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default

state = load(STATE_FILE, {"seen": {}, "history": {}})
alerts = load(ALERTS_FILE, [])

def already_seen(key):
    if key in state["seen"]:
        return True
    state["seen"][key] = time.time()
    return False

# ---- Resale estimate (eBay Browse API) ------------------------------------
EBAY_ID = os.environ.get("EBAY_CLIENT_ID", "")
EBAY_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "")
EBAY_FEE_PCT = 13.25        # eBay final value fee (most categories)
EBAY_FEE_FIXED = 0.30
SHIP_COST = 12.0            # shipping you assume you'll pay as the seller
RESALE_HAIRCUT = 0.85       # eBay data = asking prices; real sold prices run lower
FB_HAIRCUT = 0.80           # Facebook Marketplace: local, no fees/shipping, lower price
MIN_COMPS = 3               # need at least this many similar listings to estimate
RESALE_URGENT_PROFIT = 150  # make the alert urgent if est. eBay profit >= this ($)
_ebay_token = {"v": None}

def ebay_token():
    if not _ebay_token["v"]:
        r = requests.post("https://api.ebay.com/identity/v1/oauth2/token",
                          auth=(EBAY_ID, EBAY_SECRET), timeout=20,
                          data={"grant_type": "client_credentials",
                                "scope": "https://api.ebay.com/oauth/api_scope"})
        _ebay_token["v"] = r.json()["access_token"]
    return _ebay_token["v"]

def clean_query(title):
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", title)       # drop [GPU] and (Orig $599)
    t = CODE_RE.sub(" ", t)
    t = PRICE_RE.sub(" ", t)
    t = PCT_RE.sub(" ", t)
    t = re.sub(r"[^A-Za-z0-9 .\-/]", " ", t)
    skip = {"off", "at", "for", "from", "w", "with", "and", "free", "shipping",
            "after", "coupon", "code", "-", "+", "was", "now", "only", "extra", "additional",
            "promo", "clip", "checkout", "lowest", "ever", "price", "error", "deal"}
    return " ".join([w for w in t.split() if w.lower() not in skip][:8])

# Reads "$299.99 + $4.99 Shipping" correctly: ignores shipping, "$20 off", "$35+" thresholds, gift cards,
# rebates and other extras, and tells the real price apart from the "was" price.
PRICE_TOKEN = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")
_ORIG_BEFORE = re.compile(r"(?:orig(?:inal)?\.?|was|reg(?:ular)?\.?|retail|msrp|list(?: price)?|value|compare(?: at)?|rrp)\W*$", re.I)
_ORIG_AFTER = re.compile(r"^\s*(?:value|retail|msrp|list)\b", re.I)
_BAD_BEFORE = re.compile(r"(?:\+|\boff|\bover|\bspend|\bof|\bmin(?:imum)?\.?|\bup to|\bsaves?|\bextra|\bgets?|\breceive|\bearn|\bcredit|\bwith a)\W*$", re.I)
_BAD_AFTER = re.compile(
    r"^(?:\+(?!\s)|\s*(?:shipping|s&h|handling|delivery|tax|\/mo|per month|monthly)\b|"
    r"\s*(?:[\w'&-]+\s+){0,2}?(?:gift\s?card|e-?gift|credit|rebate|cash\s?back|rewards?|points|coupon)|"
    r"\s*off\b|\s*(?:or more|and up|minimum|min\b))", re.I)

def parse_prices(title):
    """{'price': the item's price, 'original': the 'was' price} - either may be None."""
    price = original = None
    for m in PRICE_TOKEN.finditer(title):
        val = float(m.group(1).replace(",", ""))
        before, after = title[max(0, m.start() - 18):m.start()], title[m.end():m.end() + 30]
        if _ORIG_BEFORE.search(before) or _ORIG_AFTER.search(after):
            original = max(original or 0, val)
            continue
        if _BAD_BEFORE.search(before) or _BAD_AFTER.search(after):
            continue
        if price is None:
            price = val
    if price is not None and original is not None and original <= price * 1.05:
        original = None
    return {"price": price, "original": original}

def title_cost(title):
    return parse_prices(title)["price"]

def resale_estimate(query, cost):
    """Rough profit if you flip the item, based on current eBay asking prices."""
    if not (EBAY_ID and EBAY_SECRET and query and cost):
        return None
    cache = state.setdefault("ebay_cache", {})
    hit = cache.get(query)
    if hit and time.time() - hit[0] < 86400:
        med = hit[1]
    else:
        try:
            r = requests.get("https://api.ebay.com/buy/browse/v1/item_summary/search",
                             headers={"Authorization": f"Bearer {ebay_token()}",
                                      "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
                             params={"q": query, "limit": 30,
                                     "filter": "buyingOptions:{FIXED_PRICE},conditions:{NEW}"},
                             timeout=25)
            prices = [float(i["price"]["value"]) for i in r.json().get("itemSummaries", [])
                      if i.get("price", {}).get("currency") == "USD"]
        except Exception as e:
            print("ebay error:", e)
            return None
        prices = [x for x in prices if x > cost * 0.3]  # drop parts/accessory noise
        med = statistics.median(prices) if len(prices) >= MIN_COMPS else None
        cache[query] = [time.time(), med]
    if not med:
        return None
    ebay_profit = med * RESALE_HAIRCUT * (1 - EBAY_FEE_PCT / 100) - EBAY_FEE_FIXED - SHIP_COST - cost
    fb_profit = med * FB_HAIRCUT - cost
    return {"median": round(med), "ebay": round(ebay_profit), "fb": round(fb_profit)}

# ---- Coupons / promo codes (read from the deal post text) -------------------
CODE_RE = re.compile(
    r"(?i:promo(?:tion)?\s*code|coupon\s*code|use\s*code|with\s*code|code)[\s:\"'\u201c\u2018]+"
    r"([A-Z0-9][A-Z0-9_-]{3,19})\b")
BAD_CODES = {"FREE", "SHIPPING", "OFF", "SALE", "WITH", "FROM", "AND", "THE", "ONLY", "DEAL", "TODAY"}
EXTRA_PCT_RE = re.compile(r"(?:extra|additional|take|save)\s+(\d{1,2})%\s*off", re.I)
EXTRA_USD_RE = re.compile(r"\$(\d{1,3})\s*off\b", re.I)
COUPON_WORD_RE = re.compile(r"coupon|promo|\bcode\b|clip", re.I)
CLIP_RE = re.compile(r"\bclip\b[^.]{0,25}coupon|clippable|on[- ]page coupon|coupon (?:at|on) checkout|auto[- ]applied", re.I)

def coupon_info(text, price):
    """Pull a promo code / coupon out of the post text. Codes are as posted and may be expired."""
    text = re.sub(r"<[^>]+>", " ", text)
    codes = [c for c in CODE_RE.findall(text) if c.upper() not in BAD_CODES and not c.isdigit()]
    clip = bool(CLIP_RE.search(text))
    if not (codes or clip):
        return None
    pct = usd = after = None
    if COUPON_WORD_RE.search(text):
        m = EXTRA_PCT_RE.search(text)
        pct = int(m.group(1)) if m else None
        m = EXTRA_USD_RE.search(text)
        usd = int(m.group(1)) if (m and not pct) else None
    if price and pct:
        after = round(price * (1 - pct / 100), 2)
    elif price and usd:
        after = round(max(price - usd, 0), 2)
    return {"code": codes[0] if codes else None, "pct": pct, "usd": usd, "clip": clip, "price_after": after}

# ---- Price comparison across stores (Google Shopping via SerpApi) -----------
COMPARE_DAILY_BUDGET = 10   # comparison searches per day (each costs 1 SerpApi credit)
COMPARE_MAX_STORES = 8
COMPARE_VERIFY_PCT = 30     # "Verified" if the deal is this % below the market median

def compare_prices(query, deal_price):
    """Current prices for the same product at other stores, cheapest first."""
    if not (SERPAPI_KEY and query):
        return None
    cache = state.setdefault("compare_cache", {})
    hit = cache.get(query)
    if hit and time.time() - hit[0] < 12 * 3600:
        return hit[1]
    today = datetime.date.today().isoformat()
    budget = state.setdefault("compare_budget", {"day": "", "n": 0})
    if budget["day"] != today:
        budget.update({"day": today, "n": 0})
    if budget["n"] >= COMPARE_DAILY_BUDGET:
        return None
    budget["n"] += 1
    try:
        data = requests.get("https://serpapi.com/search.json", timeout=60, params={
            "engine": "google_shopping", "q": query, "gl": "us", "hl": "en",
            "api_key": SERPAPI_KEY}).json()
    except Exception as e:
        print("compare error:", e)
        return None
    items = []
    for it in data.get("shopping_results", []):
        price, store = it.get("extracted_price"), it.get("source")
        if not price or not store or it.get("second_hand_condition"):
            continue
        items.append({"store": store, "price": round(float(price), 2),
                      "url": it.get("link") or it.get("product_link")})
    best = {}
    for i in items:
        if i["store"] not in best or i["price"] < best[i["store"]]["price"]:
            best[i["store"]] = i
    stores = sorted(best.values(), key=lambda x: x["price"])[:COMPARE_MAX_STORES]
    prices = [i["price"] for i in items]
    thumb = next((good_image(it.get("thumbnail")) for it in data.get("shopping_results", [])
                  if good_image(it.get("thumbnail"))), None)
    res = {"stores": stores, "median": round(statistics.median(prices), 2) if len(prices) >= 3 else None,
           "thumb": thumb}
    cache[query] = [time.time(), res]
    return res

# ---- Deal photos --------------------------------------------------------------
IMG_TAG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)
OG_RE = re.compile(
    r"<meta[^>]+(?:property|name)=[\"'](?:og:image|twitter:image)[\"'][^>]*content=[\"']([^\"']+)[\"']|"
    r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*(?:property|name)=[\"'](?:og:image|twitter:image)[\"']", re.I)
BAD_IMG_RE = re.compile(r"pixel|spacer|emoji|avatar|favicon|logo|sprite|1x1|feedburner|\.gif(\?|$)|\.svg(\?|$)|/icons?/|"
                        r"placeholder|no-?image|noimage|coming-?soon|default[-_]|communityicon|styles\.redditmedia|award|badge|banner", re.I)
OG_FETCH_BUDGET = 30            # product-page photo lookups per run (for posts with no image)
NO_PHOTO = []                   # titles of alerts that ended up without a real photo (logged each run)
_og_used = [0]

def good_image(u):
    if not u:
        return None
    u = unescape(str(u)).strip()
    if u.startswith("//"):
        u = "https:" + u
    if u.startswith("http://"):
        u = "https://" + u[7:]
    return u if u.startswith("https://") and not BAD_IMG_RE.search(u) else None

def entry_images(e):
    """All usable photo URLs in an RSS/Atom entry (media tags, enclosures, <img> tags), in order."""
    found = []
    def add(u):
        u = good_image(u)
        if u and u not in found:
            found.append(u)
    for key in ("media_thumbnail", "media_content"):
        for m in e.get(key) or []:
            add(m.get("url"))
    for enc in (e.get("enclosures") or []) + (e.get("links") or []):
        if str(enc.get("type", "")).startswith("image"):
            add(enc.get("href"))
    html = (e.get("summary") or "") + " ".join(c.get("value", "") for c in (e.get("content") or []))
    for m in IMG_TAG_RE.finditer(html):
        add(m.group(1))
    return found[:8]

def entry_image(e):
    imgs = entry_images(e)
    return imgs[0] if imgs else None

def bb_images(p):
    """Best Buy product photos: main shot first, then other angles."""
    keys = ("largeFrontImage", "image", "angleImage", "backViewImage", "leftViewImage",
            "rightViewImage", "alternateViewsImage")
    out = []
    for k in keys:
        u = good_image(p.get(k))
        if u and u not in out:
            out.append(u)
    return out[:6]

def og_image(url):
    """Fallback: the page's preview image (og:image) for a deal link."""
    if _og_used[0] >= OG_FETCH_BUDGET or not url:
        return None
    _og_used[0] += 1
    try:
        html = requests.get(url, headers=UA, timeout=8).text[:250000]
        m = OG_RE.search(html)
        return good_image(urljoin(url, (m.group(1) or m.group(2)))) if m else None
    except Exception:
        return None

# ---- Your style profile (style.json): brands/keywords you like -----------------
_style_cache = {}

def load_style(force=False):
    if force or not _style_cache:
        d = load(STYLE_FILE, {})
        def terms(key):
            return [str(t).strip().lower() for t in d.get(key, []) if str(t).strip()]
        _style_cache.clear()
        _style_cache.update({"brands": terms("brands"), "keywords": terms("keywords"), "avoid": terms("avoid")})
    return _style_cache

def _has_term(text, term):
    return re.search(r"(?<![\w])" + re.escape(term) + r"(?![\w])", text) is not None

def style_hits(title):
    """Brands/keywords from your style profile that appear in a deal title (max 3)."""
    st, t = load_style(), title.lower()
    hits = [x for x in st["brands"] + st["keywords"] if _has_term(t, x)]
    return list(dict.fromkeys(hits))[:3]

def style_avoided(title):
    t = title.lower()
    return any(_has_term(t, x) for x in load_style()["avoid"])

# ---- "Hotness" score: ranks deals for the hero card, widget and email --------
def hot_score(a):
    base = a.get("pct") or 35
    if a.get("verified"):
        base += 15
    if a.get("urgent"):
        base += 20
    if (a.get("profit_ebay") or 0) > 0:
        base += min(a["profit_ebay"] / 10, 25)
    if a.get("coupon"):
        base += 5
    if a.get("market_savings") is not None and a["market_savings"] <= 0:
        base -= 25                       # not actually cheaper than other stores
    base += min(10 * len(a.get("style_terms") or []), 20)       # matches your style profile
    if style_avoided(a.get("title", "")):
        base -= 30
    return round(base, 1)

def decayed_score(a, now=None):
    now = now or time.time()
    return (a.get("score") or hot_score(a)) * 0.5 ** ((now - a["ts"]) / 3600 / 36)

def money(n):
    return f"-${abs(n)}" if n < 0 else f"${n}"

def push(category, source, title, detail, url=None, urgent=False, pct=None,
         cost=None, query=None, text="", image=None, images=None, credit=None, posted=None, was=None):
    est = resale_estimate(query, cost)
    deal_price = cost or title_cost(title)
    was_price = was or parse_prices(title)["original"]
    if not was_price:
        wm = re.search(r"was \$([\d,]+(?:\.\d+)?)", detail or "")
        was_price = float(wm.group(1).replace(",", "")) if wm else None
    cmp_ = None if category == "flights" else compare_prices(query or clean_query(title), deal_price)
    gallery = []
    for u in [image] + list(images or []):
        u = good_image(u)
        if u and u not in gallery:
            gallery.append(u)
    image = gallery[0] if gallery else None
    if not image and category != "flights":
        image = og_image(url) or (good_image(cmp_.get("thumb")) if cmp_ else None)
    if not image and category != "flights":
        NO_PHOTO.append(title)
    mkt = cmp_["median"] if cmp_ else None
    save = round((1 - deal_price / mkt) * 100) if (mkt and deal_price) else None
    verified = save is not None and save >= COMPARE_VERIFY_PCT
    cp = None if category == "flights" else coupon_info(title + " " + text, deal_price)
    lines = []
    if est:
        lines.append(f"Est. profit: eBay ~{money(est['ebay'])}, FB local ~{money(est['fb'])} "
                     f"(similar listings ~${est['median']})")
        if est["ebay"] >= RESALE_URGENT_PROFIT:
            urgent = True
    if mkt and save is not None:
        lines.append(f"Other stores: median ${mkt:.0f} ({'about ' + str(save) + '% below' if save > 0 else 'NOT below'} market)")
    if cp:
        c = f"Code {cp['code']}" if cp["code"] else "Clip on-page coupon"
        if cp["price_after"]:
            c += f" (about ${cp['price_after']:.2f} after)"
        lines.append(c)
    find = None
    if cmp_ and cmp_["stores"]:
        find = "https://www.google.com/search?q=" + quote_plus(cmp_["stores"][0]["store"] + " promo code")
    print(f"[ALERT] {source}: {title} | {detail} | " + " | ".join(lines))
    entry = {"ts": int(min(posted, time.time())) if posted else int(time.time()), "category": category, "source": source,
                      "title": title, "detail": detail, "url": url,
                      "urgent": urgent, "pct": pct, "deal_price": deal_price, "was_price": was_price,
                      "profit_ebay": est["ebay"] if est else None,
                      "profit_fb": est["fb"] if est else None,
                      "resale_median": est["median"] if est else None,
                      "compare": cmp_["stores"] if cmp_ else None,
                      "market_median": mkt, "market_savings": save, "verified": verified,
                      "coupon": cp, "find_codes": find, "image": image, "images": [], "image_credit": credit,
                      "style_terms": style_hits(title)}
    if image and image not in gallery:
        gallery.insert(0, image)          # fallback photo found after the gallery was built
    entry["images"] = gallery[:8]
    entry["score"] = hot_score(entry)
    entry["caution"] = None
    if category in ("perfume", "makeup") and ((pct or 0) >= 60 or (save or 0) >= 60):
        entry["caution"] = "Very low for authentic beauty. Confirm the seller is authorized."
        lines.append("Caution: " + entry["caution"])
    alerts.insert(0, entry)
    if not NTFY_TOPIC:
        return
    headers = {"Title": f"{title} ({source})".encode("utf-8"),
               "Priority": "5" if urgent else "4",
               "Tags": "rotating_light" if urgent else "moneybag"}
    if url:
        headers["Click"] = url
    body = detail + ("\n" + "\n".join(lines) if lines else "")
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode("utf-8"),
                      headers=headers, timeout=15)
    except Exception as e:
        print("ntfy error:", e)

# ---- 1. Deal feeds ---------------------------------------------------------
PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")
PCT_RE = re.compile(r"(\d{2,3})\s?%\s?off", re.I)

def estimate_discount(title):
    """% off from the title: price vs 'was' price, else an explicit '40% off'. Never guesses from stray amounts."""
    pp = parse_prices(title)
    if pp["price"] and pp["original"]:
        pct = round((1 - pp["price"] / pp["original"]) * 100)
        if 5 <= pct <= 90:
            return pct
    m = PCT_RE.search(title)
    if m and int(m.group(1)) <= 95 and not re.search(r"up to\s*$", title[max(0, m.start() - 8):m.start()], re.I):
        return int(m.group(1))
    return None

def deal_discount(title, summary=""):
    """(pct off, 'was' price). Also looks in the post summary for a 'was' price when the title has only a price."""
    pp = parse_prices(title)
    orig = pp["original"]
    if pp["price"] and not orig and summary:
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(summary)))[:600]
        so = parse_prices(text)["original"]
        if so and so > pp["price"] * 1.05:
            orig = so
    if pp["price"] and orig:
        pct = round((1 - pp["price"] / orig) * 100)
        if 5 <= pct <= 90:
            return pct, orig
    return estimate_discount(title), None

def home_unit_hit(title):
    """Return (price_per_unit, unit) if a household deal beats your per-unit target."""
    price = title_cost(title)
    if not price:
        return None
    for item_re, count_re, unit, target in HOME_UNIT_TARGETS:
        if item_re.search(title):
            if count_re is None:
                n = 1.0
            else:
                m = count_re.search(title)
                n = float(m.group(1)) if m else 0
            if n > 0 and price / n <= target:
                return price / n, unit, (price / n) / target
    return None

def jewelry_status(title):
    """'real' for karat gold / sterling / platinum / diamonds / genuine gems; else 'fake'/'unknown'."""
    if FAKE_RE.search(title):
        return "fake"
    return "real" if REAL_RE.search(title) else "unknown"

def classify(title, summary, feed_cat):
    """Decide (category, alert bar %, note) for a deal post, or None to skip it."""
    text = title + " " + summary
    mens = bool(MENS_RE.search(title)) and not WOMENS_RE.search(title)
    electronics = bool(ELECTRONICS_RE.search(title))
    luxury = bool(LUXURY_BRANDS.search(title) or DESIGNER_RE.search(title))
    if feed_cat == "home":
        return "home", HOME_PCT_THRESHOLD, ""
    if (feed_cat == "shoes" or SHOE_RE.search(title)) and not electronics:
        if mens:
            return None
        size = size_status(text)
        if size == "no":
            return None
        note = "Size " + "/".join(f"{x:g}" for x in SHOE_SIZES) + (": listed" if size == "ok" else ": not stated, check")
        return "shoes", min(PCT_THRESHOLD, SHOE_PCT_THRESHOLD), note
    beauty_kw = bool(PERFUME_RE.search(title) or MAKEUP_RE.search(title) or WIPES_RE.search(title) or DEOD_RE.search(title))
    if (feed_cat in ("makeup", "perfume", "personalcare") or
            (beauty_kw and feed_cat not in ("pets", "cats", "appliances", "jewelry", "clothing", "handbags", "shoes"))) and not electronics:
        if WIPES_RE.search(title) or DEOD_RE.search(title) or feed_cat == "personalcare":
            if mens:
                return None
            return "personalcare", PERSONALCARE_PCT_THRESHOLD, ""
        if CHEAP_BEAUTY_RE.search(title) or not PREMIUM_BEAUTY_RE.search(title):
            return None                      # premium brands only; no dupes or drugstore lines
        if PERFUME_RE.search(title):
            return "perfume", PERFUME_PCT_THRESHOLD, ""
        return "makeup", MAKEUP_PCT_THRESHOLD, ""
    if (feed_cat == "handbags" or HANDBAG_RE.search(title)) and not electronics:
        if not luxury:
            return None                      # designer bags only
        return "handbags", HANDBAG_PCT_THRESHOLD, ""
    if feed_cat == "jewelry" or JEWELRY_RE.search(title):
        st = jewelry_status(title)
        if st != "real":
            return None                      # plated / costume / unclear: skip
        return "jewelry", JEWELRY_PCT_THRESHOLD, "Real: " + REAL_RE.search(title).group(0)
    decor_brand = bool(DECOR_BRAND_RE.search(title))
    anthro = bool(re.search(r"anthropologie", title, re.I))
    home_words = bool(HOME_WORDS_RE.search(title))
    if decor_brand or (anthro and home_words) or (feed_cat == "decor" and home_words):
        return "decor", DECOR_PCT_THRESHOLD, ""
    if feed_cat == "decor":
        return None
    fashion_ok = luxury or bool(RETAIL_FASHION_RE.search(title))
    if (feed_cat == "clothing" or CLOTHING_RE.search(title)) and fashion_ok and not electronics:
        if mens:
            return None
        size = clothing_size_status(text)
        if size == "no":
            return None
        note = "Size S / 4 / 6" + (": listed" if size == "ok" else ": not stated, check")
        return "clothing", CLOTHING_PCT_THRESHOLD, note
    if feed_cat == "clothing":
        return None
    if feed_cat == "underwear" or UNDERWEAR_RE.search(title):
        if mens:
            return None
        return "underwear", UNDERWEAR_PCT_THRESHOLD, ""
    if feed_cat == "appliances" or APPLIANCE_RE.search(title):
        return "appliances", APPLIANCE_PCT_THRESHOLD, ""
    if feed_cat == "pets" or PET_RE.search(title):
        return "pets", PET_PCT_THRESHOLD, ""
    if feed_cat == "cats" or CAT_RE.search(title):
        return "cats", (CAT_LUXURY_PCT_THRESHOLD if CAT_LUX_RE.search(title) else CAT_PCT_THRESHOLD), ""
    if APPLE_RE.search(title):
        return "apple", (APPLE_FAV_PCT if APPLE_FAV_RE.search(title) else APPLE_PCT_THRESHOLD), ""
    if LUXURY_BRANDS.search(title):
        return "fashion", LUXURY_PCT_THRESHOLD, ""
    return feed_cat, PCT_THRESHOLD, ""

def check_feeds():
    now = time.time()
    last = state.setdefault("last_run", {})
    due = {c: now - last.get(c, 0) > h * 3600 for c, h in THROTTLE_HOURS.items()}
    for name, (url, cat) in FEEDS.items():
        if cat in due and not due[cat]:
            continue
        try:
            resp = requests.get(url, headers=UA, timeout=20)
            if cat in THROTTLE_HOURS:
                time.sleep(0.4)                  # be polite to search feeds
            if resp.status_code != 200:
                print(f"feed {name}: HTTP {resp.status_code} (some sites block cloud servers; see README)")
                STATS["feeds_blocked"] += 1
                continue
            feed = feedparser.parse(resp.content)
            STATS["feeds_read"] += 1
        except Exception as e:
            print(f"feed error {name}: {e}")
            STATS["feeds_blocked"] += 1
            continue
        for e in feed.entries[:40]:
            title, link = e.get("title", ""), e.get("link", "")
            STATS["posts"] += 1
            posted = entry_ts(e)
            if posted and time.time() - posted > FEED_MAX_AGE_HOURS * 3600:
                STATS["too_old"] += 1
                continue                         # old post (search feeds return old deals too)
            if EXPIRED_RE.search(title + " " + (e.get("summary") or "")[:300]):
                STATS["expired"] += 1
                continue                         # marked expired / sold out
            if not title or already_seen(link or title):
                STATS["already_seen"] += 1
                continue
            pct, was = deal_discount(title, e.get("summary", ""))
            is_error = bool(ERROR_WORDS.search(title))
            res = classify(title, e.get("summary", ""), cat)
            if res is None:
                STATS["filtered_out"] += 1
                continue
            category, bar, note = res
            if BRANDS_RE and BRANDS_RE.search(title):
                bar = min(bar, BRAND_PCT_THRESHOLD)
            fb = fav_bar(title)
            if fb:
                bar = min(bar, fb)
            if cat == "favorites" and category == cat and not re.search(r"\bsonos\b", title, re.I):
                STATS["not_your_interests"] += 1
                continue
            if (name in GENERAL_FEEDS or cat in ("clearance", "favorites")) and category == cat and not (ELECTRONICS_RE.search(title) or is_error):
                STATS["not_your_interests"] += 1
                continue                         # general feed item that matches none of your interests
            if category in ("clearance", "favorites"):
                category = "electronics"
            unit_hit = (home_unit_hit(title) if category == "home"
                        else pet_unit_hit(title) if category == "pets"
                        else care_unit_hit(title) if category == "personalcare" else None)
            if is_error or unit_hit or (pct is not None and pct >= bar):
                if is_error:
                    detail = "Possible price error"
                elif unit_hit:
                    detail = (f"${unit_hit[0]:.2f} per {unit_hit[1]}" + (f" ({pct}% off)" if pct else "")
                              + (" | stock-up price" if unit_hit[2] <= 0.8 else ""))
                else:
                    detail = f"{pct}% off"
                if note:
                    detail += " | " + note
                flip = category not in NO_RESALE
                push(category, name, title, detail, link,
                     urgent=is_error or (pct or 0) >= 70, pct=pct,
                     cost=title_cost(title) if flip else None,
                     query=clean_query(title) if flip else None,
                     text=e.get("summary", ""), images=entry_images(e), posted=posted, was=was)
                STATS["alerted"] += 1
            else:
                STATS["below_your_bar"] += 1
    print("feed scan:", dict(STATS))
    for c, d in due.items():
        if d:
            last[c] = now

# ---- 2. Best Buy API -------------------------------------------------------
def check_bestbuy():
    if not BESTBUY_KEY:
        return
    q = f"(onSale=true&percentSavings>={BESTBUY_PCT}&regularPrice>={BESTBUY_MIN_REGULAR}&condition=new)"
    try:
        r = requests.get(f"https://api.bestbuy.com/v1/products{q}", timeout=30, params={
            "apiKey": BESTBUY_KEY, "format": "json", "pageSize": 50,
            "sort": "percentSavings.dsc",
            "show": "sku,name,salePrice,regularPrice,percentSavings,url,image,largeFrontImage,angleImage,backViewImage,leftViewImage,rightViewImage,alternateViewsImage"})
        products = r.json().get("products", [])
    except Exception as e:
        print("bestbuy error:", e)
        return
    for p in products:
        if already_seen(f"bb:{p['sku']}:{p['salePrice']}"):
            continue
        pct = round(p.get("percentSavings") or 0)
        push("electronics", "Best Buy", p["name"],
             f"${p['salePrice']:.2f} (was ${p['regularPrice']:.2f}, {pct}% off)",
             p.get("url"), urgent=pct >= 70, pct=pct,
             cost=p["salePrice"], query=p["name"], images=bb_images(p))

def check_bestbuy_apple():
    """Every Apple product Best Buy sells, at a much lower discount bar."""
    if not BESTBUY_KEY:
        return
    q = f"(manufacturer=Apple&onSale=true&percentSavings>={BESTBUY_APPLE_PCT}&condition=new)"
    try:
        r = requests.get(f"https://api.bestbuy.com/v1/products{q}", timeout=30, params={
            "apiKey": BESTBUY_KEY, "format": "json", "pageSize": 100,
            "sort": "percentSavings.dsc",
            "show": "sku,name,salePrice,regularPrice,percentSavings,url,image,largeFrontImage,angleImage,backViewImage,leftViewImage,rightViewImage,alternateViewsImage"})
        products = r.json().get("products", [])
    except Exception as e:
        print("bestbuy apple error:", e)
        return
    for p in products:
        if already_seen(f"bb:{p['sku']}:{p['salePrice']}"):
            continue
        pct = round(p.get("percentSavings") or 0)
        push("apple", "Best Buy", p["name"],
             f"${p['salePrice']:.2f} (was ${p['regularPrice']:.2f}, {pct}% off)",
             p.get("url"), urgent=pct >= 25, pct=pct,
             cost=p["salePrice"], query=p["name"], images=bb_images(p))

def check_bestbuy_appliances():
    """iRobot, Tineco, Dyson, Shark, Roborock, Bissell, Eufy on sale at Best Buy."""
    if not BESTBUY_KEY:
        return
    mf = "|".join(f"manufacturer={m}" for m in
                  ("iRobot", "Tineco", "Dyson", "Shark", "Roborock", "Bissell", "Eufy"))
    q = f"(onSale=true&percentSavings>={APPLIANCE_PCT_THRESHOLD}&condition=new&({mf}))"
    try:
        r = requests.get(f"https://api.bestbuy.com/v1/products{q}", timeout=30, params={
            "apiKey": BESTBUY_KEY, "format": "json", "pageSize": 50, "sort": "percentSavings.dsc",
            "show": "sku,name,salePrice,regularPrice,percentSavings,url,image,largeFrontImage,angleImage,backViewImage,leftViewImage,rightViewImage,alternateViewsImage"})
        products = r.json().get("products", [])
    except Exception as e:
        print("bestbuy appliances error:", e)
        return
    for p in products:
        if already_seen(f"bb:{p['sku']}:{p['salePrice']}"):
            continue
        pct = round(p.get("percentSavings") or 0)
        push("appliances", "Best Buy", p["name"],
             f"${p['salePrice']:.2f} (was ${p['regularPrice']:.2f}, {pct}% off)",
             p.get("url"), urgent=pct >= 60, pct=pct, cost=p["salePrice"], query=p["name"], images=bb_images(p))

def check_bestbuy_clearance():
    """Best Buy clearance items (new condition) at steep discounts."""
    if not BESTBUY_KEY:
        return
    q = f"(clearance=true&percentSavings>={BESTBUY_CLEARANCE_PCT}&regularPrice>={BESTBUY_MIN_REGULAR}&condition=new)"
    try:
        r = requests.get(f"https://api.bestbuy.com/v1/products{q}", timeout=30, params={
            "apiKey": BESTBUY_KEY, "format": "json", "pageSize": 50, "sort": "percentSavings.dsc",
            "show": "sku,name,salePrice,regularPrice,percentSavings,url,image,largeFrontImage,angleImage,backViewImage,leftViewImage,rightViewImage,alternateViewsImage"})
        products = r.json().get("products", [])
    except Exception as e:
        print("bestbuy clearance error:", e)
        return
    for p in products:
        if already_seen(f"bbc:{p['sku']}:{p['salePrice']}"):
            continue
        pct = round(p.get("percentSavings") or 0)
        res = classify(p["name"], "", "electronics")
        cat = res[0] if res else "electronics"
        push(cat, "Best Buy clearance", p["name"],
             f"Clearance ${p['salePrice']:.2f} (was ${p['regularPrice']:.2f}, {pct}% off)",
             p.get("url"), urgent=pct >= 70, pct=pct, cost=p["salePrice"], query=p["name"], images=bb_images(p))

def check_bestbuy_openbox():
    """Best Buy open-box listings (beta API; parsing is tolerant of field differences)."""
    if not BESTBUY_KEY:
        return
    for page in range(1, OPENBOX_PAGES + 1):
        try:
            r = requests.get("https://api.bestbuy.com/beta/products/openBox", timeout=40, params={
                "apiKey": BESTBUY_KEY, "pageSize": 100, "page": page})
            results = r.json().get("results", [])
        except Exception as e:
            print("bestbuy open box error:", e)
            return
        if not results:
            return
        for it in results:
            title = (it.get("names") or {}).get("title") or it.get("name") or ""
            sku = it.get("sku")
            regular = (it.get("prices") or {}).get("regular") or 0
            link = (it.get("links") or {}).get("web") or (it.get("links") or {}).get("product") or it.get("url")
            best = None
            for off in it.get("offers") or []:
                pr = off.get("prices") or {}
                cur, reg = pr.get("current"), pr.get("regular") or regular
                if cur and reg and (best is None or cur < best[0]):
                    best = (cur, reg, off.get("condition", "open box"))
            if not (title and best):
                continue
            cur, reg, cond = best
            pct = round((1 - cur / reg) * 100) if reg else 0
            if pct < OPENBOX_PCT or reg < BESTBUY_MIN_REGULAR:
                continue
            if not (ELECTRONICS_RE.search(title) or APPLE_RE.search(title) or APPLIANCE_RE.search(title)):
                continue
            if already_seen(f"bbo:{sku}:{cur}"):
                continue
            res = classify(title, "", "electronics")
            push(res[0] if res else "electronics", "Best Buy open box", title,
                 f"Open box ({cond}) ${cur:.2f} (was ${reg:.2f}, {pct}% off)",
                 link, urgent=pct >= 60, pct=pct, cost=cur, query=title,
                 image=(it.get("images") or {}).get("standard") or it.get("image"))

def check_bestbuy_favorites():
    """Sonos, Smeg, Ninja, Nespresso and KitchenAid on sale at Best Buy, at each brand's own bar."""
    if not BESTBUY_KEY:
        return
    mf = "|".join(f"manufacturer={m}" for m in ("Sonos", "Smeg", "Ninja", "Nespresso", "KitchenAid"))
    q = f"(onSale=true&percentSavings>=15&regularPrice>={BESTBUY_MIN_REGULAR}&condition=new&({mf}))"
    try:
        r = requests.get(f"https://api.bestbuy.com/v1/products{q}", timeout=30, params={
            "apiKey": BESTBUY_KEY, "format": "json", "pageSize": 100, "sort": "percentSavings.dsc",
            "show": "sku,name,salePrice,regularPrice,percentSavings,url,image,largeFrontImage,angleImage,backViewImage,leftViewImage,rightViewImage,alternateViewsImage"})
        products = r.json().get("products", [])
    except Exception as e:
        print("bestbuy favorites error:", e)
        return
    for p in products:
        pct = round(p.get("percentSavings") or 0)
        bar = fav_bar(p["name"])
        if bar is None or pct < bar or already_seen(f"bbf:{p['sku']}:{p['salePrice']}"):
            continue
        res = classify(p["name"], "", "electronics")
        push(res[0] if res else "electronics", "Best Buy", p["name"],
             f"${p['salePrice']:.2f} (was ${p['regularPrice']:.2f}, {pct}% off)",
             p.get("url"), urgent=pct >= 45, pct=pct, cost=p["salePrice"], query=p["name"], images=bb_images(p))

# ---- 3. Amazon via Keepa ---------------------------------------------------
def check_amazon():
    if not (KEEPA_KEY and AMAZON_WATCHLIST):
        return
    try:
        r = requests.get("https://api.keepa.com/product", timeout=30, params={
            "key": KEEPA_KEY, "domain": 1, "asin": ",".join(AMAZON_WATCHLIST), "stats": 90})
        products = r.json().get("products", [])
    except Exception as e:
        print("keepa error:", e)
        return
    for p in products:
        st = p.get("stats") or {}
        cur, avg = st.get("current", []), st.get("avg90", [])
        for idx in (0, 1):  # 0 = Amazon, 1 = new 3rd-party (cents, -1 = none)
            if len(cur) > idx and len(avg) > idx and cur[idx] > 0 and avg[idx] > 0:
                drop = (1 - cur[idx] / avg[idx]) * 100
                if drop >= AMAZON_VS_AVG_PCT and not already_seen(f"{p['asin']}:{cur[idx]}"):
                    push("amazon", "Amazon", AMAZON_WATCHLIST.get(p["asin"], p["asin"]),
                         f"${cur[idx]/100:.2f} vs 90-day avg ${avg[idx]/100:.2f}",
                         f"https://www.amazon.com/dp/{p['asin']}",
                         urgent=drop >= 50, pct=round(drop),
                         cost=cur[idx] / 100,
                         query=AMAZON_WATCHLIST.get(p["asin"], p["asin"]),
                         images=["https://m.media-amazon.com/images/I/" + n
                                 for n in (p.get("imagesCSV") or "").split(",") if n][:6])
                break

# ---- 4. Product URL watcher ------------------------------------------------
LD_RE = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
META_RE = re.compile(r'<meta[^>]+(?:product:price:amount|og:price:amount)[^>]+content="([\d.,]+)"', re.I)

def _find_price(node):
    if isinstance(node, dict):
        offers = node.get("offers")
        if offers:
            for o in (offers if isinstance(offers, list) else [offers]):
                if isinstance(o, dict):
                    v = o.get("price") or o.get("lowPrice")
                    if v:
                        try:
                            return float(str(v).replace(",", ""))
                        except ValueError:
                            pass
        for v in node.values():
            found = _find_price(v)
            if found:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find_price(v)
            if found:
                return found
    return None

def page_price(url):
    html = requests.get(url, headers=UA, timeout=25).text
    for block in LD_RE.findall(html):
        try:
            price = _find_price(json.loads(block))
            if price:
                return price
        except Exception:
            continue
    m = META_RE.search(html)
    return float(m.group(1).replace(",", "")) if m else None

def check_urls():
    for url, label in URL_WATCHLIST.items():
        try:
            price = page_price(url)
        except Exception as e:
            print(f"url error {label}: {e}")
            continue
        if not price:
            print(f"no price found for {label} (site may block bots)")
            continue
        hist = state["history"].setdefault(url, [])
        prices = [h[1] for h in hist[-60:]]
        hist.append([int(time.time()), price])
        del hist[:-200]
        if len(prices) >= 3:
            med = statistics.median(prices)
            drop = (1 - price / med) * 100
            if drop >= URL_DROP_PCT and not already_seen(f"url:{url}:{price}"):
                push("watchlist", "Watchlist", label,
                     f"${price:.2f} vs usual ${med:.2f} ({drop:.0f}% below)",
                     url, urgent=drop >= 50, pct=round(drop), cost=price, query=label)

# ---- Destination photos for flights (Wikipedia) ----------------------------
FLIGHT_DEST_WIKI = {
    "LHR": "London", "CDG": "Paris", "AMS": "Amsterdam", "FRA": "Frankfurt", "MAD": "Madrid",
    "BCN": "Barcelona", "FCO": "Rome", "LIS": "Lisbon", "DUB": "Dublin", "KEF": "Reykjav\u00edk",
    "ZRH": "Z\u00fcrich", "IST": "Istanbul", "ATH": "Athens", "CUN": "Canc\u00fan", "PUJ": "Punta Cana",
    "NAS": "Nassau, Bahamas", "MBJ": "Montego Bay", "SJO": "San Jos\u00e9, Costa Rica", "PTY": "Panama City",
    "BOG": "Bogot\u00e1", "LIM": "Lima", "GRU": "S\u00e3o Paulo", "EZE": "Buenos Aires", "MEX": "Mexico City",
    "YYZ": "Toronto", "NRT": "Tokyo", "ICN": "Seoul", "DOH": "Doha", "DXB": "Dubai",
}
NOT_A_PHOTO_RE = re.compile(r"flag|coat_of_arms|coat of arms|\bmap\b|locator|seal_of|logo|emblem", re.I)

def destination_photo(code):
    """A skyline/landmark photo for a flight destination, cached after the first lookup."""
    cache = state.setdefault("dest_photos", {})
    if cache.get(code):
        return cache[code]
    title = FLIGHT_DEST_WIKI.get(code) or FLIGHT_DESTINATIONS.get(code)
    if not title:
        return None
    try:
        r = requests.get("https://en.wikipedia.org/w/api.php", headers=UA, timeout=15, params={
            "action": "query", "prop": "pageimages", "format": "json", "piprop": "thumbnail",
            "pithumbsize": 900, "titles": title, "redirects": 1})
        for pg in ((r.json().get("query") or {}).get("pages") or {}).values():
            name = pg.get("pageimage") or ""
            url = good_image((pg.get("thumbnail") or {}).get("source"))
            if url and not NOT_A_PHOTO_RE.search(name):
                cache[code] = url
                return url
    except Exception as e:
        print("destination photo error:", e)
    return None

# ---- 5. Flights from Tampa ---------------------------------------------------
def check_flight_feeds():
    """Free deal feeds: keep only Tampa-origin, non-domestic posts (all destinations)."""
    for name, url in FLIGHT_FEEDS.items():
        try:
            resp = requests.get(url, headers=UA, timeout=20)
            if resp.status_code != 200:
                print(f"flight feed {name}: HTTP {resp.status_code}")
                continue
            feed = feedparser.parse(resp.content)
        except Exception as e:
            print(f"flight feed error {name}: {e}")
            continue
        for e in feed.entries[:40]:
            title, link = e.get("title", ""), e.get("link", "")
            text = title + " " + e.get("summary", "")
            if not title or not TPA_RE.search(text) or DOMESTIC_RE.search(title):
                continue
            posted = entry_ts(e)
            if posted and time.time() - posted > FLIGHT_MAX_AGE_HOURS * 3600:
                continue
            if EXPIRED_RE.search(text[:400]) or already_seen(link or title):
                continue
            err = bool(ERROR_WORDS.search(text))
            price = title_cost(title)
            push("flights", name, title,
                 (f"From ${price:.0f}. " if price else "") +
                 ("Possible mistake fare. " if err else "") +
                 "Layovers not verified, check before booking.",
                 link, urgent=err, images=entry_images(e), posted=posted)

def _flight_ok(opt):
    lay = opt.get("layovers") or []
    return len(lay) <= 1 and all((l.get("duration") or 9999) < MAX_LAYOVER_MIN for l in lay)

def check_flights():
    """Google Flights data via SerpApi: international, nonstop or 1 stop with short layover."""
    if not SERPAPI_KEY:
        return
    today = datetime.date.today()
    budget = state.setdefault("flight_budget", {"day": "", "n": 0})
    if budget["day"] != today.isoformat():
        budget.update({"day": today.isoformat(), "n": 0})
    checked = state.setdefault("flight_checked", {})
    keys = [(d, off) for d in FLIGHT_DESTINATIONS for off in FLIGHT_DEPART_OFFSETS]
    keys.sort(key=lambda k: checked.get(f"{k[0]}:{k[1]}", 0))   # least recently checked first
    done = 0
    for dest, off in keys:
        if done >= FLIGHT_MAX_PER_RUN or budget["n"] >= FLIGHT_DAILY_BUDGET:
            break
        ck = f"{dest}:{off}"
        if time.time() - checked.get(ck, 0) < FLIGHT_RECHECK_HOURS * 3600:
            break    # everything left was checked recently
        checked[ck] = time.time()
        budget["n"] += 1
        done += 1
        depart = (today + datetime.timedelta(days=off)).isoformat()
        ret = (today + datetime.timedelta(days=off + FLIGHT_TRIP_DAYS)).isoformat()
        try:
            r = requests.get("https://serpapi.com/search.json", timeout=90, params={
                "engine": "google_flights", "departure_id": FLIGHT_ORIGIN, "arrival_id": dest,
                "outbound_date": depart, "return_date": ret, "type": 1, "stops": 2,
                "currency": "USD", "hl": "en", "api_key": SERPAPI_KEY})
            data = r.json()
        except Exception as e:
            print(f"flight search error {dest}: {e}")
            continue
        options = [o for o in (data.get("best_flights", []) + data.get("other_flights", []))
                   if o.get("price") and _flight_ok(o)]
        if not options:
            continue
        best = min(options, key=lambda o: o["price"])
        price = best["price"]
        hist = state["history"].setdefault(f"flight:{dest}", [])
        prices = [h[1] for h in hist[-40:]]
        hist.append([int(time.time()), price])
        del hist[:-100]
        rng = (data.get("price_insights") or {}).get("typical_price_range") or []
        ref = (rng[0] + rng[1]) / 2 if len(rng) == 2 else (statistics.median(prices) if len(prices) >= 5 else None)
        if not ref:
            continue
        drop = (1 - price / ref) * 100
        if drop >= FLIGHT_DROP_PCT and not already_seen(f"fl:{dest}:{depart}:{price}"):
            lay = best.get("layovers") or []
            stops = "nonstop" if not lay else f"1 stop ({lay[0].get('name', '?')}, {lay[0].get('duration')} min)"
            link = (data.get("search_metadata") or {}).get("google_flights_url") or (
                f"https://www.google.com/travel/flights?q=Flights%20to%20{dest}%20from%20"
                f"{FLIGHT_ORIGIN}%20on%20{depart}%20through%20{ret}")
            push("flights", "Google Flights", f"{FLIGHT_ORIGIN} to {FLIGHT_DESTINATIONS[dest]} ({dest})",
                 f"${price} round trip, {depart} to {ret}, {stops}. Typical ~${ref:.0f}",
                 link, urgent=drop >= 55, pct=round(drop),
                 image=destination_photo(dest), credit="Photo: Wikipedia")

# ---- Mispricing scan ---------------------------------------------------------
def check_outliers():
    """Search Google Shopping for key products; flag listings far below the median price."""
    if not SERPAPI_KEY:
        return
    today = datetime.date.today().isoformat()
    budget = state.setdefault("outlier_budget", {"day": "", "n": 0})
    if budget["day"] != today:
        budget.update({"day": today, "n": 0})
    checked = state.setdefault("outlier_checked", {})
    for q in sorted(OUTLIER_QUERIES, key=lambda x: checked.get(x, 0)):
        if budget["n"] >= OUTLIER_DAILY_BUDGET or time.time() - checked.get(q, 0) < OUTLIER_RECHECK_HOURS * 3600:
            break
        checked[q] = time.time()
        budget["n"] += 1
        try:
            data = requests.get("https://serpapi.com/search.json", timeout=60, params={
                "engine": "google_shopping", "q": q, "gl": "us", "hl": "en",
                "api_key": SERPAPI_KEY}).json()
        except Exception as e:
            print("outlier scan error:", e)
            continue
        items = [{"store": r.get("source"), "price": float(r["extracted_price"]),
                  "title": r.get("title", q), "url": r.get("link") or r.get("product_link"),
                  "thumb": r.get("thumbnail")}
                 for r in data.get("shopping_results", [])
                 if r.get("extracted_price") and r.get("source") and not r.get("second_hand_condition")]
        if len(items) < 6:
            continue
        med = statistics.median(i["price"] for i in items)
        best = {}
        for i in items:
            if i["store"] not in best or i["price"] < best[i["store"]]["price"]:
                best[i["store"]] = i
        stores = sorted(best.values(), key=lambda x: x["price"])[:COMPARE_MAX_STORES]
        state.setdefault("compare_cache", {})[q] = [time.time(), {
            "stores": [{k: v[k] for k in ("store", "price", "url")} for v in stores],
            "median": round(med, 2)}]
        toks = [t.lower() for t in q.split()[:2]]
        res = classify(q, "", "electronics")
        cat = res[0] if res else "electronics"
        for i in items:
            drop = (1 - i["price"] / med) * 100
            if (drop >= OUTLIER_PCT and i["price"] >= med * 0.25
                    and all(t in i["title"].lower() for t in toks)
                    and not already_seen(f"out:{i['store']}:{q}:{i['price']}")):
                push(cat, "Price scan", i["title"],
                     f"${i['price']:.2f} at {i['store']} vs median ${med:.0f} ({drop:.0f}% below)",
                     i["url"], urgent=drop >= 55, pct=round(drop), cost=i["price"], query=q,
                     image=i.get("thumb"))

# ---- Email digest ------------------------------------------------------------
def _digest_html(deals, label):
    rows = ""
    for a in deals:
        pct = f"{a['pct']}%" if a.get("pct") else "Check"
        bits = [escape(a.get("detail") or "")]
        if a.get("verified"):
            bits.append(f"Verified: {a['market_savings']}% below other stores")
        cp = a.get("coupon")
        if cp:
            bits.append("Code <b>%s</b>" % escape(cp["code"]) if cp.get("code") else "Clip the on-page coupon")
        if a.get("profit_ebay") is not None and a["profit_ebay"] > 0:
            bits.append(f"Est. eBay profit ${a['profit_ebay']}")
        cmp_ = [c for c in (a.get("compare") or []) if c.get("price")][:3]
        if cmp_:
            bits.append("Elsewhere: " + ", ".join(f"{escape(c['store'])} ${c['price']:.0f}" for c in cmp_))
        link = escape(a.get("url") or "#")
        img_html = (f'<img src="{escape(a["image"])}" width="76" height="76" alt="" '
                    'style="display:block;width:76px;height:76px;object-fit:cover;border-radius:14px;margin-bottom:6px">'
                    if a.get("image") else "")
        rows += (
            '<tr><td style="padding:14px 0;border-bottom:1px solid #dcdad6">'
            '<table role="presentation" width="100%"><tr>'
            f'<td width="76" valign="top">{img_html}<div style="background:#f4d5cc;color:#b8452f;border-radius:14px;'
            f'font:800 22px/1 -apple-system,Segoe UI,Arial,sans-serif;padding:14px 0;text-align:center">{pct}</div></td>'
            f'<td style="padding-left:12px" valign="top"><a href="{link}" style="color:#1c1c1b;font:700 16px/1.3 '
            f'-apple-system,Segoe UI,Arial,sans-serif;text-decoration:none">{escape(a["title"])}</a>'
            f'<div style="color:#8b8a86;font:14px/1.5 -apple-system,Segoe UI,Arial,sans-serif;margin-top:4px">'
            f'{" &middot; ".join(bits)}</div></td></tr></table></td></tr>')
    return (
        '<div style="background:#ecebe8;padding:20px 12px"><table role="presentation" align="center" width="100%" '
        'style="max-width:560px;background:#fff;border-radius:18px;overflow:hidden">'
        '<tr><td style="background:#1c1c1b;padding:22px 20px"><div style="color:#f0a38f;font:800 13px/1 '
        '-apple-system,Segoe UI,Arial,sans-serif;letter-spacing:.04em">DEALWATCH</div>'
        f'<div style="color:#fff;font:800 26px/1.2 -apple-system,Segoe UI,Arial,sans-serif;margin-top:8px">'
        f'{len(deals)} hot deal{"s" if len(deals) != 1 else ""} this {label}</div></td></tr>'
        f'<tr><td style="padding:6px 20px 18px"><table role="presentation" width="100%">{rows}</table></td></tr>'
        '</table></div>')

def send_email(subject, html, text):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, SMTP_USER, EMAIL_TO
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context()) as srv:
        srv.login(SMTP_USER, SMTP_PASS)
        srv.send_message(msg)

def maybe_send_digest(now_local=None):
    """Sends the digest once per slot (7 AM / 6 PM local) with deals found since the last one."""
    if not (SMTP_USER and SMTP_PASS and EMAIL_TO):
        return
    from zoneinfo import ZoneInfo
    now_local = now_local or datetime.datetime.now(ZoneInfo(DIGEST_TZ))
    if now_local.hour not in DIGEST_HOURS:
        return
    key = f"{now_local.date()}-{now_local.hour}"
    sent = state.setdefault("digest_sent", {})
    if key in sent:
        return
    since = state.get("last_digest_ts", time.time() - 12 * 3600)
    now = time.time()
    deals = sorted([a for a in alerts if a["ts"] > since and (a.get("score") or 0) >= DIGEST_MIN_SCORE],
                   key=lambda a: a["score"], reverse=True)[:DIGEST_MAX_DEALS]
    sent[key] = now
    state["last_digest_ts"] = now
    for k in [k for k, v in sent.items() if v < now - 3 * 86400]:
        del sent[k]
    if not deals and not DIGEST_SEND_EMPTY:
        return
    label = "morning" if now_local.hour < 12 else "evening"
    top = deals[0] if deals else None
    subject = f"Dealwatch {label}: {len(deals)} hot deal{'s' if len(deals) != 1 else ''}" + (
        f", top is {top['pct']}% off" if top and top.get("pct") else "")
    text = "\n".join(f"- {a['title']} | {a.get('detail', '')} | {a.get('url') or ''}" for a in deals) or "No new hot deals."
    try:
        send_email(subject, _digest_html(deals, label), text)
        print("digest sent:", subject)
    except Exception as e:
        print("digest error:", e)
        sent.pop(key, None)              # try again on the next run

def write_top():
    """top.json: the hottest recent deals, for the iPhone widget."""
    now = time.time()
    recent = [a for a in alerts if now - a["ts"] < 48 * 3600]
    top = sorted(recent, key=lambda a: decayed_score(a, now), reverse=True)[:10]
    out = [{"title": a["title"], "pct": a.get("pct"), "price": a.get("deal_price"),
            "detail": a.get("detail"), "url": a.get("url"), "category": a.get("category"),
            "urgent": a.get("urgent"), "verified": a.get("verified"), "ts": a["ts"], "image": a.get("image"),
            "code": (a.get("coupon") or {}).get("code")} for a in top]
    with open(TOP_FILE, "w") as f:
        json.dump({"updated": int(now), "deals": out, "stats": dict(STATS)}, f)

# ---- Store alert emails -> deals (Gmail, read-only) --------------------------
# For stores that block scrapers (The RealReal, Perigold, Hogan...): sign up for their sale / price-drop
# emails, and Dealwatch reads those emails and turns the good ones into deals.
GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "")
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN", "")
GMAIL_QUERY = ("from:(therealreal.com OR perigold.com OR hogan.com OR ssense.com OR farfetch.com OR nordstromrack.com OR "
               "anthropologie.com OR westelm.com OR cb2.com OR aritzia.com OR thereformation.com OR sonos.com OR smeg.com OR "
               "ninjakitchen.com OR nespresso.com OR kitchenaid.com OR tuftandpaw.com) newer_than:2d")
GMAIL_MIN_PCT = 30                  # promo emails below this discount are ignored
GMAIL_MAX = 25
GMAIL_SENDER_CATEGORY = {
    "therealreal": "fashion", "ssense": "fashion", "farfetch": "fashion", "nordstromrack": "fashion",
    "perigold": "decor", "westelm": "decor", "cb2": "decor", "anthropologie": "clothing", "aritzia": "clothing",
    "thereformation": "clothing", "hogan": "shoes", "sonos": "electronics", "smeg": "appliances",
    "ninjakitchen": "appliances", "nespresso": "appliances", "kitchenaid": "appliances", "tuftandpaw": "cats",
}
PRICE_DROP_RE = re.compile(r"price drop|dropped|markdown|reduced|sale alert|back in stock|final sale|just reduced", re.I)
_SKIP_LINK_RE = re.compile(r"unsubscribe|preferences|privacy|mailto:|facebook|instagram|twitter|pinterest|youtube|tiktok|"
                           r"view[-_]?in[-_]?browser|app\.link|play\.google|itunes", re.I)

def _gmail_walk(payload, out):
    mt, data = payload.get("mimeType", ""), (payload.get("body") or {}).get("data")
    if data and mt in ("text/html", "text/plain"):
        try:
            out.setdefault(mt, []).append(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "ignore"))
        except Exception:
            pass
    for part in payload.get("parts") or []:
        _gmail_walk(part, out)

def check_gmail_alerts():
    if not (GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN):
        return
    try:
        tok = requests.post("https://oauth2.googleapis.com/token", timeout=20, data={
            "client_id": GMAIL_CLIENT_ID, "client_secret": GMAIL_CLIENT_SECRET,
            "refresh_token": GMAIL_REFRESH_TOKEN, "grant_type": "refresh_token"}).json()["access_token"]
        hdr = {"Authorization": "Bearer " + tok}
        base = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
        ids = requests.get(base, headers=hdr, timeout=30,
                           params={"q": GMAIL_QUERY, "maxResults": GMAIL_MAX}).json().get("messages", [])
    except Exception as e:
        print("gmail error (re-run tools/gmail_auth.py if the token expired):", e)
        return
    for m in ids:
        if already_seen("gm:" + m["id"]):
            continue
        try:
            msg = requests.get(f"{base}/{m['id']}", headers=hdr, params={"format": "full"}, timeout=30).json()
        except Exception as e:
            print("gmail message error:", e)
            continue
        h = {x["name"].lower(): x["value"] for x in (msg.get("payload") or {}).get("headers", [])}
        subject, sender = h.get("subject", ""), h.get("from", "")
        parts = {}
        _gmail_walk(msg.get("payload") or {}, parts)
        html_body = "\n".join(parts.get("text/html", []))
        plain = "\n".join(parts.get("text/plain", []))
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(html_body or plain)))[:4000]
        pm = re.search(r"(?:up to\s*)?(\d{2})\s?%\s?off", subject + " " + text[:700], re.I)
        pct = int(pm.group(1)) if pm else None
        drop = bool(PRICE_DROP_RE.search(subject))
        if (pct is None or pct < GMAIL_MIN_PCT) and not drop:
            continue
        res = classify(subject + " " + text[:200], "", "favorites")
        if res is None:
            continue                                   # men's, dupes, etc.
        dom = (re.search(r"@([\w.-]+)", sender) or [None, ""])[1].lower()
        default_cat = next((c for k, c in GMAIL_SENDER_CATEGORY.items() if k in dom.replace("-", "")), "fashion")
        cat = res[0] if res[0] not in ("favorites", "electronics") else default_cat
        link = next((u for u in re.findall(r"href=[\"'](https?://[^\"']+)", html_body) if not _SKIP_LINK_RE.search(u)), None)
        imgs = []
        for tag in re.finditer(r"<img[^>]+>", html_body, re.I):
            sm = re.search(r"src=[\"']([^\"']+)", tag.group(0), re.I)
            wm = re.search(r"width=[\"']?(\d+)", tag.group(0), re.I)
            if sm and not (wm and int(wm.group(1)) < 120):
                u = good_image(sm.group(1))
                if u and u not in imgs:
                    imgs.append(u)
        name = re.sub(r"<.*?>", "", sender).strip(" \"'") or dom
        push(cat, f"Email: {name}", subject, f"{pct}% off" if pct else "Price drop", link,
             urgent=bool(pct and pct >= 60), pct=pct, images=imgs[:6], text=subject)

# ---- Config file ------------------------------------------------------------
CONFIG_FILE = os.path.join(HERE, "config.json")

def apply_config():
    """config.json overrides the defaults above, so you never need to edit code."""
    global BRANDS_RE
    cfg = load(CONFIG_FILE, {})
    for k, v in cfg.items():
        if k.startswith("_"):
            continue
        if k in globals():
            globals()[k] = v
        else:
            print("unknown config key:", k)
    BRANDS_RE = re.compile("|".join(re.escape(b) for b in MY_BRANDS), re.I) if MY_BRANDS else None
    load_style(force=True)
    build_unit_targets()
    build_beauty_regex()
    build_search_feeds()
    for name, url in EXTRA_FEEDS.items():
        FEEDS[name] = (url, "electronics")
        GENERAL_FEEDS.add(name)

# ---- Run -------------------------------------------------------------------
if __name__ == "__main__":
    apply_config()
    if state.get("schema") != 4:             # one-time clean-up: earlier alerts used the old price reader
        alerts.clear()
        state["seen"] = {}                   # so recent live deals are re-checked with the fixed logic
        state["last_run"] = {}               # and every search group is scanned on the next run
        state["schema"] = 4
    alerts[:] = [a for a in alerts if time.time() - a["ts"] < ALERT_MAX_AGE_HOURS * 3600]
    for fn in (check_feeds, check_flight_feeds, check_flights, check_bestbuy,
               check_bestbuy_apple, check_bestbuy_appliances,
               check_bestbuy_clearance, check_bestbuy_openbox, check_bestbuy_favorites, check_gmail_alerts, check_amazon, check_urls, check_outliers):
        try:
            fn()
        except Exception as e:
            print(f"{fn.__name__} failed: {e}")
    cutoff = time.time() - 30 * 86400
    state["seen"] = {k: v for k, v in state["seen"].items() if v > cutoff}
    state["compare_cache"] = {k: v for k, v in state.get("compare_cache", {}).items() if v[0] > time.time() - 2 * 86400}
    state["ebay_cache"] = {k: v for k, v in state.get("ebay_cache", {}).items() if v[0] > time.time() - 2 * 86400}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)
    with open(ALERTS_FILE, "w") as f:
        json.dump(alerts[:300], f)
    write_top()
    if NO_PHOTO:
        print(f"{len(NO_PHOTO)} alert(s) this run had no photo (app shows a plain tile): " + "; ".join(t[:40] for t in NO_PHOTO[:5]))
    maybe_send_digest()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)
