"""Read a roster extract (.xlsx / .xls / .csv) and find date -> roster-code series.

Rosters come in two common shapes and both are detected:
  * across:  a header row of dates, one row per person (codes under each date)
  * down:    a column of dates, with codes in a neighbouring column
The caller picks which series is theirs; matching codes to shifts happens in the browser.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, time, timedelta

MAX_ROWS, MAX_COLS = 600, 200
MON = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
WD = r"(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s*"
RE_ISO = re.compile(rf"^(?:{WD})?(\d{{4}})-(\d{{1,2}})-(\d{{1,2}})(?:[ T]\d{{1,2}}:\d{{2}}(?::\d{{2}})?)?$", re.I)
RE_DMY = re.compile(rf"^(?:{WD})?(\d{{1,2}})[/.\-](\d{{1,2}})[/.\-](\d{{2,4}})(?:\s+\d{{1,2}}:\d{{2}}(?::\d{{2}})?)?$", re.I)
RE_DM = re.compile(rf"^(?:{WD})?(\d{{1,2}})[/.](\d{{1,2}})$", re.I)
RE_DMON = re.compile(rf"^(?:{WD})?(\d{{1,2}})(?:st|nd|rd|th)?[\s\-]*([a-z]{{3}})[a-z]*\.?[\s\-,]*(\d{{2,4}})?$", re.I)
RE_MOND = re.compile(rf"^(?:{WD})?([a-z]{{3}})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s*(\d{{4}})?$", re.I)


def _year_near(m: int, d: int, today: date) -> date | None:
    best = None
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            cand = date(y, m, d)
        except ValueError:
            continue
        if best is None or abs((cand - today).days) < abs((best - today).days):
            best = cand
    return best


def _mk(y: int, m: int, d: int) -> date | None:
    if y < 100:
        y += 2000
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_date(v, today: date) -> date | None:
    if isinstance(v, datetime):
        return v.date() if v.year > 1900 else None
    if isinstance(v, date):
        return v
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s or len(s) > 30:
        return None
    if m := RE_ISO.match(s):
        return _mk(int(m[1]), int(m[2]), int(m[3]))
    if m := RE_DMY.match(s):                       # Australian day/month/year
        return _mk(int(m[3]), int(m[2]), int(m[1]))
    if m := RE_DM.match(s):
        return _year_near(int(m[2]), int(m[1]), today)
    if (m := RE_DMON.match(s)) and m[2].lower()[:3] in MON:
        mo = MON[m[2].lower()[:3]]
        return _mk(int(m[3]), mo, int(m[1])) if m[3] else _year_near(mo, int(m[1]), today)
    if (m := RE_MOND.match(s)) and m[1].lower()[:3] in MON:
        mo = MON[m[1].lower()[:3]]
        return _mk(int(m[3]), mo, int(m[2])) if m[3] else _year_near(mo, int(m[2]), today)
    return None


def text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        if v.year <= 1900:          # Excel time-only values
            return v.strftime("%H:%M")
        return v.strftime("%Y-%m-%d %H:%M") if (v.hour or v.minute) else v.strftime("%Y-%m-%d")
    if isinstance(v, time):
        return v.strftime("%H:%M")
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(v)
    return str(v).strip()


# ---------------------------------------------------------------- readers
def _read_xlsx(data: bytes) -> list[tuple[str, list[list]]]:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    out = []
    for ws in wb.worksheets:
        rows = []
        for r in ws.iter_rows(max_row=MAX_ROWS, max_col=MAX_COLS, values_only=True):
            rows.append(list(r))
        out.append((ws.title, rows))
    return out


def _read_xls(data: bytes) -> list[tuple[str, list[list]]]:
    import xlrd
    book = xlrd.open_workbook(file_contents=data)
    out = []
    for sh in book.sheets():
        rows = []
        for r in range(min(sh.nrows, MAX_ROWS)):
            row = []
            for c in range(min(sh.ncols, MAX_COLS)):
                cell = sh.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        row.append(xlrd.xldate.xldate_as_datetime(cell.value, book.datemode))
                    except Exception:
                        row.append(cell.value)
                else:
                    row.append(cell.value if cell.value != "" else None)
            rows.append(row)
        out.append((sh.name, rows))
    return out


def _read_csv(data: bytes) -> list[tuple[str, list[list]]]:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            txt = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(txt[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [row[:MAX_COLS] for _, row in zip(range(MAX_ROWS), csv.reader(io.StringIO(txt), dialect))]
    return [("CSV", [[c if c != "" else None for c in r] for r in rows])]


def read_book(data: bytes, filename: str):
    name = filename.lower()
    if name.endswith(".csv") or name.endswith(".txt"):
        return _read_csv(data)
    if name.endswith(".xls") and not data[:4] == b"PK\x03\x04":
        return _read_xls(data)
    return _read_xlsx(data)


# ---------------------------------------------------------------- detection
def find_series(sheets, today: date | None = None) -> list[dict]:
    today = today or date.today()
    cands: list[dict] = []
    for sheet, rows in sheets:
        width = max((len(r) for r in rows), default=0)
        grid = [r + [None] * (width - len(r)) for r in rows]
        dates = [[parse_date(v, today) for v in r] for r in grid]

        # across: header rows with >= 5 dates
        header_rows = [i for i, r in enumerate(dates) if sum(1 for d in r if d) >= 5]
        for n, hi in enumerate(header_rows):
            cols = {j: d for j, d in enumerate(dates[hi]) if d}
            stop = header_rows[n + 1] if n + 1 < len(header_rows) else len(grid)
            first_col = min(cols)
            for ri in range(hi + 1, stop):
                row = grid[ri]
                cells = {cols[j].isoformat(): text(row[j]) for j in cols if text(row[j])}
                if not cells or sum(1 for d in dates[ri] if d) >= 5:
                    continue
                label = " ".join(text(row[j]) for j in range(first_col) if text(row[j])) or f"Row {ri + 1}"
                cands.append({"sheet": sheet, "label": label[:80], "layout": "across", "cells": cells})

        # down: columns with >= 5 dates
        for j in range(width):
            date_rows = [i for i in range(len(grid)) if dates[i][j]]
            if len(date_rows) < 5:
                continue
            for k in range(width):
                if k == j:
                    continue
                cells = {}
                for i in date_rows:
                    t = text(grid[i][k])
                    if t and not dates[i][k]:
                        cells[dates[i][j].isoformat()] = t
                if len(cells) < 1:
                    continue
                above = date_rows[0] - 1
                head = text(grid[above][k]) if above >= 0 else ""
                cands.append({"sheet": sheet, "label": (head or f"Column {k + 1}")[:80], "layout": "down",
                              "cells": cells})

    wd = re.compile(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?$", re.I)
    seen, out = set(), []
    for c in cands:
        if all(wd.match(v) for v in c["cells"].values()):
            continue  # a row/column of weekday names, not a roster
        key = (c["sheet"], c["label"], tuple(sorted(c["cells"].items())))
        if key in seen:
            continue
        seen.add(key)
        days = sorted(c["cells"])
        c.update({"id": len(out), "count": len(days), "start": days[0], "end": days[-1]})
        out.append(c)
        if len(out) >= 400:
            break
    return out
