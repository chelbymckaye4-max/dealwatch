# Dealwatch

Alerts you (push via ntfy) on mispriced items and extreme discounts: electronics, all Apple products, Amazon, luxury fashion.

## Free hosting on GitHub (runs every ~15 min)
1. Install the ntfy app, subscribe to a long random topic.
2. Create a GitHub repo, upload everything in this folder (keep `.github/workflows`).
3. Repo > Settings > Secrets and variables > Actions > add `NTFY_TOPIC` (required), `BESTBUY_KEY`, `KEEPA_KEY` (optional).
4. Actions tab > dealwatch > Run workflow to test.
5. Dashboard: make the repo public, Settings > Pages > deploy from main branch root. Open the Pages URL on your phone.

Note: Reddit sometimes blocks GitHub's servers. If feeds show HTTP 403/429 in the Actions log, run `python dealwatch.py` from a home machine/Raspberry Pi via cron instead.

## Run locally
    pip install -r requirements.txt
    export NTFY_TOPIC=your-topic
    python dealwatch.py
    python -m http.server   # dashboard at localhost:8000

## Config
Edit the tunables, `AMAZON_WATCHLIST` (ASIN -> label) and `URL_WATCHLIST` (product URL -> label) at the top of dealwatch.py.

## Resale profit estimates (eBay)
Optional. Create a free developer account at developer.ebay.com, make a Production keyset, and set `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET` (as env vars locally or Actions secrets).
eBay may ask you to confirm how you handle account-deletion notifications when creating production keys; their form has an exemption option for personal use.
Each alert then shows estimated profit for eBay (after ~13% fees, assumed $12 shipping, 15% haircut off asking prices) and for Facebook Marketplace (local, 20% haircut, no fees).
Estimates use current eBay asking prices of similar new items, not sold prices, so treat them as a rough screen. Tune the assumptions at the top of the resale section in dealwatch.py. Alerts with est. eBay profit >= $150 are marked urgent.

## Home products
Slickdeals searches for toilet paper, paper towels, laundry, cleaning supplies, bedding and more run every 2 hours. Alerts fire at 35%+ off, or when the per-roll / per-load price beats your target (`HOME_UNIT_TARGETS`, my starting guesses, so tune them). For exact products on Amazon, add ASINs to `AMAZON_WATCHLIST`.

## Flights from Tampa (TPA)
- Free: r/FlightDeals, Secret Flying and The Flight Deal are scanned for Tampa-origin international deals and mistake fares (layovers are not verified there).
- With a SerpApi key (`SERPAPI_KEY`, free tier = 100 searches/month): the script checks round trips on Google Flights for every city in `FLIGHT_DESTINATIONS`, keeps only nonstop or 1 stop with a layover under 2 hours, and alerts when the price is 40%+ below the route's typical price. It rotates through routes and departure dates within `FLIGHT_DAILY_BUDGET` searches/day.
- The layover rule is applied to the outbound flight only.

## Your categories and rules (all adjustable in config.json)
- Apple: AirPods Max and AirPort gear alert at 20%+ off (other Apple at 25%+; Best Buy Apple scan at 20%+).
- Cat furniture: premium brands (Mau Lifestyle, Refined Feline, Tuft + Paw, Catastrophic Creations, Hauspanther, designer/solid wood) at 35%+, generic cat trees at 55%+.
- Handbags: designer brands only (Coach, Kate Spade, Michael Kors, Tory Burch, Chloe, Chanel, etc.), 50%+.
- Jewelry: REAL only. Requires karat gold, sterling/925, platinum, diamonds or genuine gems in the title. Skips plated, vermeil, gold-filled, costume, CZ, moissanite, stainless, brass. 40%+.
- Women's underwear: 40%+. Men's skipped.
- Vacuums: iRobot, Tineco, Dyson, Shark, Roborock, Bissell, Eufy, 40%+ (Best Buy scan plus deal feeds).
- Luxury women's clothing: designer brands, 50%+, only if the post's sizes include S, 4 or 6 (or no sizes are stated).
- Hand soap and candles: in the Home group (35%+, or per-oz / per-candle targets).
- Shoes: women's 8 / 8.5, 50%+.
Search groups are checked every 1 to 2 hours (`THROTTLE_HOURS`). Slickdeals searches may be rate-limited; if the log shows 403/429, lower the number of search terms.

