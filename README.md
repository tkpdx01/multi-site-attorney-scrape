# Immigration-attorney multi-site scrape — demo

Demo for the Upwork posting ["Multi-Site Data Scrape And Enrichment — Immigration Attorneys"](https://www.upwork.com/jobs/Multi-Site-Data-Scrape-And-Enrichment-Immigration-Attorneys_~022046260294377450737/).

Matches the exact 14-column spec from the job description:

```
firm_name, firm_url, firm_size,
attorney_first_name, attorney_last_name, attorney_email, attorney_phone,
firm_address_street, firm_address_city, firm_address_state, firm_address_zip,
source_website, practice_specialty, status
```

## What the demo proves

| Pipeline step | Implemented |
|---|---|
| Parse directory listing page → rows | ✓ `parse_listing()` |
| Follow each attorney's profile URL → fill missing contact fields | ✓ `enrich_from_profile()` |
| **AILA-style fallback**: if profile has no phone/email, scrape firm URL's contact page | ✓ `enrich_from_firm()` (sets `email_from_firm_page` flag on the row) |
| Drop **in-house counsel** rows | ✓ (listing flag + keyword heuristic) |
| Drop **inactive / not in good standing** | ✓ |
| Phone normalization `(XXX) XXX-XXXX` | ✓ `norm_phone()` |
| Email regex validation | ✓ `EMAIL_RE` |
| Dedup across directories on `(firm_url, last, first)` | ✓ `dedup()` |
| Per-site config (selectors, rate, backend) | ✓ parser is one function per site |
| **Rejects CSV with reasons** so the client can sanity-check what we dropped | ✓ `--keep-rejects` |

## Run the demo (no network)

```bash
python3 -m venv .venv
.venv/bin/pip install beautifulsoup4 requests
.venv/bin/python scrape.py \
  --listing fixtures/listing.html --backend local \
  --source-name "OK State Bar Find A Lawyer (fixture)" \
  --out out/oklahoma_immigration.csv \
  --keep-rejects out/oklahoma_immigration_rejects.csv
```

Output: **12 valid rows, 2 rejects (in-house + inactive), 1 dedup.**

Excerpt from `out/oklahoma_immigration.csv`:

| firm_name | first | last | email | phone | city | status |
|---|---|---|---|---|---|---|
| Rodriguez Immigration Law PLLC | Alicia M. | Rodriguez | arodriguez@rodriguezimmigration.example.com | (918) 555-0101 | Tulsa | Active |
| Patel & Patel LLP | Rajesh | Patel | info@patelpatelllp.example.net | (405) 555-0450 | Norman | Active |
| Chen Immigration Services | Wei | Chen | hello@chenimmigrate.example.com | (405) 555-0561 | Edmond | Active |
| … | | | | | | |

Rejects (sample):

| last | reject_reason |
|---|---|
| Smith (Smith Corporate HR Partners) | `in_house_counsel` |
| Williams | `dropped` (status=Inactive) |

See `logs/run.log` for the full run transcript.

## Swapping backends for real sites

The `Fetcher` abstraction has three implementations. Flip with `--backend`:

| Backend | When to use |
|---|---|
| `local` | This demo / regression tests |
| `requests` | State bars / firm sites without CF / VIEWSTATE. Built-in 1.5 s polite-delay between requests. |
| `cdp`     | AILA, Justia, `ams.okbar.org` — anything with Cloudflare Turnstile or ASP.NET VIEWSTATE. Drives an already-open Chrome/Edge tab through CDP, so the real browser carries the cookies, clears the challenge, and we read the DOM. |

The two named problem-sites in the posting:

- **AILA (`ailalawyer.com`)** — ASP.NET VIEWSTATE + JS form-post → `--backend cdp`.
  Per the job notes, AILA sometimes lacks phone/email on profile; `enrich_from_firm()` already handles that fallback.
- **`ams.okbar.org` / Oklahoma Bar Find-A-Lawyer** — ASP.NET VIEWSTATE + AIOSEO layer.
  Same `cdp` backend; and since Oklahoma has no Practice Area filter, the listing step substring-filters firm_name for "Immigration" (per the posting's instruction).

Per-site parsers are ~30-50 LOC each. Adding a new directory is:

1. New `parse_listing_<site>()` function
2. Register in a dispatch dict `{"okbar": parse_listing_okbar, "aila": ...}`
3. CLI grows a `--site` flag

## Budget fit & delivery shape

- Posting: $100 base + $0.04 per valid complete entry.
- Expected per-site throughput on the `requests` backend: **~1 row/sec** with polite 1.5 s rate.
- CDP backend: **~3-5 s/row** because of full page-render.
- For a ~5-directory / ~2000-row target list → 1-2 full days for initial extraction + enrichment + dedup.
- Deliverable: the 14-col CSV + the rejects CSV + the per-site parser code.

## What's deliberately out of scope here

- **Live email-validity check** (MX lookup, SMTP ping). Worth doing before hand-off; adds ~1-2 s/row. Current code only does format-regex.
- **Phone carrier/type lookup**. Tell me if you want it and I'll wire Twilio Lookup — $0.005/number.
- **Proxy rotation**. None needed at this volume; add it if any site starts 429'ing.
