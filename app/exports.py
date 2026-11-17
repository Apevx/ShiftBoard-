"""PDF month calendar and spreadsheet (xlsx) exports."""
from __future__ import annotations

import calendar
import io
from collections import defaultdict
from datetime import date, datetime, timedelta

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from reportlab.lib.colors import Color, HexColor, white
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

MONTHS = list(calendar.month_name)
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
KIND_LABEL = {"shift": "Ordinary", "leave": "Paid leave", "ot_shift": "Overtime shift", "overtime": "Overtime",
              "callin": "Called in", "remote": "Remote recall", "oncall": "On call"}


def _lum(hex_: str) -> float:
    n = int(hex_[1:], 16)
    def ch(x):
        x /= 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(n >> 16) + 0.7152 * ch((n >> 8) & 255) + 0.0722 * ch(n & 255)


def _fit(c: canvas.Canvas, text: str, font: str, size: float, width: float) -> str:
    if c.stringWidth(text, font, size) <= width:
        return text
    while text and c.stringWidth(text + "…", font, size) > width:
        text = text[:-1]
    return text + "…"


# ---------------------------------------------------------------- PDF
def month_pdf(year: int, month: int, title: str, shifts: dict, assignments: list, oncall: list,
              extras: list, notes: dict, week_start_sun: bool = False) -> bytes:
    buf = io.BytesIO()
    W, H = landscape(A4)
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.setTitle(f"{title} — {MONTHS[month]} {year}")
    M = 26
    first = date(year, month, 1)
    offset = (first.weekday() + (1 if week_start_sun else 0)) % 7
    grid_start = first - timedelta(days=offset)
    days_in = calendar.monthrange(year, month)[1]
    weeks = (offset + days_in + 6) // 7
    dow = (["Sun"] + DOW[:6]) if week_start_sun else DOW

    # title
    c.setFillColor(HexColor("#0f172a"))
    c.setFont("Helvetica-Bold", 22)
    c.drawString(M, H - M - 18, f"{MONTHS[month]} {year}")
    c.setFont("Helvetica", 11)
    c.setFillColor(HexColor("#64748b"))
    c.drawRightString(W - M, H - M - 16, title)

    footer_h = 58
    top = H - M - 34
    head_h = 16
    cw = (W - 2 * M) / 7
    ch = (top - head_h - (M + footer_h)) / weeks

    c.setFont("Helvetica-Bold", 9)
    for i, name in enumerate(dow):
        c.setFillColor(HexColor("#64748b"))
        c.drawCentredString(M + cw * i + cw / 2, top - 11, name)

    by_day = defaultdict(list)
    for a in assignments:
        if a["shift_id"] in shifts:
            by_day[a["day"]].append(a)
    x_by_day = defaultdict(list)
    for x in extras:
        x_by_day[x["day"]].append(x)
    xlabel = {"callin": "Called in", "remote": "Remote", "overtime": "Overtime"}

    tot = {"ord": 0.0, "ot": 0.0, "shifts": 0, "callin": 0, "oncall": 0}
    for o in oncall:
        s = datetime.fromisoformat(o["start"])
        if s.year == year and s.month == month:
            tot["oncall"] += 1

    for i in range(weeks * 7):
        d = grid_start + timedelta(days=i)
        iso = d.isoformat()
        col, row = i % 7, i // 7
        x0 = M + col * cw
        y1 = top - head_h - row * ch     # top edge
        y0 = y1 - ch
        inm = d.month == month
        c.setStrokeColor(HexColor("#d7dce3"))
        c.setLineWidth(0.6)
        c.setFillColor(HexColor("#ffffff") if inm else HexColor("#f3f4f6"))
        c.rect(x0, y0, cw, ch, stroke=1, fill=1)
        c.setFillColor(HexColor("#0f172a") if inm else HexColor("#9ca3af"))
        c.setFont("Helvetica-Bold", 10)
        c.drawString(x0 + 4, y1 - 12, str(d.day))
        if not inm:
            continue

        y = y1 - 17
        items = sorted(by_day.get(iso, []), key=lambda a: shifts[a["shift_id"]]["sort"])
        chip_h = 15 if len(items) <= 2 else 11
        for a in items:
            s = shifts[a["shift_id"]]
            ot = bool(a.get("ot"))
            c.setFillColor(HexColor(s["color"]))
            if ot:
                c.setStrokeColor(HexColor("#dc2626"))
                c.setLineWidth(1.6)
            c.roundRect(x0 + 3, y - chip_h, cw - 6, chip_h, 3, stroke=1 if ot else 0, fill=1)
            c.setFillColor(HexColor("#0f172a") if _lum(s["color"]) > 0.45 else white)
            fs = 8 if chip_h > 12 else 7
            c.setFont("Helvetica-Bold", fs)
            label = s["code"] if s["all_day"] else f"{s['code']}  {s['start_time']}–{s['end_time']}"
            if s["all_day"]:
                label = f"{s['code']}  {s['name']}"
            avail = cw - 14 - (14 if ot else 0)
            c.drawString(x0 + 7, y - chip_h + (chip_h - fs) / 2 + 1, _fit(c, label, "Helvetica-Bold", fs, avail))
            if ot:
                c.drawRightString(x0 + cw - 7, y - chip_h + (chip_h - fs) / 2 + 1, "OT")
            y -= chip_h + 2
            tot["shifts"] += 1
            if not s["all_day"]:
                if ot or s.get("pay_type") == "overtime":
                    tot["ot"] += s.get("hours", 0)
                elif s.get("pay_type", "ordinary") == "ordinary":
                    tot["ord"] += s.get("hours", 0)

        for x in x_by_day.get(iso, []):
            c.setFillColor(HexColor("#dc2626"))
            c.setFont("Helvetica-Bold", 7)
            c.drawString(x0 + 4, y - 8, _fit(c, f"{xlabel[x['kind']]} {x['start']}–{x['end']}", "Helvetica-Bold", 7, cw - 8))
            y -= 9
            if x["kind"] in ("callin", "remote"):
                tot["callin"] += 1

        if notes.get(iso):
            c.setFillColor(HexColor("#64748b"))
            c.setFont("Helvetica-Oblique", 6.5)
            c.drawString(x0 + 4, y - 8, _fit(c, notes[iso].replace("\n", " "), "Helvetica-Oblique", 6.5, cw - 8))
            y -= 9

        # on-call bars along the bottom of the cell
        ds = datetime.combine(d, datetime.min.time())
        de = ds + timedelta(days=1)
        by = y0 + 3
        for o in oncall:
            s_, e_ = datetime.fromisoformat(o["start"]), datetime.fromisoformat(o["end"])
            if not (s_ < de and e_ > ds):
                continue
            starts, ends = s_ >= ds, e_ <= de
            fx0 = x0 + 3 + ((s_ - ds).total_seconds() / 86400 * (cw - 6) if starts else 0)
            fx1 = x0 + 3 + ((e_ - ds).total_seconds() / 86400 * (cw - 6) if ends else cw - 6)
            c.setFillColor(HexColor(o.get("color") or "#f59e0b"))
            c.roundRect(fx0, by, max(4, fx1 - fx0), 5, 2, stroke=0, fill=1)
            lab = o.get("label") or "On call"
            if starts and ends:
                txt = f"{lab} {s_:%H:%M}–{e_:%H:%M}"
            elif starts:
                txt = f"{lab} from {s_:%H:%M}"
            elif ends:
                txt = f"{lab} until {e_:%H:%M}"
            else:
                txt = lab
            c.setFillColor(HexColor("#92400e"))
            c.setFont("Helvetica", 6.5)
            c.drawString(x0 + 4, by + 7, _fit(c, txt, "Helvetica", 6.5, cw - 8))
            by += 15

    # footer: legend + totals
    fy = M + footer_h - 14
    lx = M
    c.setFont("Helvetica-Bold", 7.5)
    for s in sorted((s for s in shifts.values() if not s["archived"]), key=lambda s: s["sort"]):
        label = f"{s['code']} {s['name']}" + ("" if s["all_day"] else f" {s['start_time']}–{s['end_time']}")
        w = c.stringWidth(label, "Helvetica-Bold", 7.5) + 12
        if lx + w > W - M:
            lx, fy = M, fy - 14
        c.setFillColor(HexColor(s["color"]))
        c.roundRect(lx, fy - 2, w, 11, 2.5, stroke=0, fill=1)
        c.setFillColor(HexColor("#0f172a") if _lum(s["color"]) > 0.45 else white)
        c.drawString(lx + 6, fy + 1, label)
        lx += w + 6
    c.setFillColor(HexColor("#334155"))
    c.setFont("Helvetica", 9)
    summary = (f"{tot['shifts']} shifts · {tot['ord']:g} h ordinary · {tot['ot']:g} h overtime shifts · "
               f"{tot['callin']} call-ins · {tot['oncall']} on-call periods")
    c.drawString(M, M + 6, summary)
    c.setFillColor(HexColor("#94a3b8"))
    c.setFont("Helvetica", 7)
    c.drawRightString(W - M, M + 6, f"Red outline = overtime · amber bar = on call · generated {date.today():%d %b %Y}")
    c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------- spreadsheet
