# Dealwatch: setup (about 15 minutes, on a computer)

1. **Phone:** install the free **ntfy** app and subscribe to a long random topic name (e.g. `dw-7f3k9x2m-tpa`). Anyone who knows the name can read your alerts, so keep it private.
2. **GitHub:** create a repository (public, so the dashboard can be hosted free) and upload every file in this folder, including the hidden `.github` folder. If the web uploader skips `.github`, click Add file > Create new file, type `.github/workflows/dealwatch.yml`, and paste the file contents.
3. **Secrets:** repo Settings > Secrets and variables > Actions > New repository secret. Add:
   - `NTFY_TOPIC` (required): your topic name
   - `BESTBUY_KEY` (free, developer.bestbuy.com): Best Buy and all-Apple checks
   - `SERPAPI_KEY` (free tier, serpapi.com): Tampa flights on Google Flights
   - `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET` (free, developer.ebay.com): resale profit estimates
   - `KEEPA_KEY` (paid): Amazon price history
   - `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` (optional): store alert emails such as The RealReal (see README)
   Anything you skip is simply turned off.
4. **Run it:** Actions tab > dealwatch > Run workflow. Check the log, then it runs by itself about every 15 minutes.
5. **Dashboard:** Settings > Pages > Deploy from branch `main`, folder `/ (root)`. Open the Pages link on your phone, then Share > Add to Home Screen. It installs as an app called Dealwatch.
6. **Tune it:** edit `config.json` (thresholds, destinations, brands, Amazon ASINs, product URLs) and commit. No code changes needed.

If a feed shows HTTP 403/429 in the log, that site is blocking GitHub's servers. Run `python dealwatch.py` from a home computer or Raspberry Pi on a schedule instead.