## Price comparison, coupons, and checkout
- **Compare prices:** each alert is looked up on Google Shopping (via SerpApi) and the app shows what other stores list the same product for, cheapest first, the median, and a "Verified" badge when the deal is 30%+ below it. It shows the stores Google returns (usually 5 to 10), not literally every website, and matches can include similar models.
- **SerpApi credits:** comparisons use up to `COMPARE_DAILY_BUDGET` (default 10) searches a day, on top of flights (`FLIGHT_DAILY_BUDGET`, default 3). That is about 390 a month, so use a paid SerpApi plan or lower both numbers. The free tier is 100 a month.
- **Coupons:** promo codes, "extra X% off" and "clip the coupon" notes are read from the deal post text. The app shows the code with a Copy button and the estimated price after. Codes are as posted and may be expired. "Find more codes" opens a search for the cheapest store's promo codes.
- **Checkout:** Apple Pay only works on a store's own checkout, so tapping a deal opens the store. Save your address once in the app's Checkout details panel (stored on your phone only, never in the repo) and use Copy address. For Apple Pay, set Settings > Wallet & Apple Pay > Shipping Address on your iPhone.

## Bulk buying: pet food, litter, household staples
Pet food, litter, toilet paper, paper towels, hand soap, body wash, bar soap, dishwasher pods and laundry soap are judged by unit price (per lb, oz, roll, load or pod), so bulk deals are compared fairly. The script reads sizes from the title ("35 lb", "24 x 3 oz cans", "24 Mega Rolls"). `UNIT_TARGETS` in `config.json` sets the max price per unit that counts as a deal; those numbers are my starting guesses, so tune them to what you usually pay. A price 20% or more under your target is tagged "stock-up price". Percent-off deals alert at 40% (pets) or 35% (home). Sizes the title doesn't state can't be priced per unit.

## Open box, clearance, and your favorite retailers
- **Open box and clearance (Best Buy):** `check_bestbuy_openbox` scans open-box listings for electronics, Apple and vacuums that are 35%+ below regular price (`OPENBOX_PCT`), and `check_bestbuy_clearance` finds new clearance items 50%+ off. Open box uses Best Buy's beta API; I wrote the parsing from its documented fields, so check the first run's log for "open box error". Slickdeals "open box / clearance / floor model" searches add deals from other stores, kept only when they match your interests.
- **Home decor (Decor tab):** CB2 and West Elm deals, Anthropologie home items, and Amazon home and kitchen finds, at 45%+ off (`DECOR_PCT_THRESHOLD`).
- **Women's clothing:** Anthropologie, Aritzia, Express (women's), ASOS, Topshop and Reformation join the designer brands, 50%+ off, only when the post's sizes include S, 4 or 6 (or no sizes are stated).
- There is no public deal feed from these retailers, so deals appear when Slickdeals or Reddit posts them. Roughly 110 feeds are now watched; if the log shows 403/429 from Slickdeals, remove some terms from the `*_SEARCH_TERMS` lists in `config.json`.

## App layout
The app has three tabs: **Hot** (the hottest deal, top picks, and category tiles), **All deals** (everything, filterable by group), and **Checkout** (saved address and Apple Pay tips). Tap a category tile for its page, with filter chips for each subcategory. Groups: Travel; Tech & Apple; Clothing & Style (clothing, shoes, handbags, jewelry, underwear, designer finds); Home & Appliances (appliances, decor); Pets & Household (pet food and litter, cat furniture, household staples); Amazon & Watchlist. To change the grouping, edit the `GROUPS` list near the top of the script in `index.html`.

