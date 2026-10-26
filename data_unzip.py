#!/usr/bin/env python3
"""
Swiss Specialty List (SL) -- Pipeline step 3: unzip, inventory, selection
=========================================================================

Input   data/raw/zips/SL_YYYY.zip          (from data_scraping.py)

Step 3a Unzip
        Each yearly ZIP is extracted to data/raw/extracted/YYYY/. ZIPs inside
        ZIPs are unpacked too. Extraction is safe (no paths outside the target
        folder), skips macOS junk (__MACOSX, .DS_Store) and is done only once
        per archive (a marker file records the archive's SHA-256).

Step 3b Inventory  ->  data/meta/file_inventory.csv
        One row per extracted file: year, path, file name, format, size,
        reference date parsed from the name, naming scheme, list type
        (SL / GGSL / changes / other) and flags (invalid date, no date ...).

Step 3c Monthly selection  ->  data/meta/monthly_selection.csv
        For every month one SL release is chosen:
          * only files classified as list "SL" in a data format
          * the release dated the 1st of the month = SL status at month start;
            if there is none, the earliest release in that month (flagged)
          * fixed format priority (FORMAT_PRIORITY below)
        Months without any SL release are listed as "missing".
        Period: 2003-01 to 2026-07 (see feasibility study, risk table).

Usage
-----
    python data_unzip.py                    # all years
    python data_unzip.py --years 2003-2005  # subset
    python data_unzip.py --force            # re-extract everything

The printed summary (formats per year, unrecognised file names, missing
months) is the first check: send unusual results back before step 4.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import re
import shutil
import sys
import zipfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import date
from pathlib import Path, PurePosixPath

log = logging.getLogger("sl_unzip")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
FIRST_YEAR, LAST_YEAR = 2003, 2026
PERIOD_END = (2026, 7)                    # new format from 2026-08 (study)
DATA_FORMATS = {"xls", "xlsx", "xml"}
# Fixed priority when the same release exists in several formats.
# Excel exists for every year (xls until 2017, then xlsx); XML only in parallel.
FORMAT_PRIORITY = ["xlsx", "xls", "xml"]
MAX_NESTING = 3                           # ZIP in ZIP in ZIP ...
JUNK = re.compile(r"(^|/)(__MACOSX|\.DS_Store|Thumbs\.db|~\$)", re.I)

# --- reference date in the file name --------------------------------------
# Order matters: the first pattern that matches wins.
DATE_PATTERNS = [
    # 2008.01.01 / 2008-01-01 / 2008_01_01 / 2008 01 01
    ("Y.M.D", re.compile(r"(?<!\d)(20\d\d)[._\- ](\d{1,2})[._\- ](\d{1,2})(?!\d)")),
    # 20080101
    ("YMD", re.compile(r"(?<!\d)(20\d\d)(\d\d)(\d\d)(?!\d)")),
    # 01.01.2008 / 01-01-2008
    ("D.M.Y", re.compile(r"(?<!\d)(\d{1,2})[._\- ](\d{1,2})[._\- ](20\d\d)(?!\d)")),
    # 01012008
    ("DMY", re.compile(r"(?<!\d)(\d\d)(\d\d)(20\d\d)(?!\d)")),
]

# --- naming schemes (from the feasibility study) ---------------------------
SCHEMES = [
    ("BSV_per", re.compile(r"^BSV[_\- ]?per", re.I)),
    ("Publications", re.compile(r"^Publications?[_\-\s]?", re.I)),
]

# --- which list a file belongs to -------------------------------------------
# Checked in this order; the first match wins.
LIST_RULES = [
    ("GGML", re.compile(r"GGML", re.I)),
    ("GGSL", re.compile(r"GGSL|Geburtsgebrechen|(^|[_\-\s])GG([_\-\s]|$)", re.I)),
    ("changes", re.compile(r"[äa]e?nderung|mutation|changes|modif", re.I)),
    ("deleted", re.compile(r"gestrichen|streichung|delet|radiation", re.I)),
    ("limitation", re.compile(r"limitation|limitatio", re.I)),
    ("generics", re.compile(r"generika|generic|g[ée]n[ée]rique", re.I)),
    ("schema", re.compile(r"\.xsd$|schema", re.I)),
    ("SL", re.compile(r"BSV[_\- ]?per|Publication|Spezialit|SL[_\- ]|^SL|"
                      r"BAG[_\- ]?(xls|xml)|Preparations", re.I)),
]


@dataclass
class InvRow:
    year: int                 # archive year
    rel_path: str             # path inside data/raw/extracted/
    file_name: str
    fmt: str                  # xls / xlsx / xml / pdf / ...
    size_bytes: int
    nested_in: str            # inner ZIP it came from ("" = top level)
    list_type: str            # SL / GGSL / changes / ... / unknown
    naming_scheme: str        # BSV_per / Publications / other
    date_raw: str             # the date text found in the name
    date_pattern: str         # which DATE_PATTERNS entry matched
    ref_date: str             # ISO date, "" if none / invalid
    date_flag: str            # ok / invalid_date / no_date / year_mismatch


# --------------------------------------------------------------------------- #
# Step 3a: unzip
# --------------------------------------------------------------------------- #
def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def decode_name(info: zipfile.ZipInfo) -> str:
    """Old Windows ZIPs store names in cp437/cp850 without the UTF-8 flag,
    which turns umlauts into garbage. Re-decode those names."""
    name = info.filename
    if info.flag_bits & 0x800:            # UTF-8 flag set -> already correct
        return name
    try:
        raw = name.encode("cp437")
        for enc in ("utf-8", "cp850", "cp1252"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                pass
    except UnicodeEncodeError:
        pass
    return name


def safe_extract(zf: zipfile.ZipFile, dest: Path) -> list[Path]:
    """Extract all members, refusing paths that would escape `dest`."""
    out = []
    dest_resolved = dest.resolve()
    for info in zf.infolist():
        name = decode_name(info)
        if info.is_dir() or JUNK.search(name):
            continue
        parts = [p for p in PurePosixPath(name.replace("\\", "/")).parts
                 if p not in ("", ".", "..", "/")]
        if not parts:
            continue
        target = dest.joinpath(*parts)
        if not str(target.resolve()).startswith(str(dest_resolved)):
            log.warning("Skipped unsafe path in archive: %s", name)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        out.append(target)
    return out


def extract_recursive(zip_path: Path, dest: Path, depth: int = 0) -> list[Path]:
    """Extract `zip_path` into `dest`; inner ZIPs go to a folder of the same
    name (without .zip) and the inner .zip file itself is removed."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            files = safe_extract(zf, dest)
    except zipfile.BadZipFile:
        log.error("Not a valid ZIP: %s", zip_path)
        return []
    result = []
    for f in files:
        if f.suffix.lower() == ".zip" and depth < MAX_NESTING:
            inner_dest = f.with_suffix("")
            inner_dest.mkdir(parents=True, exist_ok=True)
            result.extend(extract_recursive(f, inner_dest, depth + 1))
            f.unlink()
        else:
            result.append(f)
    return result


