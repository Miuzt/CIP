import marimo

__generated_with = "0.25.1"
app = marimo.App(width="full")


@app.cell
def _():
    import marimo as mo

    return (mo,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    # Import of the Swiss Specialty List (SL)

    This notebook is the **second step** of the data pipeline:

    1. **Download** (done, separate notebook): the URLs of all yearly archive files
       were collected via web scraping and the files downloaded into a local folder.
    2. **Import** (this notebook): the downloaded files are read, the column names
       are harmonised across all years and the result is stored as a single panel
       (`sl_panel.parquet`).

    - **Source:** Specialty List (SL) of the Federal Office of Public Health (FOPH),
      one Excel file per publication
    - **Period:** 2003-01 to 2026-07 (from 2026-08 the FOPH uses a new format, excluded)
    - **Reference dates:** first day of each month only, one snapshot per month
    - **Output:** `data/sl_panel.parquet` and `data/import_log.csv`

    The raw files are stored outside the project and are only needed for this
    import. All subsequent analyses start from the Parquet file.
    """)
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 1. Setup

    Path to the raw data (hard-coded, located outside the project because of its
    size) and the output directory within the project. The output path is resolved
    relative to this notebook and therefore works on every machine.

    **Environment:** Environment: Python 3.14, packages managed with `uv` and listed in
    `pyproject.toml` (`marimo`, `pandas`, `pyarrow`, `python-calamine`, `xlrd`,
    `openpyxl`). After changing packages, the notebook must be fully restarted;
    otherwise saving to Parquet fails with an `ArrowKeyError` because pandas is
    loaded twice.
    """)
    return


@app.cell
def _(mo):
    import re
    import pandas as pd
    from pathlib import Path


    # raw files are outside the project (too large), only needed for this import
    RAW_PATH = Path("/Users/michaelengel/Documents/HSLU Master/Semester 2/DS in Healthcare/ds_healthcare_project/data/raw")

    # output goes into the project, relative to this notebook
    OUT_DIR = mo.notebook_dir() / "data"
    OUT_DIR.mkdir(exist_ok=True)
    return OUT_DIR, RAW_PATH, pd, re


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 2. File inventory

    The download folder contains files from two naming eras:

    | Pattern | Source | Date format |
    |---|---|---|
    | `BSV_per_YYYY.MM.DD.xls(x)` | BSV (older files) | `%Y.%m.%d` |
    | `Publication(s)-YYYYMMDD.xls(x)` | Publications (newer files) | `%Y%m%d` |

    The reference date is parsed from the file name. Files that match neither pattern (e.g. supplementary lists, Excel lock files `~$…`) are ignored. Files with an invalid date in their name (e.g. 31 September 2017) are skipped and listed in the output.

    **Manual correction:** the file for 2008-12-01 was downloaded with a lower-case name (`publications…`). It was renamed manually to match the pattern above; no other files were modified.

    Only files dated on the **first of the month** up to **2026-07-01** are kept,
    yielding one snapshot per month. Two checks follow:
    - **Duplicates:** two files for the same month → error, must be resolved manually.
    - **Gaps:** months without a file are listed (warning only).
    """)
    return


@app.cell
def _(pd, re):
    END_DATE = pd.Timestamp("2026-07-01")

    # file name patterns: (regex, source, date format)
    patterns = [
        (r"^BSV_per_(\d{4}\.\d{2}\.\d{2})\.xlsx?$", "BSV", "%Y.%m.%d"),
        (r"^Publications?[-_](\d{8})\.xlsx?$", "Publications", "%Y%m%d"),
    ]


    def parse_name(name):
        for pattern, source, fmt in patterns:
            m = re.match(pattern, name)
            if m:
                ref_date = pd.to_datetime(m.group(1), format=fmt, errors="coerce")
                if pd.isna(ref_date):
                    print("Invalid date in file name, skipped:", name)
                    return None, None
                return source, ref_date
        return None, None


    def list_files(raw_path, end_date):
        rows = []
        for f in raw_path.rglob("*.xls*"):
            source, ref_date = parse_name(f.name)
            if source:
                rows.append({"file": f, "source": source, "ref_date": ref_date})

        df = pd.DataFrame(rows)

        # keep only first of month and dates up to end_date
        df = df[(df["ref_date"].dt.day == 1) & (df["ref_date"] <= end_date)]
        df = df.sort_values("ref_date").reset_index(drop=True)

        # stop if there are two files for the same month
        if df["ref_date"].duplicated().any():
            raise ValueError("Duplicate reference dates found")

        return df

    return END_DATE, list_files


@app.cell
def _(END_DATE, RAW_PATH, list_files, pd):
    inventory = list_files(RAW_PATH, END_DATE)

    # which months are missing?
    _months = pd.date_range(inventory["ref_date"].min(), END_DATE, freq="MS")
    missing_months = _months.difference(inventory["ref_date"])

    print(len(inventory), "files")
    print(len(missing_months), "missing months:", list(missing_months.strftime("%Y-%m")))
    inventory
    return (inventory,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    **Result:** 282 monthly files from 2003-01 to 2026-07. No file exists for
    2003-03; this month is missing in the panel. One file with an invalid date
    in its name (`Publications_20170931.xls`) is skipped; it is not a
    first-of-month file and would be excluded anyway.
    """)
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 3. Column harmonisation

    The column structure of the SL changed over the years: columns were renamed,
    added or dropped, headers contain line breaks and double spaces, and the price
    columns carry the price date in their name (e.g. `Exf-Preis per 01.01.2023`).

    As a first step, the header row of every file is read and all distinct column
    names are listed with the period in which they occur. Whitespace is normalised
    before comparison. This overview is the basis for the mapping to uniform
    target names.
    """)
    return


@app.cell
def _(inventory, pd):
    _rows = []
    for _, _r in inventory.iterrows():
        # only read the header row
        _cols = pd.read_excel(_r["file"], sheet_name=0, nrows=0).columns
        for _c in _cols:
            # remove line breaks and double spaces in header names
            _rows.append({"ref_date": _r["ref_date"], "column": " ".join(str(_c).split())})

    headers = pd.DataFrame(_rows)

    header_overview = (
        headers.groupby("column")["ref_date"]
        .agg(first="min", last="max", n_files="count")
        .sort_values("first")
        .reset_index()
    )
    header_overview
    return (headers,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    **Result:** the raw header rows contain **591** distinct column names, mainly
    because of the price date in the price column names. After removing the date
    (`per DD.MM.YYYY`), **33** distinct names remain. These are mapped below.
    """)
    return


