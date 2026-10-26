#!/usr/bin/env python3
"""
Swiss Specialty List (SL) archive scraper -- Pipeline steps 1 & 2
=================================================================

Step 1  Scrape the download URLs
        Selenium opens the FOPH archive page (a JavaScript app), clicks the
        "Archiv" tab, selects each year and clicks its download button. The
        file URL is captured from Chrome's network events and Chrome saves the
        ZIP straight into data/raw/zips. URLs are stored (CSV + JSON).
        Screenshots of each stage go to data/meta/debug for troubleshooting.

Step 2  Download the yearly ZIPs
        For any year not already saved by the browser, requests downloads each ZIP sequentially with polite pauses, once,
        into a local cache. Existing valid ZIPs are skipped. Every download is
        logged with size and SHA-256 for reproducibility.

Fallback (risk table, row 1): if scraping fails for a year, a manual list
(manual_urls.csv with columns year,url) is used instead.

Usage
-----
    pip install selenium beautifulsoup4 requests
    python sl_scraper.py                     # scrape + download 2003-2026
    python sl_scraper.py --years 2003-2005   # subset
    python sl_scraper.py --no-headless       # watch the browser (debugging)
    python sl_scraper.py --skip-scrape       # reuse stored URLs, only download
    python sl_scraper.py --scrape-only       # only collect URLs

Requires Google Chrome; Selenium >= 4.6 fetches a matching chromedriver itself.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import random
import re
import sys
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
ARCHIVE_URL = "https://sl.bag.admin.ch/resources/current-and-archived-data"
FIRST_YEAR, LAST_YEAR = 2003, 2026

USER_AGENT = (
    "CIP-SL-Research/1.0 (university course project; "
    "contact: replace-with-your-email@example.ch)"
)
PAUSE_RANGE = (4.0, 8.0)      # seconds between downloads
CLICK_WAIT = 15               # seconds to wait for a link after a click
PAGE_LOAD_WAIT = 30           # seconds to wait for the year buttons to appear
MAX_RETRIES = 3

log = logging.getLogger("sl_scraper")


@dataclass
class ZipLink:
    year: int
    url: str
    link_text: str
    method: str          # "scraped" | "manual"
    scraped_at: str


# --------------------------------------------------------------------------- #
# Step 1: scrape download URLs with Selenium + BeautifulSoup
# --------------------------------------------------------------------------- #
# The FOPH page (German UI: "Aktuelle Daten | Nächster Monat | Archiv") keeps
# the yearly archive behind the "Archiv" tab. Downloads are <button>s
# ("herunterladen"), not plain links, so the real file URL is captured from
# Chrome's network/download events. Chrome also saves the file directly into
# data/raw/zips, so step 2 finds it cached and does not download it twice.

ARCHIVE_TAB_TEXTS = ["Archiv", "Archive", "Archives", "Archivio"]
DOWNLOAD_WORDS = ["herunterladen", "download", "télécharger", "scarica"]
DOWNLOAD_ALL_WORDS = ["alles herunterladen", "download all", "tout télécharger",
                      "scarica tutto"]
DOWNLOAD_TIMEOUT = 600        # seconds to wait for one browser download


def make_driver(headless: bool, download_dir: Path):
    import os
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    download_dir.mkdir(parents=True, exist_ok=True)
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1400,1000")
    opts.add_argument("--lang=de-CH")
    opts.add_argument(f"--user-agent={USER_AGENT}")
    opts.add_experimental_option("prefs", {
        "download.default_directory": str(download_dir.resolve()),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
    })
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    if os.environ.get("CHROME_BINARY"):                    # optional override
        opts.binary_location = os.environ["CHROME_BINARY"]
        opts.add_argument("--no-sandbox")
    service = Service(os.environ["CHROMEDRIVER"],
                      service_args=["--disable-build-check"]) if os.environ.get(
        "CHROMEDRIVER") else Service()
    driver = webdriver.Chrome(options=opts, service=service)
    try:   # make downloads work in headless mode too
        driver.execute_cdp_cmd("Browser.setDownloadBehavior", {
            "behavior": "allow", "downloadPath": str(download_dir.resolve()),
            "eventsEnabled": True})
    except Exception:
        pass
    return driver


def save_debug(driver, debug_dir: Path, name: str) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / f"{name}.html").write_text(driver.page_source, encoding="utf-8")
    driver.save_screenshot(str(debug_dir / f"{name}.png"))
    log.info("Saved screenshot + HTML: %s/%s.*", debug_dir, name)


def safe_click(driver, el) -> None:
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    time.sleep(0.3)
    try:
        el.click()
    except Exception:
        driver.execute_script("arguments[0].click();", el)


def dismiss_cookie_banner(driver) -> None:
    """Click a cookie/consent button if one is shown. Silent if none."""
    words = ["akzeptieren", "accept", "accepter", "accetta", "einverstanden"]
    for el in driver.find_elements("xpath", "//button"):
        try:
            t = el.text.strip().lower()
            if el.is_displayed() and (t == "ok" or any(w in t for w in words)):
                el.click()
                time.sleep(1)
                return
        except Exception:
            pass


def visible_with_text(driver, text: str, exact: bool = True):
    """Visible elements whose text equals (or contains) `text`, innermost first."""
    lit = f'"{text}"' if "'" in text else f"'{text}'"
    cond = (f"normalize-space(.)={lit}" if exact
            else f"contains(normalize-space(.), {lit})")
    els = driver.find_elements("xpath", f"//body//*[{cond}]")
    out = []
    for e in reversed(els):            # document order reversed = inner first
        try:
            if e.is_displayed():
                out.append(e)
        except Exception:
            pass
    return out


def open_archive_tab(driver) -> bool:
    """Click the Angular Material tab 'Archiv' and wait until it is really
    selected (aria-selected="true") and the year chips are rendered.
    A click that lands too early only focuses the tab, so retry."""
    from selenium.webdriver.support.ui import WebDriverWait
    for attempt in range(1, 4):
        for txt in ARCHIVE_TAB_TEXTS:
            tabs = driver.find_elements(
                "xpath", f"//*[@role='tab'][normalize-space(.)='{txt}']")
            if not tabs:
                continue
            tab = tabs[0]
            if attempt == 1:
                safe_click(driver, tab)
            else:
                driver.execute_script("arguments[0].click();", tab)
            try:
                WebDriverWait(driver, 10).until(
                    lambda d: tab.get_attribute("aria-selected") == "true"
                    and year_card_title(d, None) is not None)
                log.info("Tab '%s' is open", txt)
                time.sleep(1)
                return True
            except Exception:
                log.info("Tab '%s' not active yet (attempt %d), retrying",
                         txt, attempt)
                time.sleep(2)
    return False


SHOW_MORE_TEXTS = ["Mehr anzeigen", "Show more", "Afficher plus",
                   "Mostra di più", "Mostra altro"]


def expand_year_list(driver) -> None:
    """Only the latest ~10 years are shown; click 'Mehr anzeigen' to see all."""
    for txt in SHOW_MORE_TEXTS:
        for el in innermost_with_text(driver, txt):
            try:
                if el.is_displayed():
                    anc = el.find_elements(
                        "xpath", "ancestor-or-self::*[self::button or self::a or "
                                 "@role='button'][1]")
                    safe_click(driver, anc[0] if anc else el)
                    time.sleep(1.5)
                    log.info("Clicked '%s'", txt)
                    return
            except Exception:
                pass


def innermost_with_text(driver, text: str):
    """Elements whose text is exactly `text` and that have no child with the
    same text (= the label itself, not its wrappers), in document order.
    Visibility is NOT required: the unselected year chips on the FOPH page
    use very faint text, which Selenium may report as hidden."""
    lit = f"'{text}'"
    return driver.find_elements(
        "xpath",
        f"//body//*[normalize-space(.)={lit} and not(*[normalize-space(.)={lit}])]")


def year_card_title(driver, year):
    """The heading 'Archiv 2003' of the card shown for the selected year.
    year=None matches any year (used to check the Archiv tab has loaded)."""
    if year is None:
        els = driver.find_elements(
            "xpath", "//body//*[starts-with(normalize-space(.), 'Archiv 20') and "
                     "string-length(normalize-space(.)) = 11]")
        return els[0] if els else None
    for t in (f"Archiv {year}", f"Archive {year}", f"Archives {year}",
              f"Archivio {year}"):
        els = innermost_with_text(driver, t)
        if els:
            return els[0]
    return None


def select_year(driver, year: int) -> bool:
    """Click the year chip in the row 'Archiv der Spezialitätenliste pro Jahr'.

    The page has two chip rows (SL per year, and 'Aktuelle Änderungen' for
    2024-2026). The SL row comes first in the page, so we take the first
    matching chip. Success = the card heading 'Archiv <year>' appears."""
    from selenium.webdriver.support.ui import WebDriverWait
    y = str(year)

    chips = innermost_with_text(driver, y)
    if not chips:
        expand_year_list(driver)
        chips = innermost_with_text(driver, y)
    if not chips:
        # selected chip may carry an icon text, e.g. "check 2026"
        chips = [e for e in driver.find_elements(
            "xpath", f"//body//*[contains(normalize-space(.), '{y}') and "
                     f"not(*[contains(normalize-space(.), '{y}')])]")
            if len(e.text.strip()) <= 12]
    if chips:
        chip = chips[0]
        # click the clickable ancestor (button/chip) if the label is a <span>
        anc = chip.find_elements(
            "xpath", "ancestor-or-self::*[self::button or @role='button' or "
                     "@role='option' or @role='radio' or self::label or "
                     "self::a][1]")
        safe_click(driver, anc[0] if anc else chip)
        try:
            WebDriverWait(driver, 15).until(
                lambda d: year_card_title(d, year) is not None)
            return True
        except Exception:
            log.warning("%d: clicked the chip but 'Archiv %d' did not appear",
                        year, year)
            return False

    # Fallback: native <select> dropdown
    from selenium.webdriver.support.ui import Select
    for sel in driver.find_elements("tag name", "select"):
        try:
            Select(sel).select_by_visible_text(y)
            return True
        except Exception:
            pass
    return False


def is_download_button(el) -> bool:
    try:
        t = (el.text or el.get_attribute("aria-label") or "").lower()
        return el.is_displayed() and any(w in t for w in DOWNLOAD_WORDS)
    except Exception:
        return False


def find_download_button(driver, year: int):
    """The 'ZIP herunterladen' button inside the 'Archiv <year>' card."""
    title = year_card_title(driver, year)
    if title is None:
        return None
    node = title
    for _ in range(10):                            # walk up to the card
        try:
            node = node.find_element("xpath", "..")
        except Exception:
            break
        btns = [b for b in node.find_elements("xpath", ".//button | .//a")
                if is_download_button(b)]
        if btns:
            zip_btns = [b for b in btns if "zip" in b.text.lower()]
            return (zip_btns or btns)[0]
    return None


def flush_perf_log(driver) -> None:
    try:
        driver.get_log("performance")
    except Exception:
        pass


def captured_download_url(driver) -> str | None:
    """Look through Chrome's performance log for the URL of a download."""
    url = None
    try:
        entries = driver.get_log("performance")
    except Exception:
        return None
    for e in entries:
        try:
            msg = json.loads(e["message"])["message"]
        except Exception:
            continue
        method, p = msg.get("method", ""), msg.get("params", {})
        if method in ("Page.downloadWillBegin", "Browser.downloadWillBegin"):
            u = p.get("url", "")
            if u.startswith("http"):
                return u
            continue      # blob: URL -> use the network request seen before
        if method == "Network.responseReceived":
            r = p.get("response", {})
            mime = r.get("mimeType", "")
            disp = str({k.lower(): v for k, v in r.get("headers", {}).items()}
                       .get("content-disposition", ""))
            if ("zip" in mime or "octet-stream" in mime or "attachment" in disp
                    or re.search(r"\.zip(\?|$)", r.get("url", ""), re.I)):
                url = r.get("url")
    return url