def unzip_year(zip_path: Path, out_root: Path, year: int, force: bool) -> Path:
    dest = out_root / str(year)
    marker = dest / ".extracted_from"
    digest = sha256_of(zip_path)
    if not force and marker.exists() and marker.read_text().strip() == digest:
        log.info("%d: already extracted, skipping", year)
        return dest
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    files = extract_recursive(zip_path, dest)
    marker.write_text(digest)
    log.info("%d: extracted %d files from %s", year, len(files), zip_path.name)
    return dest


# --------------------------------------------------------------------------- #
# Step 3b: inventory
# --------------------------------------------------------------------------- #
def parse_date(name: str, year: int) -> tuple[str, str, str, str]:
    """Return (date_raw, pattern, iso_date, flag) from a file name."""
    stem = Path(name).stem
    for label, rx in DATE_PATTERNS:
        for m in rx.finditer(stem):
            a, b, c = m.groups()
            if label in ("Y.M.D", "YMD"):
                y, mo, d = int(a), int(b), int(c)
            else:
                d, mo, y = int(a), int(b), int(c)
            try:
                dt = date(y, mo, d)
            except ValueError:
                return m.group(0), label, "", "invalid_date"
            flag = "ok" if abs(dt.year - year) <= 1 else "year_mismatch"
            return m.group(0), label, dt.isoformat(), flag
    return "", "", "", "no_date"