## Beauty & Care (makeup, perfume, wipes, deodorant)
- **Premium only:** makeup and perfume alert only when the title names a brand in `BEAUTY_BRANDS` (Chanel, Dior, YSL, Tom Ford, Charlotte Tilbury, NARS, Jo Malone, Le Labo, and more; edit the list in `config.json`). Anything with a term in `BEAUTY_EXCLUDE` is skipped: dupes, "inspired by", body sprays and mists, fragrance oils, and drugstore or mass-market lines (Maybelline, L'Oreal, e.l.f., NYX, Revlon, Bath & Body Works, Calvin Klein and others).
- **Thresholds:** makeup 40%+ off, perfume 30%+ off (perfume rarely goes deeper at real retailers), wipes and deodorant 35%+ off or under your unit target (`makeup_wipes_per_ct` $0.12, `deodorant_per_oz` $1.00 in `UNIT_TARGETS`; my guesses, so tune them).
- **Fake warning:** a makeup or perfume deal 60%+ off, or 60%+ below other stores, gets a "Check the seller" tag. Very low prices are where counterfeits and gray-market sellers show up, so buy from Sephora, Ulta, Nordstrom, the brand site, or Amazon when sold by Amazon or the brand.

## Deal photos
Each alert carries a photo of the item. The script takes it from the deal post itself (feed image tags), falls back to the store's own listing image (Best Buy, Amazon via Keepa), then to the product page's preview image (up to `OG_FETCH_BUDGET` lookups per run), then to the Google Shopping thumbnail. The photo shows in the app (hero card, top picks, and every deal row), in the email digest, and in the Home Screen widget (Scriptable). If a photo can't be found or fails to load, the app shows the price sticker instead. Photos are loaded from the original site, so a few sites that block hotlinking may show the sticker. Flights have no photo.

## Photo gallery and destination photos
- Tap any deal photo (hero, top pick, or row) to open a full-screen gallery. Swipe through every photo we found for that deal, tap Open deal to go to the store, and close with the X, a tap outside the photo, or Escape. A "+2" badge on a photo means more photos are available. Extra angles come from the deal post's images and Best Buy's front, angle and back shots; Amazon items (Keepa) include the listing's photo set.
- Flight deals show a photo of the destination from Wikipedia (looked up once per city and saved). Flags, maps and logos are rejected. Edit `FLIGHT_DEST_WIKI` in `config.json` to change which Wikipedia page a code uses. Wikipedia photos are shared under open licenses, and the gallery notes the source.

## App design (minimal storefront style)
The app follows a calm, minimal storefront look: warm neutral grays, soft product tiles with a heart in the corner, small pill badges (salmon for % off, green for Verified), dark rounded buttons, a swipeable "Hottest right now" banner, a Shop by Category row, and a two-column Trending Now grid.
- **Tabs:** Home, Browse (category tiles), Saved, Checkout. The search icon in the header searches titles, brands, stores and categories.
- **Deal page:** tap any card for a full-screen photo gallery (swipe), the price with the other-stores price crossed out, promo code, resale estimate, the price comparison bars, and an Open deal button.
- **Saved:** tap the heart on any deal to keep it. Saved deals live on your phone and stay even after they drop out of the feed.
- **Sort & filter:** the round dark button on list pages sorts by Hottest or Newest and can show only Verified deals or only deals with a promo code.
- Light and dark mode follow your phone. To restyle, edit the color variables at the top of `index.html`.

## Two looks: Boutique and Minimal
Profile (or Checkout) has an Appearance switch. **Boutique** (the default) is ivory with serif headlines, sharp-cornered product tiles, circular category thumbnails, a full-bleed photo banner with a Discover button, hairline detail rows, and a red accent. **Minimal** is the warm-gray look with rounded tiles and dark pill buttons. Both support light and dark mode and follow the same features.

## Your style profile and Pinterest
`style.json` holds brands and keywords you like. Deals that match get a score boost, a tag ("Matches your style"), and a spot in **Picked for you** on the home screen. It starts with the brands you've told me about; add to it by hand (`brands`, `keywords`, and `avoid` for things you don't want).