@app.cell
def _(headers):
    # remove "per DD.MM.YYYY" from price column names
    headers_norm = headers.assign(
        column=headers["column"].str.replace(r"\s*per\s+\d{2}\.\d{2}\.\d{4}$", "", regex=True)
    )

    header_overview_norm = (
        headers_norm.groupby("column")["ref_date"]
        .agg(first="min", last="max", n_files="count")
        .sort_values("first")
        .reset_index()
    )
    header_overview_norm
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ### Mapping to the target schema

    All column names are mapped to English target names. Columns that were renamed
    over time are merged into one target column:

    | Target | Original names (period) |
    |---|---|
    | `dossier` | `BSV-Dossier` (2003) → `BAG-Dossier` (from 2004-01) |
    | `swissmedic_nr` | `IKS-Nr` → `Swissmedic-Nr` (from 2004-02) → `Swissmedic-Nr.` (from 2008-12) |
    | `swissmedic_list` | `IKS` → `Swissmedic-Liste` (from 2004-02) |
    | `parallel_import_nr` | `Swissmedic-Nr Parallelimporten` (2022) → `SwissmedicNo8 ParallelImp` (from 2023-07) |
    | `exf_price`, `pub_price` | `Exf-Preis per <date>`, `Pub-Preis per <date>` (date removed) |

    Two renamings reflect changes of the responsible authority:

    - `BSV-Dossier` → `BAG-Dossier`: responsibility for health and accident
      insurance, and thus for the SL, moved from the Federal Social Insurance
      Office (BSV) to the FOPH on 1 January 2004 [1, 2].
    - `IKS` → `Swissmedic`: the Swiss Agency for Therapeutic Products (Swissmedic)
      replaced the Intercantonal Office for the Control of Medicines (IKS) on
      1 January 2002 [3, 4]; the SL kept the old label until 2004-01.

    The numbering is assumed to be continuous across these renamings.

    The co-payment column contains the rate in its name (`20% Selbstbehalt` from
    2008-12, `40% Selbstbehalt` from 2024-01). It is stored as `deductible`, the
    rate as a separate column `deductible_rate`.

    Columns that do not exist in a file are filled with missing values. Columns
    that are not part of the mapping are dropped and listed in the import log.

    **References**
    1. FOPH: *RKUV – Kranken- und Unfallversicherung. Rechtsprechung und Verwaltungspraxis bis 2006.* https://www.bag.admin.ch/de/rkuv-kranken-und-unfallversicherung-rechtsprechung-und-verwaltungspraxis-bis-2006
    2. FINMA archive (BPV): overview of supervised insurers 2004. https://www.finma.ch/finmaarchiv/bpv/d/dokumentation/00439/00775/00780/00791/index.html?lang=de
    3. Swissmedic: *Organisation.* https://www.swissmedic.ch/swissmedic/de/home/ueber-uns/organisation.html
    4. Swiss Federal Archives: *Bewertungsentscheid Swissmedic* (2019).
    """)
    return


@app.cell
def _(re):
    COLUMN_MAP = {
        # identifiers
        "Pharmacode": "pharmacode",
        "BSV-Dossier": "dossier",
        "BAG-Dossier": "dossier",
        "IKS-Nr": "swissmedic_nr",
        "Swissmedic-Nr": "swissmedic_nr",
        "Swissmedic-Nr.": "swissmedic_nr",
        "IKS": "swissmedic_list",
        "Swissmedic-Liste": "swissmedic_list",
        "Rec-ID": "rec_id",
        "GTIN": "gtin",
        # product
        "Hersteller": "manufacturer",
        "Bezeichnung": "name",
        "Substanzen": "substances",
        "Generika": "generic",
        "Therap. Gruppe": "therap_group",
        "ATC": "atc",
        "Einf.-Datum": "listing_date",
        "Einf-Datum": "listing_date",
        "Gammennummer": "range_nr",
        "Gammenbezeichnung": "range_name",
        "GGSL": "ggsl",
        "RefBiosimCode": "ref_biosim_code",
        "Swissmedic-Nr Parallelimporten": "parallel_import_nr",
        "SwissmedicNo8 ParallelImp": "parallel_import_nr",
        # prices
        "Exf-Preis": "exf_price",
        "Pub-Preis": "pub_price",
        "Preismodell": "price_model",
        "Preismodell Indikationen": "price_model_indications",
        "letzte Preis-änderung": "last_price_change",
        # limitations
        "Limitation": "limitation",
        "Lim-Pkte": "lim_points",
        "Indikationscode": "indication_code",
    }

    TARGET_COLUMNS = list(dict.fromkeys(COLUMN_MAP.values())) + ["deductible", "deductible_rate"]


    def harmonise(df):
        # same cleaning as in the header overview
        cols = [" ".join(str(c).split()) for c in df.columns]
        cols = [re.sub(r"\s*per\s+\d{2}\.\d{2}\.\d{4}$", "", c) for c in cols]
        df = df.set_axis(cols, axis=1)

        # co-payment: rate is part of the column name
        rate = None
        for c in cols:
            m = re.match(r"^(\d+)% Selbstbehalt$", c)
            if m:
                rate = int(m.group(1))
                df = df.rename(columns={c: "deductible"})

        ignored = [c for c in df.columns if c not in COLUMN_MAP and c != "deductible"]

        df = df.rename(columns=COLUMN_MAP)
        df["deductible_rate"] = rate

        # missing columns -> NaN, unknown columns are dropped
        return df.reindex(columns=TARGET_COLUMNS), ignored

    return (harmonise,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 4. Import

    All files in the inventory are read, harmonised and stacked into one long
    panel: **one row per package and reference date**. Each row carries the
    reference date (`ref_date`) and the name of the source file (`source_file`).

    - The archive mixes formats: `.xls` until 2017 (with one `.xlsx` in 2010-10),
      `.xlsx` from 2018. Both are read with the `calamine` engine, which handles
      both formats and is considerably faster than the default engines
      (`xlrd`/`openpyxl`); reading all headers with the default engines took
      several minutes.
    - All values are read as text, so that identifiers keep their original form
      (e.g. no loss of leading zeros).
    - Empty rows (e.g. footer lines at the end of a sheet) are dropped.

    The import log records the number of rows and any ignored columns per file.
    """)
    return