def classify_list(rel_path: str) -> str:
    name = Path(rel_path).name
    for label, rx in LIST_RULES:
        if rx.search(name):
            return label
    # fall back to the folder names (e.g. ".../GGSL/file.xls")
    for label, rx in LIST_RULES:
        if rx.search(str(Path(rel_path).parent)):
            return label
    return "unknown"


def naming_scheme(name: str) -> str:
    for label, rx in SCHEMES:
        if rx.search(name):
            return label
    return "other"


def build_inventory(extract_root: Path, years: list[int]) -> list[InvRow]:
    rows = []
    for year in years:
        ydir = extract_root / str(year)
        if not ydir.exists():
            continue
        for f in sorted(ydir.rglob("*")):
            if not f.is_file() or f.name.startswith("."):
                continue
            rel = f.relative_to(extract_root).as_posix()
            inner = f.relative_to(ydir).parent.as_posix()
            fmt = f.suffix.lower().lstrip(".")
            d_raw, d_pat, d_iso, d_flag = parse_date(f.name, year)
            rows.append(InvRow(
                year=year, rel_path=rel, file_name=f.name, fmt=fmt,
                size_bytes=f.stat().st_size,
                nested_in="" if inner == "." else inner,
                list_type=classify_list(f.relative_to(ydir).as_posix()),
                naming_scheme=naming_scheme(f.name),
                date_raw=d_raw, date_pattern=d_pat, ref_date=d_iso,
                date_flag=d_flag,
            ))
    return rows


def write_csv(rows: list, path: Path, fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r) if hasattr(r, "__dataclass_fields__") else r)


# --------------------------------------------------------------------------- #
# Step 3c: one release per month
# --------------------------------------------------------------------------- #
def months_in_range(years: list[int]):
    for y in years:
        for m in range(1, 13):
            if (y, m) > PERIOD_END:
                return
            yield y, m


