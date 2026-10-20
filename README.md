# NASDAQ Momentum Scanner

Top 200 NASDAQ common stocks (by market cap, price ≥ $5) filtered by the **Momentum_investing_Entry** system and ranked by Return %.

- **Open the scanner:** the GitHub Pages link of this repo (Settings → Pages).
- **Automatic refresh:** every US trading day at about 3:45 AM IST (GitHub Action "Update scanner data").
- **Refresh now (phone):** GitHub app → this repo → Actions → *Update scanner data* → **Run workflow**. Takes about 5–10 minutes, then reload the page.

## Entry conditions (all editable on the page)
1. EMA 50 > EMA 200 · 2. Today's High > EMA 50 · 3. EMA 50 > EMA 150 · 4. EMA 150 > EMA 200 · 5. RSI 14 > 38 · 6. Today's High > EMA 150 · 7. Today's High > EMA 200 · 8. TTM Net Sales rising · 9. TTM Net Profit rising

Scanner filters: Period/Weight return, Retracement from 52W High/Low/ATH/ATL, EMA filter, Volume/Turnover, Price, Sector, Top-N by market cap.

## Files
| File | Purpose |
|---|---|
| `index.html` | the scanner page |
| `download_data.py` | builds the universe and downloads daily prices + quarterly results (Yahoo Finance) |
| `scanner_data.js` | data the page reads (written by the Action) |
| `fundamentals_cache.json` | quarterly results, refreshed weekly |
| `universe_cache.csv` | last good NASDAQ stock list, used if Nasdaq's site refuses the request |
| `.github/workflows/update.yml` | the daily/manual refresh job |

Run locally: `pip3 install -r requirements.txt && python3 download_data.py`, then open `index.html`.