@app.cell
def _(harmonise, inventory, pd):
    _parts = []
    _log = []

    for _i, _r in inventory.iterrows():
        print(_i + 1, "/", len(inventory), _r["file"].name)

        _raw = pd.read_excel(_r["file"], sheet_name=0, dtype=str, engine="calamine")
        _df, _ignored = harmonise(_raw)

        # drop empty rows (e.g. footer lines at the end of the sheet)
        _df = _df.dropna(how="all", subset=["name", "exf_price", "pub_price"])

        _df.insert(0, "ref_date", _r["ref_date"])
        _df.insert(1, "source_file", _r["file"].name)

        _parts.append(_df)
        _log.append({
            "ref_date": _r["ref_date"],
            "file": _r["file"].name,
            "rows": len(_df),
            "ignored": "|".join(_ignored),
        })

    sl_raw = pd.concat(_parts, ignore_index=True)
    import_log = pd.DataFrame(_log)

    print(len(sl_raw), "rows")
    import_log
    return import_log, sl_raw


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    **Result:** 2,493,907 rows from 282 files, between 6,328 (2004-07) and
    10,317 (2026-04) packages per month. The only ignored column is an unnamed,
    empty-header column (`Unnamed: 22`) in the files 2023-01 to 2023-03.
    """)
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 5. Data types and export

    Minimal type conversion before saving:
    - Identifiers stay text; trailing `.0` from Excel number cells is removed.
    - Prices are converted to numbers; values that cannot be converted become
      missing and are counted.
    - The co-payment rate is stored as integer.

    Date columns from the files (`listing_date`, `last_price_change`) are kept
    as text for now, as their format varies between years; they are converted
    in the cleaning step. The panel is saved as Parquet, the import log as CSV.
    """)
    return