def wait_for_new_file(download_dir: Path, before: set[str],
                      timeout: int) -> Path | None:
    end = time.time() + timeout
    while time.time() < end:
        new = [p for p in download_dir.iterdir() if p.name not in before
               and not p.name.endswith((".crdownload", ".tmp", ".part"))
               and not p.name.startswith(".")]
        pending = [p for p in download_dir.iterdir()
                   if p.name.endswith(".crdownload")]
        if new and not pending:
            return max(new, key=lambda p: p.stat().st_mtime)
        time.sleep(1)
    return None


def scrape_zip_urls(years: list[int], headless: bool, debug_dir: Path,
                    download_dir: Path) -> dict[int, ZipLink]:
    from selenium.webdriver.support.ui import WebDriverWait

    results: dict[int, ZipLink] = {}
    driver = make_driver(headless, download_dir)
    try:
        log.info("Opening %s", ARCHIVE_URL)
        driver.get(ARCHIVE_URL)
        try:   # wait until the JS app has rendered its tabs
            WebDriverWait(driver, PAGE_LOAD_WAIT).until(
                lambda d: any(visible_with_text(d, t) for t in ARCHIVE_TAB_TEXTS))
        except Exception:
            save_debug(driver, debug_dir, "page_initial")
            log.error("The page did not load (no 'Archiv' tab).")
            return results
        dismiss_cookie_banner(driver)

        if not open_archive_tab(driver):
            save_debug(driver, debug_dir, "page_initial")
            log.error("Could not click the 'Archiv' tab.")
            return results
        expand_year_list(driver)
        save_debug(driver, debug_dir, "page_archive")   # for troubleshooting

        seen_hashes: dict[str, str] = {
            sha256_of(p): p.name for p in download_dir.glob("SL_*.zip")}
        for year in years:
            target = download_dir / f"SL_{year}.zip"
            if not select_year(driver, year):
                log.warning("%d: year not found on the Archiv tab", year)
                continue
            time.sleep(2)
            btn = find_download_button(driver, year)
            if btn is None:
                save_debug(driver, debug_dir, f"page_{year}")
                log.warning("%d: no download button found", year)
                continue

            href = btn.get_attribute("href") or ""
            if href.startswith("http") and not target.exists():
                # Plain link: just record it; step 2 downloads with requests.
                url = href
            else:
                if target.exists() and zipfile.is_zipfile(target):
                    log.info("%d: already downloaded, skipping click", year)
                    url = ""
                else:
                    flush_perf_log(driver)
                    before = {p.name for p in download_dir.iterdir()}
                    safe_click(driver, btn)
                    log.info("%d: download started in browser ...", year)
                    f = wait_for_new_file(download_dir, before, DOWNLOAD_TIMEOUT)
                    url = captured_download_url(driver) or ""
                    if f is None:
                        save_debug(driver, debug_dir, f"page_{year}")
                        log.warning("%d: no file arrived within %ds", year,
                                    DOWNLOAD_TIMEOUT)
                        if not url:
                            continue
                    else:
                        if f.suffix.lower() == ".zip":
                            f.replace(target)
                            log.info("%d: saved %s (%.1f MB)", year, target.name,
                                     target.stat().st_size / 1e6)
                            digest = sha256_of(target)
                            if digest in seen_hashes:
                                log.error("%d: identical to %s - wrong file? "
                                          "Removed it.", year,
                                          seen_hashes[digest])
                                target.unlink()
                                save_debug(driver, debug_dir, f"page_{year}")
                                continue
                            seen_hashes[digest] = target.name
                        else:
                            log.warning("%d: downloaded %s (not a ZIP) - kept "
                                        "as is", year, f.name)
            if url.startswith("blob:"):
                url = ""          # in-page blob, not reusable
            results[year] = ZipLink(
                year=year, url=url or f"browser-download:{target.name}",
                link_text=btn.text.strip(), method="scraped",
                scraped_at=datetime.now(timezone.utc).isoformat(
                    timespec="seconds"),
            )
            log.info("%d: url = %s", year, results[year].url)
            time.sleep(random.uniform(*PAUSE_RANGE))     # be polite
    finally:
        driver.quit()
    return results