HEAD_FILL = PatternFill("solid", fgColor="1E3A8A")
HEAD_FONT = Font(bold=True, color="FFFFFF")
TOTAL_FONT = Font(bold=True)
THIN = Border(top=Side(style="thin", color="94A3B8"))
MONEY = '"$"#,##0.00'
HRS = "0.00"


def _sheet(wb, title, headers, widths):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
        cell = ws.cell(row=1, column=i)
        cell.fill, cell.font = HEAD_FILL, HEAD_FONT
        cell.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"
    return ws


def _fmt(ws, col, fmt, start=2):
    for r in range(start, ws.max_row + 1):
        ws.cell(row=r, column=col).number_format = fmt


def hours_xlsx(start: date, end: date, fortnights: list[dict], title: str) -> bytes:
    """fortnights: pay.compute() results covering start..end (each with 'lines')."""
    configured = any(f.get("configured") for f in fortnights)
    lines = [ln for f in fortnights for ln in f.get("lines", []) if start.isoformat() <= ln["day"] <= end.isoformat()]
    lines.sort(key=lambda x: (x["day"], x["start"] or ""))
    wb = Workbook()
    wb.remove(wb.active)

    # --- Summary (filled after details so totals are known)
    sm = wb.create_sheet("Summary")

    # --- Shifts
    ws = _sheet(wb, "Shifts", ["Date", "Day", "Code", "Shift", "Start", "Finish", "Paid hours", "Type",
                               "Penalties applied", "Base pay", "Penalty pay", "Total"],
                [12, 6, 7, 16, 8, 8, 11, 15, 22, 12, 12, 12])
    shift_lines = [ln for ln in lines if ln["kind"] in ("shift", "leave", "ot_shift")]
    for ln in shift_lines:
        d = date.fromisoformat(ln["day"])
        ws.append([d, d.strftime("%a"), ln["code"], ln["name"], ln["start"], ln["end"], round(ln["hours"], 2),
                   KIND_LABEL[ln["kind"]], ln["detail"] if ln["kind"] == "shift" else "",
                   round(ln["base"], 2) if ln["kind"] != "ot_shift" else None,
                   round(ln["penalty"], 2) if ln["kind"] == "shift" else None, round(ln["amount"], 2)])
    n = ws.max_row
    ws.append(["Total", "", "", "", "", "", round(sum(l["hours"] for l in shift_lines), 2), "", "",
               round(sum(l["base"] for l in shift_lines), 2), round(sum(l["penalty"] for l in shift_lines), 2),
               round(sum(l["amount"] for l in shift_lines), 2)])
    for cell in ws[ws.max_row]:
        cell.font, cell.border = TOTAL_FONT, THIN
    _fmt(ws, 1, "dd/mm/yyyy", 2)
    ws.cell(row=ws.max_row, column=1).number_format = "General"
    _fmt(ws, 7, HRS)
    for col in (10, 11, 12):
        _fmt(ws, col, MONEY)

    # --- Extra work
    wx = _sheet(wb, "Extra work", ["Date", "Day", "Type", "Start", "Finish", "Hours worked", "Hours paid", "Note", "Amount"],
                [12, 6, 15, 8, 8, 13, 11, 30, 12])
    x_lines = [ln for ln in lines if ln["kind"] in ("callin", "remote", "overtime")]
    for ln in x_lines:
        d = date.fromisoformat(ln["day"])
        wx.append([d, d.strftime("%a"), ln["name"], ln["start"], ln["end"], round(ln.get("actual_hours", ln["hours"]), 2),
                   round(ln["hours"], 2), ln["detail"], round(ln["amount"], 2)])
    wx.append(["Total", "", "", "", "", round(sum(l.get("actual_hours", l["hours"]) for l in x_lines), 2),
               round(sum(l["hours"] for l in x_lines), 2), "", round(sum(l["amount"] for l in x_lines), 2)])
    for cell in wx[wx.max_row]:
        cell.font, cell.border = TOTAL_FONT, THIN
    _fmt(wx, 1, "dd/mm/yyyy")
    wx.cell(row=wx.max_row, column=1).number_format = "General"
    _fmt(wx, 6, HRS); _fmt(wx, 7, HRS); _fmt(wx, 9, MONEY)

    # --- On call
    wo = _sheet(wb, "On call", ["Allowance date", "Day", "Label", "Period start", "Period end", "Duty status", "Allowance"],
                [14, 6, 14, 17, 17, 18, 12])
    o_lines = [ln for ln in lines if ln["kind"] == "oncall"]
    for ln in o_lines:
        d = date.fromisoformat(ln["day"])
        wo.append([d, d.strftime("%a"), ln["name"], datetime.fromisoformat(ln["start"]), datetime.fromisoformat(ln["end"]),
                   ln["detail"], round(ln["amount"], 2)])
    wo.append(["Total", "", f"{len(o_lines)} allowances", "", "", "", round(sum(l["amount"] for l in o_lines), 2)])
    for cell in wo[wo.max_row]:
        cell.font, cell.border = TOTAL_FONT, THIN
    _fmt(wo, 1, "dd/mm/yyyy")
    wo.cell(row=wo.max_row, column=1).number_format = "General"
    _fmt(wo, 4, "dd/mm/yyyy hh:mm"); _fmt(wo, 5, "dd/mm/yyyy hh:mm"); _fmt(wo, 7, MONEY)

    # --- Fortnights (whole pay periods, including any that run past the range edges)
    wf = _sheet(wb, "Fortnights", ["Period start", "Period end", "Ordinary hours", "Ordinary pay", "Penalties",
                                   "Paid leave", "Overtime hours", "Overtime pay", "Call-ins", "Call-in pay",
                                   "On-call allowances", "On-call pay", "Estimated gross"],
                [13, 13, 14, 13, 12, 12, 14, 13, 9, 12, 17, 12, 15])
    fortnights = [f for f in fortnights if f.get("gross") or f.get("ordinary_hours") or f.get("leave_hours")
                  or f.get("overtime_hours") or f.get("oncall_count") or f.get("callin_count")]
    for f in fortnights:
        wf.append([date.fromisoformat(f["start"]), date.fromisoformat(f["end"]), f.get("ordinary_hours", 0),
                   f.get("ordinary_pay", 0), f.get("penalties", 0), f.get("leave_pay", 0), f.get("overtime_hours", 0),
                   f.get("overtime_pay", 0), f.get("callin_count", 0), f.get("callin_pay", 0),
                   f.get("oncall_count", 0), f.get("oncall_pay", 0), f.get("gross", 0)])
    keys = ["ordinary_hours", "ordinary_pay", "penalties", "leave_pay", "overtime_hours", "overtime_pay",
            "callin_count", "callin_pay", "oncall_count", "oncall_pay", "gross"]
    wf.append(["Total", ""] + [round(sum(f.get(k, 0) for f in fortnights), 2) for k in keys])
    for cell in wf[wf.max_row]:
        cell.font, cell.border = TOTAL_FONT, THIN
    _fmt(wf, 1, "dd/mm/yyyy"); _fmt(wf, 2, "dd/mm/yyyy")
    wf.cell(row=wf.max_row, column=1).number_format = "General"
    for col in (3, 7):
        _fmt(wf, col, HRS)
    for col in (4, 5, 6, 8, 10, 12, 13):
        _fmt(wf, col, MONEY)

    # --- Summary
    def tot(kinds, key="amount"):
        return round(sum(l[key] for l in lines if l["kind"] in kinds), 2)
    sm.column_dimensions["A"].width = 34
    sm.column_dimensions["B"].width = 14
    sm.column_dimensions["C"].width = 16
    sm["A1"] = title
    sm["A1"].font = Font(bold=True, size=16)
    sm["A2"] = f"{start:%d %b %Y} – {end:%d %b %Y}"
    sm["A2"].font = Font(size=12, color="475569")
    rates = []
    for f in fortnights:
        if f.get("hourly") and f.get("rate_label") and (f["rate_label"], f["hourly"]) not in rates:
            rates.append((f["rate_label"], f["hourly"]))
    sm["A3"] = (" → ".join(f"{l} · ${h:,.2f}/h" for l, h in rates) if rates
                else "Pay classification not set — dollar amounts are $0")
    sm["A3"].font = Font(italic=True, color="64748B")
    rows = [
        ("Ordinary hours", tot({"shift"}, "hours"), tot({"shift"}, "base")),
        ("Penalties (shift, weekend, public holiday)", None, tot({"shift"}, "penalty")),
        ("Paid leave", tot({"leave"}, "hours"), tot({"leave"})),
        ("Overtime (shifts + extra hours)", tot({"ot_shift", "overtime"}, "hours"), tot({"ot_shift", "overtime"})),
        ("Called in / remote recall (paid hours)", tot({"callin", "remote"}, "hours"), tot({"callin", "remote"})),
        (f"On-call allowances ({len(o_lines)})", None, tot({"oncall"})),
    ]
    sm.append([])
    sm.append(["", "Hours", "Amount"])
    for cell in sm[sm.max_row]:
        cell.font = Font(bold=True)
    for r in rows:
        sm.append(list(r))
    gross = round(sum(r[2] for r in rows), 2)
    sm.append(["Estimated gross", round(sum((r[1] or 0) for r in rows), 2), gross])
    for cell in sm[sm.max_row]:
        cell.font, cell.border = TOTAL_FONT, THIN
    for r in range(6, sm.max_row + 1):
        sm.cell(row=r, column=2).number_format = HRS
        sm.cell(row=r, column=3).number_format = MONEY

    sm.append([])
    sm.append(["Shifts by type", "Count", "Paid hours"])
    for cell in sm[sm.max_row]:
        cell.font = Font(bold=True)
    agg = defaultdict(lambda: [0, 0.0])
    for ln in shift_lines:
        k = f"{ln['code']} {ln['name']}" + (" (overtime)" if ln["kind"] == "ot_shift" else "")
        agg[k][0] += 1
        agg[k][1] += ln["hours"]
    for k, (cnt, hrs) in sorted(agg.items()):
        sm.append([k, cnt, round(hrs, 2)])
        sm.cell(row=sm.max_row, column=3).number_format = HRS
    sm.append(["Days worked", len({l["day"] for l in shift_lines if l["kind"] != "leave"}), None])
    sm.append(["Call-ins / remote recalls", len([l for l in x_lines if l["kind"] != "overtime"]), None])
    sm.append([])
    sm.append(["Estimate of gross pay before tax and super, calculated from the NSW Health awards."])
    sm.append(["Fortnights tab shows whole pay periods, which can start before or finish after this range."])
    for r in (sm.max_row - 1, sm.max_row):
        sm.cell(row=r, column=1).font = Font(italic=True, color="64748B")
    if not configured:
        sm.append(["Set your classification in Settings › Pay to include dollar amounts."])

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