@app.cell
def _(OUT_DIR, import_log, pd, sl_raw):
    sl = sl_raw.copy()

    # IDs: keep as text, remove ".0" from Excel numbers
    for _c in ["pharmacode", "dossier", "swissmedic_nr", "rec_id", "gtin", "parallel_import_nr"]:
        sl[_c] = sl[_c].astype("string").str.strip().str.replace(r"\.0$", "", regex=True)

    # prices: numeric, count values that could not be converted
    for _c in ["exf_price", "pub_price"]:
        _num = pd.to_numeric(sl[_c], errors="coerce")
        print(_c, "not numeric:", (sl[_c].notna() & _num.isna()).sum())
        sl[_c] = _num

    sl["deductible_rate"] = sl["deductible_rate"].astype("Int64")

    sl.to_parquet(OUT_DIR / "sl_panel.parquet", index=False)
    import_log.to_csv(OUT_DIR / "import_log.csv", index=False)
    print("saved:", len(sl), "rows")
    return


@app.cell
def _(OUT_DIR, pd):
    _check = pd.read_parquet(OUT_DIR / "sl_panel.parquet")
    print(_check.shape)
    _check.head()
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    **Result:** all price values could be converted to numbers (0 non-numeric
    values). The panel has 30 columns.

    Note that some columns exist in the file headers for longer than they actually
    contain values:

    | Column | In header | Values present |
    |---|---|---|
    | `pharmacode` | 2003-01 – 2020-05 | 2003-01 – 2014-12 |
    | `price_model` | from 2010-10 | from 2021-01 (few packages) |
    | `atc` | from 2010-10 | sporadic, consistently from 2016 |
    | `lim_points` | from 2003-01 | mostly empty from 2009 |
    | `generic` | from 2003-01 | filled for 17 % of rows in 2003, rising to 63 % in 2026; the meaning of empty values is unclear |
    """)
    return


if __name__ == "__main__":
    app.run()