# --------------------------------------------------------------------------- #
# URL storage and manual fallback
# --------------------------------------------------------------------------- #
FIELDS = ["year", "url", "link_text", "method", "scraped_at"]


def save_links(links: dict[int, ZipLink], csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [asdict(links[y]) for y in sorted(links)]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    csv_path.with_suffix(".json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Saved %d URLs to %s", len(rows), csv_path)


def load_links(csv_path: Path, method: str | None = None) -> dict[int, ZipLink]:
    if not csv_path.exists():
        return {}
    out = {}
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("year") or not row.get("url"):
                continue
            y = int(row["year"])
            out[y] = ZipLink(
                year=y, url=row["url"].strip(),
                link_text=row.get("link_text", ""),
                method=method or row.get("method", "manual"),
                scraped_at=row.get("scraped_at", ""),
            )
    return out


# --------------------------------------------------------------------------- #
# Step 2: download ZIPs (sequential, paused, cached, logged)
# --------------------------------------------------------------------------- #
def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_one(session: requests.Session, link: ZipLink,
                 dest: Path) -> tuple[str, int]:
    """Download `link` to `dest`. Returns (status, bytes)."""
    if dest.exists() and zipfile.is_zipfile(dest):
        return "cached", dest.stat().st_size

    if link.url.startswith("browser-download:"):
        log.warning("%d: browser download missing and no direct URL known",
                    link.year)
        return "failed", 0

    tmp = dest.with_suffix(".zip.part")
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with session.get(link.url, stream=True, timeout=(15, 300)) as r:
                r.raise_for_status()
                with tmp.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        f.write(chunk)
            if not zipfile.is_zipfile(tmp):
                raise ValueError("response is not a valid ZIP file")
            tmp.replace(dest)
            return "downloaded", dest.stat().st_size
        except Exception as exc:
            log.warning("%d: attempt %d/%d failed: %s",
                        link.year, attempt, MAX_RETRIES, exc)
            tmp.unlink(missing_ok=True)
            if attempt < MAX_RETRIES:
                time.sleep(10 * attempt)
    return "failed", 0


def download_all(links: dict[int, ZipLink], zip_dir: Path,
                 log_path: Path) -> None:
    zip_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    rows = []
    years = sorted(links)
    for i, year in enumerate(years):
        link = links[year]
        dest = zip_dir / f"SL_{year}.zip"
        status, size = download_one(session, link, dest)
        digest = sha256_of(dest) if status != "failed" else ""
        log.info("%d: %-10s %8.1f MB  %s", year, status, size / 1e6, dest.name)
        rows.append({
            "year": year, "url": link.url, "source": link.method,
            "file": dest.name if status != "failed" else "",
            "status": status, "bytes": size, "sha256": digest,
            "logged_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
        })
        if status == "downloaded" and i < len(years) - 1:
            time.sleep(random.uniform(*PAUSE_RANGE))

    with log_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["year"])
        w.writeheader()
        w.writerows(rows)
    failed = [r["year"] for r in rows if r["status"] == "failed"]
    log.info("Download log: %s", log_path)
    if failed:
        log.error("Failed years: %s - add them to manual_urls.csv", failed)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_years(spec: str) -> list[int]:
    years: set[int] = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = (int(x) for x in part.split("-"))
            years.update(range(a, b + 1))
        else:
            years.add(int(part))
    return sorted(years)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--years", default=f"{FIRST_YEAR}-{LAST_YEAR}",
                    help="e.g. 2003-2026 or 2003,2010,2020")
    ap.add_argument("--data-dir", default="data", type=Path)
    ap.add_argument("--manual-urls", default="manual_urls.csv", type=Path,
                    help="fallback CSV with columns year,url")
    ap.add_argument("--no-headless", action="store_true")
    ap.add_argument("--skip-scrape", action="store_true",
                    help="reuse stored URLs instead of running Selenium")
    ap.add_argument("--scrape-only", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    years = parse_years(args.years)
    meta = args.data_dir / "meta"
    urls_csv = meta / "sl_zip_urls.csv"

    # Step 1 -- URLs
    links = load_links(urls_csv)
    if not args.skip_scrape:
        try:
            scraped = scrape_zip_urls(years, headless=not args.no_headless,
                                      debug_dir=meta / "debug",
                                      download_dir=args.data_dir / "raw" / "zips")
            for y, l in scraped.items():   # keep a known real URL
                if l.url.startswith("browser-download:") and \
                        links.get(y, l).url.startswith("http"):
                    continue
                links[y] = l
        except Exception as exc:
            log.error("Scraping failed (%s); falling back to stored/manual URLs",
                      exc)

    manual = load_links(args.manual_urls, method="manual")
    for y in years:
        if y not in links and y in manual:
            links[y] = manual[y]
            log.info("%d: using manual URL", y)

    if links:
        save_links(links, urls_csv)

    missing = [y for y in years if y not in links]
    if missing:
        log.warning("No URL for: %s", missing)

    if args.scrape_only:
        return 0

    # Step 2 -- downloads
    selected = {y: links[y] for y in years if y in links}
    if not selected:
        log.error("Nothing to download.")
        return 1
    download_all(selected, args.data_dir / "raw" / "zips",
                 meta / "download_log.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())