"""
Multi-site immigration-attorney scraper — demo for the Upwork posting
"Multi-Site Data Scrape And Enrichment - Immigration Attorneys".

Pipeline:
  1. Fetch the listing page  (e.g. OK bar search with firm_name~"Immigration")
  2. Extract attorney rows  (pluggable selectors per-site)
  3. For each row where phone/email is missing, follow the firm URL
     and scrape the contact page
  4. Validate phone+email format; drop in-house counsel rows
  5. Deduplicate by (firm_url, last_name, first_name)
  6. Write CSV with the exact 14-column schema the client specified

Two backend fetchers:
  - requests (default)  — fast, works for ~60% of bar directories
  - chrome_cdp          — drives an already-open Chrome/Edge tab via the
                          devtools protocol, bypasses Cloudflare/VIEWSTATE.
                          Used for AILA + okbar (ASP.NET-gated).

Run the demo against the local fixture (no network needed):

    python scrape.py --listing fixtures/listing.html --backend local \\
        --out out.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

LOG = logging.getLogger("scrape")

# -------- schema ---------------------------------------------------------

COLUMNS = [
    "firm_name",
    "firm_url",
    "firm_size",
    "attorney_first_name",
    "attorney_last_name",
    "attorney_email",
    "attorney_phone",
    "firm_address_street",
    "firm_address_city",
    "firm_address_state",
    "firm_address_zip",
    "source_website",
    "practice_specialty",
    "status",
]

@dataclass
class Row:
    firm_name: str = ""
    firm_url: str = ""
    firm_size: str = ""
    attorney_first_name: str = ""
    attorney_last_name: str = ""
    attorney_email: str = ""
    attorney_phone: str = ""
    firm_address_street: str = ""
    firm_address_city: str = ""
    firm_address_state: str = ""
    firm_address_zip: str = ""
    source_website: str = ""
    practice_specialty: str = ""
    status: str = ""
    # internal:
    profile_url: str = field(default="", repr=False)
    flags: list[str] = field(default_factory=list, repr=False)

# -------- fetchers -------------------------------------------------------

class Fetcher:
    def get(self, url_or_path: str) -> str:
        raise NotImplementedError

class LocalFetcher(Fetcher):
    """Resolves file paths and relative links against a base root."""
    def __init__(self, root: Path):
        self.root = root.resolve()

    def get(self, url_or_path: str) -> str:
        # http(s) → map to our fixtures/profiles or fixtures/firms based on host/path
        if url_or_path.startswith(("http://", "https://")):
            u = urlparse(url_or_path)
            if "/profile/" in u.path:
                slug = u.path.rstrip("/").split("/")[-1]
                return (self.root / "profiles" / f"{slug}.html").read_text()
            return (self.root / "firms" / f"{u.netloc}.html").read_text()
        # site-absolute paths like /profile/slug → look under fixtures/profiles
        if url_or_path.startswith("/profile/"):
            slug = url_or_path.rstrip("/").split("/")[-1]
            return (self.root / "profiles" / f"{slug}.html").read_text()
        # plain path
        p = Path(url_or_path) if Path(url_or_path).is_absolute() else (self.root / url_or_path).resolve()
        return p.read_text()

class RequestsFetcher(Fetcher):
    def __init__(self, rate_sleep_sec: float = 1.5):
        import requests
        self.sess = requests.Session()
        self.sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self.rate = rate_sleep_sec

    def get(self, url: str) -> str:
        time.sleep(self.rate)
        r = self.sess.get(url, timeout=30)
        r.raise_for_status()
        return r.text

class CDPFetcher(Fetcher):
    """Drive an already-open Chrome/Edge (scripts/cdp.mjs) — bypasses CF/VIEWSTATE."""
    def __init__(self, target_prefix: str, cdp_bin: str = None):
        import subprocess, shutil
        self.target = target_prefix
        self.bin = cdp_bin or str(Path.home() / ".claude/skills/chrome-cdp/scripts/cdp.mjs")
        if not Path(self.bin).exists():
            raise SystemExit(f"cdp.mjs not found: {self.bin}")
        self._subprocess = subprocess

    def get(self, url: str) -> str:
        self._subprocess.run(["node", self.bin, "nav", self.target, url],
                             capture_output=True, check=True)
        # give the page a breath, then pull document.body.outerHTML
        time.sleep(2.5)
        r = self._subprocess.run(["node", self.bin, "eval", self.target, "document.documentElement.outerHTML"],
                                 capture_output=True, check=True)
        # cdp.mjs prints the JSON-serialized value; strip surrounding quotes + unescape
        raw = r.stdout.decode()
        import json
        try:
            return json.loads(raw)
        except Exception:
            return raw

# -------- validation -----------------------------------------------------

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^\+?1?[\s\-.]?\(?(\d{3})\)?[\s\-.]?(\d{3})[\s\-.]?(\d{4})$")

def norm_phone(s: str) -> Optional[str]:
    if not s: return None
    s = s.strip()
    m = PHONE_RE.match(s)
    if not m: return None
    return f"({m.group(1)}) {m.group(2)}-{m.group(3)}"

def looks_valid_email(s: str) -> bool:
    return bool(s) and bool(EMAIL_RE.match(s.strip()))

IN_HOUSE_KEYWORDS = ("hr partners", "in-house", "corporate counsel", "general counsel, ")

# -------- listing parser --------------------------------------------------

def parse_listing(html: str) -> list[Row]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[Row] = []
    for li in soup.select("li.attorney"):
        firm_a = li.select_one(".firm a")
        profile_a = li.select_one("a.profile")
        # split "Last, First Mid." into first/last
        name_raw = (profile_a.get_text(strip=True) if profile_a else "")
        last = first = ""
        if "," in name_raw:
            last, first = [s.strip() for s in name_raw.split(",", 1)]
        note = li.select_one(".note")
        is_duplicate_marker = note and "DUPLICATE" in note.get_text()
        is_inhouse = note and "In-house" in note.get_text()
        row = Row(
            attorney_first_name=first,
            attorney_last_name=last,
            firm_name=(firm_a.get_text(strip=True) if firm_a else ""),
            firm_url=(firm_a["href"] if firm_a and firm_a.has_attr("href") else ""),
            firm_size=(li.select_one(".firm-size").get_text(strip=True) if li.select_one(".firm-size") else ""),
            firm_address_city=(li.select_one(".city").get_text(strip=True) if li.select_one(".city") else ""),
            firm_address_state=(li.select_one(".state").get_text(strip=True) if li.select_one(".state") else ""),
            firm_address_zip=(li.select_one(".zip").get_text(strip=True) if li.select_one(".zip") else ""),
            practice_specialty=(li.select_one(".practice").get_text(strip=True) if li.select_one(".practice") else ""),
            status=(li.select_one(".status").get_text(strip=True) if li.select_one(".status") else ""),
            profile_url=(profile_a["href"] if profile_a and profile_a.has_attr("href") else ""),
        )
        if is_duplicate_marker: row.flags.append("listing_marked_duplicate")
        if is_inhouse: row.flags.append("in_house_counsel")
        rows.append(row)
    return rows

# -------- profile enrichment ---------------------------------------------

def enrich_from_profile(row: Row, html: str) -> None:
    soup = BeautifulSoup(html, "html.parser")
    phone = soup.select_one(".phone")
    email = soup.select_one(".email")
    addr = soup.select_one("address")
    if phone: row.attorney_phone = norm_phone(phone.get_text(strip=True)) or row.attorney_phone
    if email: row.attorney_email = email.get_text(strip=True)
    if addr:
        # first line before <br> = street
        parts = [t.strip() for t in addr.get_text("\n", strip=True).split("\n") if t.strip()]
        if parts:
            row.firm_address_street = parts[0]

def enrich_from_firm(row: Row, html: str) -> None:
    soup = BeautifulSoup(html, "html.parser")
    if not row.attorney_email:
        mailto = soup.select_one('a[href^="mailto:"]')
        if mailto:
            row.attorney_email = mailto["href"].removeprefix("mailto:").strip()
            row.flags.append("email_from_firm_page")
    if not row.attorney_phone:
        ph = soup.select_one(".phone")
        if ph:
            row.attorney_phone = norm_phone(ph.get_text(strip=True)) or row.attorney_phone
            row.flags.append("phone_from_firm_page")

# -------- pipeline --------------------------------------------------------

def dedup(rows: Iterable[Row]) -> list[Row]:
    seen, out = set(), []
    for r in rows:
        key = (r.firm_url.lower(), r.attorney_last_name.lower(), r.attorney_first_name.lower())
        if key in seen:
            LOG.info("  dedup %s — %s, %s", r.firm_url, r.attorney_last_name, r.attorney_first_name)
            continue
        seen.add(key)
        out.append(r)
    return out

def drop_invalid(rows: Iterable[Row]) -> list[Row]:
    out = []
    for r in rows:
        if r.status and r.status.lower() != "active":
            LOG.info("  drop not-active %s %s (status=%s)", r.attorney_first_name, r.attorney_last_name, r.status)
            continue
        if "in_house_counsel" in r.flags:
            LOG.info("  drop in-house %s %s (%s)", r.attorney_first_name, r.attorney_last_name, r.firm_name)
            continue
        if not looks_valid_email(r.attorney_email):
            LOG.info("  drop no-email %s %s", r.attorney_first_name, r.attorney_last_name)
            continue
        if not r.attorney_phone:
            LOG.info("  drop no-phone %s %s", r.attorney_first_name, r.attorney_last_name)
            continue
        out.append(r)
    return out

def write_csv(rows: Iterable[Row], out_path: Path, source: str) -> int:
    with out_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        n = 0
        for r in rows:
            r.source_website = source
            w.writerow([getattr(r, c) for c in COLUMNS])
            n += 1
    return n

# -------- CLI -------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listing", required=True, help="listing URL or local HTML path")
    ap.add_argument("--backend", choices=["local", "requests", "cdp"], default="local")
    ap.add_argument("--cdp-target", default="", help="cdp target id prefix (for --backend cdp)")
    ap.add_argument("--source-name", default="Fixture OK bar", help="source_website column value")
    ap.add_argument("--out", required=True)
    ap.add_argument("--keep-rejects", help="write dropped rows with reasons to this CSV")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.backend == "local":
        fetcher = LocalFetcher(Path("fixtures"))
        listing_html = Path(args.listing).read_text()
    elif args.backend == "requests":
        fetcher = RequestsFetcher()
        listing_html = fetcher.get(args.listing)
    else:
        if not args.cdp_target:
            raise SystemExit("--cdp-target required for --backend cdp")
        fetcher = CDPFetcher(args.cdp_target)
        listing_html = fetcher.get(args.listing)

    LOG.info("=== listing extract ===")
    rows = parse_listing(listing_html)
    LOG.info("  %d rows in listing", len(rows))

    LOG.info("=== enrich (profile + firm) ===")
    for r in rows:
        if r.profile_url:
            try:
                prof_url = r.profile_url if r.profile_url.startswith(("http","/")) else urljoin(args.listing, r.profile_url)
                enrich_from_profile(r, fetcher.get(prof_url))
            except FileNotFoundError:
                r.flags.append("profile_fetch_failed")
        if (not r.attorney_email or not r.attorney_phone) and r.firm_url:
            try:
                enrich_from_firm(r, fetcher.get(r.firm_url))
            except FileNotFoundError:
                r.flags.append("firm_fetch_failed")

    LOG.info("=== dedup ===")
    rows = dedup(rows)

    LOG.info("=== drop invalid (status / in-house / missing email|phone) ===")
    kept = drop_invalid(rows)

    out_path = Path(args.out)
    n = write_csv(kept, out_path, args.source_name)
    LOG.info("wrote %d rows -> %s", n, out_path)

    if args.keep_rejects:
        kept_set = {id(r) for r in kept}
        rej = [r for r in rows if id(r) not in kept_set]
        rej_path = Path(args.keep_rejects)
        with rej_path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(COLUMNS + ["reject_reason"])
            for r in rej:
                reason = "; ".join(r.flags) if r.flags else "dropped"
                w.writerow([getattr(r, c) for c in COLUMNS] + [reason])
        LOG.info("wrote %d rejects -> %s", len(rej), rej_path)

if __name__ == "__main__":
    main()