To build it from Pinterest, run the importer on your own computer (nothing is uploaded anywhere):

**Option A, no Pinterest approval needed (recommended).** On Pinterest go to Settings, Privacy and data, Request your data (choose JSON). When the file arrives:

    python tools/pinterest_import.py --export path/to/pinterest-export.zip --dry-run   # preview
    python tools/pinterest_import.py --export path/to/pinterest-export.zip             # write style.json

**Option B, Pinterest API.** Create an app at developers.pinterest.com and generate an access token with the boards:read and pins:read scopes. Pinterest may require approval before an app can read data, so this can take time.

    export PINTEREST_TOKEN=your-token
    python tools/pinterest_import.py --api

The importer finds brands, repeated style words (for example "boucle" or "minimal"), and the stores you pin from, merges them into `style.json`, and lists possible brands for you to review. I wrote the export reader to handle several file layouts, but I haven't seen a real Pinterest export, so check the preview output first.

## Real product photos
Every photo comes from the real listing: the deal post's own image, the store's product image (Best Buy, Amazon via Keepa), the product page's preview image, or the Google Shopping thumbnail. Logos, icons, avatars, banners, SVGs and "no image" placeholders are rejected. If nothing real is found, the app shows a plain gray tile, never a stand-in picture, and the run log lists those deals so you can see coverage. (The gray "photo" tiles in the sample preview are placeholders because a hosted preview can't load other sites' images.)

## Favorite brands and stores
- **Hogan** (women's shoes): a discount of 35% or more, matched to your size in US or European sizing (US 8 / 8.5 = EU 38.5 / 39 / 39.5; check Hogan's size chart, as fit varies by style).
- **Perigold** (30-40% bar, Decor tab), **Tuft & Paw** (cat furniture), **Sonos** (20%+), **Smeg** (25%+), **Ninja**, **Nespresso**, **KitchenAid** (30%+). Change the numbers in `FAV_BRAND_PCT` in `config.json`.
- Best Buy is scanned for Sonos, Smeg, Ninja, Nespresso and KitchenAid, and Slickdeals searches cover all of them plus Hogan and Perigold.
- **The RealReal** and other stores that block scrapers: use their own emails. Sign up for The RealReal's sale and price-drop alerts (and favorite items there), then turn on **Store alert emails** below. Dealwatch reads those emails and turns the good ones into deals with photos.

## Store alert emails (Gmail, optional)
Reads only emails from the store senders listed in `GMAIL_QUERY` in `dealwatch.py`, with read-only access. It keeps promos of 30%+ off (`GMAIL_MIN_PCT`) or "price drop" style subjects and skips men's items.
1. Follow the instructions at the top of `tools/gmail_auth.py` (create a Google Cloud OAuth client, about 10 minutes; use a personal Google account).
2. Run `python tools/gmail_auth.py`, then add `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET` and `GMAIL_REFRESH_TOKEN` as GitHub Actions secrets.
Note: Google expires the token after 7 days unless you publish the OAuth app ("unverified" is fine for personal use).

## Learning your taste from other accounts
| Source | What works | How |
|---|---|---|
| Pinterest | Boards, pins, links | Data export, or API token (see above) |
| Instagram / Facebook | Advertisers you interacted with, liked pages, follows, saved posts | "Download your information", then `--export file.zip --social` |
| TikTok | Likes, favorites | Data download, then `--social` |
| Gmail | Which stores email you, order receipts | Store alert emails (above) |
| Google activity | Searches, YouTube, Chrome, shopping | Google Takeout, then `--export` (JSON) |
Meta and Google do not offer an API that reads your feed or which ads you engage with, so exports are the practical route. The importer never reads private messages, and it only adds brands and style words to `style.json` for you to review.

## Privacy
GitHub Pages needs a public repo, so `alerts.json` and `style.json` are public. Keep `style.json` to brands and style words (nothing personal), never commit raw exports, and your address stays only on your phone. For full privacy use a private repo and host the app files somewhere private.