def select_monthly(inv: list[InvRow], years: list[int]) -> list[dict]:
    cands: dict[tuple[int, int], list[InvRow]] = defaultdict(list)
    for r in inv:
        if (r.list_type == "SL" and r.fmt in DATA_FORMATS and r.ref_date
                and r.date_flag in ("ok", "year_mismatch")):
            d = date.fromisoformat(r.ref_date)
            cands[(d.year, d.month)].append(r)

    out = []
    for y, m in months_in_range(years):
        rel = cands.get((y, m), [])
        row = {"month": f"{y}-{m:02d}", "status": "missing", "ref_date": "",
               "fmt": "", "rel_path": "", "n_candidates": len(rel),
               "n_releases_in_month": len({r.ref_date for r in rel}),
               "formats_available": "", "note": ""}
        if rel:
            firsts = [r for r in rel if r.ref_date.endswith("-01")]
            pool = firsts or [r for r in rel if r.ref_date ==
                              min(x.ref_date for x in rel)]
            pool.sort(key=lambda r: (FORMAT_PRIORITY.index(r.fmt)
                                     if r.fmt in FORMAT_PRIORITY else 99,
                                     -r.size_bytes))
            best = pool[0]
            same_day = sorted({r.fmt for r in pool})
            row.update(status="ok" if firsts else "not_first_of_month",
                       ref_date=best.ref_date, fmt=best.fmt,
                       rel_path=best.rel_path,
                       formats_available="/".join(same_day))
            if len([r for r in pool if r.fmt == best.fmt]) > 1:
                row["note"] = "several files of the chosen format on this date"
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
def print_summary(inv: list[InvRow], sel: list[dict]) -> None:
    print("\n=== Files per year (format counts) ===")
    by_year = defaultdict(Counter)
    for r in inv:
        by_year[r.year][r.fmt] += 1
    for y in sorted(by_year):
        print(f"  {y}: " + ", ".join(f"{k} {v}" for k, v in
                                     sorted(by_year[y].items())))

    print("\n=== List types ===")
    for k, v in Counter(r.list_type for r in inv).most_common():
        print(f"  {k:<12}{v}")

    print("\n=== Naming schemes of SL files ===")
    sch = defaultdict(set)
    for r in inv:
        if r.list_type == "SL":
            sch[r.naming_scheme].add(r.year)
    for k, ys in sch.items():
        print(f"  {k:<14}{min(ys)}-{max(ys)}")

    flags = Counter(r.date_flag for r in inv if r.fmt in DATA_FORMATS)
    print("\n=== Date parsing (data files) ===")
    for k, v in flags.most_common():
        print(f"  {k:<14}{v}")
    bad = [r for r in inv if r.fmt in DATA_FORMATS and r.date_flag != "ok"]
    for r in bad[:15]:
        print(f"    {r.date_flag:<14}{r.rel_path}")
    if len(bad) > 15:
        print(f"    ... {len(bad) - 15} more (see file_inventory.csv)")

    unk = [r for r in inv if r.list_type == "unknown"]
    if unk:
        print(f"\n=== Unrecognised file names ({len(unk)}) ===")
        for r in unk[:15]:
            print(f"    {r.rel_path}")

    st = Counter(s["status"] for s in sel)
    print("\n=== Monthly selection ===")
    print(f"  months: {len(sel)}  " +
          "  ".join(f"{k}: {v}" for k, v in st.most_common()))
    miss = [s["month"] for s in sel if s["status"] == "missing"]
    if miss:
        print(f"  missing: {', '.join(miss)}")
    fmts = Counter(s["fmt"] for s in sel if s["fmt"])
    print("  chosen formats: " + ", ".join(f"{k} {v}" for k, v in
                                           fmts.most_common()))


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
    ap.add_argument("--years", default=f"{FIRST_YEAR}-{LAST_YEAR}")
    ap.add_argument("--data-dir", default="data", type=Path)
    ap.add_argument("--force", action="store_true",
                    help="re-extract even if already done")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    years = parse_years(args.years)
    zip_dir = args.data_dir / "raw" / "zips"
    extract_root = args.data_dir / "raw" / "extracted"
    meta = args.data_dir / "meta"

    # 3a unzip
    for y in years:
        z = zip_dir / f"SL_{y}.zip"
        if not z.exists():
            log.warning("%d: %s not found - run data_scraping.py first", y, z)
            continue
        unzip_year(z, extract_root, y, args.force)

    # 3b inventory
    inv = build_inventory(extract_root, years)
    if not inv:
        log.error("No extracted files found.")
        return 1
    inv_path = meta / "file_inventory.csv"
    write_csv(inv, inv_path, [f.name for f in fields(InvRow)])
    log.info("Inventory: %d files -> %s", len(inv), inv_path)

    # 3c monthly selection
    sel = select_monthly(inv, years)
    sel_path = meta / "monthly_selection.csv"
    write_csv(sel, sel_path, list(sel[0].keys()))
    log.info("Selection: %d months -> %s", len(sel), sel_path)

    print_summary(inv, sel)
    return 0


if __name__ == "__main__":
    sys.exit(main())