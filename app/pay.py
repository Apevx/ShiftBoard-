"""Fortnightly pay estimate for NSW Health radiographers.

Sources (NSW Industrial Gazette Vol. 398, 21 Jan 2026):
  * Health Employees' Medical Radiation Scientists (State) Award 2025 — Part B, Table 1 (35 hr weekly rates)
  * Health Employees' Conditions of Employment (State) Award 2025
      cl 9  overtime: x1.5 first 2 h each day then x2; Sunday x2; public holiday x2.5; each day stands alone
            recall: min 4 h at OT rates; remote recall: min 1 h at OT rates
      cl 10 on-call allowance per 24 h or part (Table 1 items 5/6)
      cl 11 shift penalties (Mon-Fri) by start time; Saturday x1.5, Sunday x1.75 (replace shift penalties)
      cl 15 public holiday: time and a half extra for rostered hours (in lieu of weekend/shift penalties)
Estimates only — gross pay before tax, super and salary packaging.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta

# "ffppoa" = first full pay period on or after this date
RATE_DATES = [date(2025, 7, 1), date(2026, 7, 1)]

# key: (label, weekly rate ffppoa 1-Jul-2025, weekly rate ffppoa 1-Jul-2026)  — 35 hr week
RADIOGRAPHER = {
    "L1Y1": ("Level 1 Year 1", 1463.71, 1522.26),
    "L2Y1": ("Level 2 Year 1", 1515.70, 1576.33),
    "L2Y2": ("Level 2 Year 2", 1709.67, 1778.06),
    "L2Y3": ("Level 2 Year 3", 1934.20, 2011.57),
    "L2Y4": ("Level 2 Year 4", 2024.94, 2105.94),
    "L2Y5": ("Level 2 Year 5", 2087.91, 2171.43),
    "L3G1Y1": ("Level 3 Grade 1 Year 1", 2240.54, 2330.16),
    "L3G1Y2": ("Level 3 Grade 1 Year 2", 2312.99, 2405.51),
    "L3G2Y1": ("Level 3 Grade 2 Year 1", 2375.20, 2470.21),
    "L3G2Y2": ("Level 3 Grade 2 Year 2", 2627.62, 2732.72),
    "L3G3Y1": ("Level 3 Grade 3 Year 1", 2698.66, 2806.61),
    "L4G1Y1": ("Level 4 Grade 1 Year 1", 2698.66, 2806.61),
    "L4G1Y2": ("Level 4 Grade 1 Year 2", 2787.79, 2899.30),
    "L4G2Y1": ("Level 4 Grade 2 Year 1", 2870.24, 2985.05),
    "L4G2Y2": ("Level 4 Grade 2 Year 2", 2940.75, 3058.38),
    "L5Y1": ("Level 5 Year 1", 3145.58, 3271.40),
    "L5Y2": ("Level 5 Year 2", 3222.22, 3351.11),
    "L5Y3": ("Level 5 Year 3", 3384.00, 3519.36),
    "L6Y1": ("Level 6 Year 1", 3464.30, 3602.87),
    "L6Y2": ("Level 6 Year 2", 3543.63, 3685.38),
    "L6Y3": ("Level 6 Year 3", 3623.61, 3768.55),
}
ONCALL_ON_DUTY = (30.24, 31.45)   # item 5: on call, rostered on duty, per 24 h or part
ONCALL_OFF_DUTY = (59.61, 61.99)  # item 6: on call, rostered off duty, per 24 h or part
WEEKLY_HOURS = 35

DEFAULT_SETTINGS = {
    "classification": "",       # key of RADIOGRAPHER; "" = not set up
    "hourly_override": None,    # optional manual base hourly rate
    "fortnight_start": "2026-11-09",  # any date a pay fortnight starts on
    "extra_holidays": [],       # extra public-holiday dates (ISO)
    "not_holidays": [],         # computed holidays to ignore (ISO)
}

MIN_HOURS = {"callin": 4.0, "remote": 1.0, "overtime": 0.0}
EXTRA_LABEL = {"callin": "Called in", "remote": "Remote recall", "overtime": "Overtime"}


# ---------------------------------------------------------------- public holidays (NSW)
def _easter(y: int) -> date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(y, month, day)


def _nth_monday(y: int, m: int, n: int) -> date:
    d = date(y, m, 1)
    d += timedelta(days=(7 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def nsw_holidays(y: int) -> dict[date, str]:
    h: dict[date, str] = {}
    ny = date(y, 1, 1)
    h[ny] = "New Year's Day"
    if ny.weekday() >= 5:
        h[ny + timedelta(days=7 - ny.weekday())] = "New Year's Day (additional)"
    ad = date(y, 1, 26)
    if ad.weekday() >= 5:
        ad += timedelta(days=7 - ad.weekday())
    h[ad] = "Australia Day"
    e = _easter(y)
    h[e - timedelta(days=2)] = "Good Friday"
    h[e - timedelta(days=1)] = "Easter Saturday"
    h[e] = "Easter Sunday"
    h[e + timedelta(days=1)] = "Easter Monday"
    h[date(y, 4, 25)] = "Anzac Day"
    h[_nth_monday(y, 6, 2)] = "King's Birthday"
    h[_nth_monday(y, 10, 1)] = "Labour Day"
    xmas, boxing = date(y, 12, 25), date(y, 12, 26)
    h[xmas] = "Christmas Day"
    h[boxing] = "Boxing Day"
    if xmas.weekday() == 5:          # Sat: Mon 27 additional; Boxing Sun -> Tue 28
        h[date(y, 12, 27)] = "Christmas Day (additional)"
        h[date(y, 12, 28)] = "Boxing Day (additional)"
    elif xmas.weekday() == 6:        # Sun: Boxing Mon 26, Tue 27 additional
        h[date(y, 12, 27)] = "Christmas Day (additional)"
    elif boxing.weekday() == 5:      # Fri Xmas, Sat Boxing -> Mon 28
        h[date(y, 12, 28)] = "Boxing Day (additional)"
    return h


def holidays_between(start: date, end: date, settings: dict) -> dict[date, str]:
    out: dict[date, str] = {}
    for y in range(start.year, end.year + 1):
        out.update(nsw_holidays(y))
    for s in settings.get("extra_holidays") or []:
        try:
            out[date.fromisoformat(s)] = "Public holiday"
        except ValueError:
            pass
    for s in settings.get("not_holidays") or []:
        try:
            out.pop(date.fromisoformat(s), None)
        except ValueError:
            pass
    return {d: n for d, n in out.items() if start <= d <= end}


# ---------------------------------------------------------------- rates / periods
def rate_index(period_start: date) -> int:
    idx = 0
    for i, d in enumerate(RATE_DATES):
        if period_start >= d:
            idx = i
    return idx


def base_hourly(settings: dict, period_start: date) -> tuple[float | None, str]:
    if settings.get("hourly_override"):
        return float(settings["hourly_override"]), "Custom hourly rate"
    key = settings.get("classification")
    if key not in RADIOGRAPHER:
        return None, ""
    label, *weekly = RADIOGRAPHER[key]
    idx = rate_index(period_start)
    col = "1 Jul 2026" if idx == 1 else "1 Jul 2025"
    return weekly[idx] / WEEKLY_HOURS, f"Radiographer {label} (rates from {col})"


def fortnight_containing(d: date, settings: dict) -> date:
    anchor = date.fromisoformat(settings.get("fortnight_start") or DEFAULT_SETTINGS["fortnight_start"])
    return anchor + timedelta(days=((d - anchor).days // 14) * 14)


def shift_penalty_pct(start: time) -> float:
    """cl 11(ii): Mon-Fri shift allowance by shift start time."""
    m = start.hour * 60 + start.minute
    if 600 <= m < 780:
        return 0.10
    if 780 <= m < 960:
        return 0.125
    if m >= 960 or m < 240:
        return 0.15
    if 240 <= m < 360:
        return 0.10
    return 0.0


def _span(day: date, start_s: str, end_s: str) -> tuple[datetime, datetime]:
    st = datetime.combine(day, time.fromisoformat(start_s))
    en = datetime.combine(day, time.fromisoformat(end_s))
    if en <= st:
        en += timedelta(days=1)
    return st, en


def _segments(st: datetime, en: datetime):
    """Split [st, en) at midnights -> (date, hours)."""
    cur = st
    while cur < en:
        nxt = min(en, datetime.combine(cur.date() + timedelta(days=1), time()))
        yield cur.date(), (nxt - cur).total_seconds() / 3600
        cur = nxt


# ---------------------------------------------------------------- calculation
def compute(period_start: date, shifts: dict, assignments: list, extras: list, oncall: list, settings: dict) -> dict:
    """shifts: id->dict; assignments: [{day, shift_id}]; extras: [{day,kind,start,end,...}];
    oncall: [{start,end,label}] (local ISO 'YYYY-MM-DDTHH:MM')."""
    period_end = period_start + timedelta(days=13)
    rate, rate_label = base_hourly(settings, period_start)
    idx = rate_index(period_start)
    hols = holidays_between(period_start - timedelta(days=1), period_end + timedelta(days=1), settings)
    out = {
        "start": period_start.isoformat(),
        "end": period_end.isoformat(),
        "configured": rate is not None,
        "hourly": round(rate, 4) if rate else None,
        "rate_label": rate_label,
        "holidays": {d.isoformat(): n for d, n in hols.items() if period_start <= d <= period_end},
    }
    if rate is None:
        return out

    ord_hours = ord_pay = pen_pay = leave_hours = 0.0
    pen_detail = {"shift": 0.0, "saturday": 0.0, "sunday": 0.0, "holiday": 0.0}
    ot_items = []   # (day, start_dt, payable_hours, kind, actual_hours, ref)
    rostered_days = set()

    for a in assignments:
        d = date.fromisoformat(a["day"])
        s = shifts.get(a["shift_id"])
        if not s or not (period_start <= d <= period_end):
            continue
        ptype = "overtime" if a.get("ot") else (s.get("pay_type") or "ordinary")
        if ptype == "unpaid":
            continue
        if s["all_day"] or not s.get("start_time"):
            hrs = float(s.get("paid_hours") or 0)
            if ptype in ("ordinary", "leave") and hrs:
                leave_hours += hrs
            if ptype == "ordinary":
                rostered_days.add(d)
            continue
        st, en = _span(d, s["start_time"], s["end_time"])
        total = (en - st).total_seconds() / 3600
        paid = max(0.0, total - (s.get("break_mins") or 0) / 60)
        ratio = paid / total if total else 0
        if ptype == "overtime":
            ot_items.append((d, st, paid, "ot_shift", paid, s["name"]))
            continue
        if ptype == "leave":
            leave_hours += paid
            continue
        rostered_days.add(d)
        pct = shift_penalty_pct(st.time())
        for seg_day, seg_h in _segments(st, en):
            h = seg_h * ratio
            ord_hours += h
            ord_pay += h * rate
            if seg_day in hols:
                p, k = 1.5, "holiday"
            elif seg_day.weekday() == 5:
                p, k = 0.5, "saturday"
            elif seg_day.weekday() == 6:
                p, k = 0.75, "sunday"
            else:
                p, k = pct, "shift"
            pen_pay += h * rate * p
            pen_detail[k] += h * rate * p

    extra_out = []
    for x in extras:
        d = date.fromisoformat(x["day"])
        if not (period_start <= d <= period_end):
            continue
        st, en = _span(d, x["start"], x["end"])
        actual = (en - st).total_seconds() / 3600
        payable = max(actual, MIN_HOURS.get(x["kind"], 0))
        ot_items.append((d, st, payable, x["kind"], actual, x.get("id")))

    # overtime ladder — each day stands alone (cl 9(xiii)), keyed on the day the work started
    ot_items.sort(key=lambda t: t[1])
    used_by_day: dict[date, float] = {}
    ot_by_kind = {"ot_shift": 0.0, "overtime": 0.0, "callin": 0.0, "remote": 0.0}
    hrs_by_kind = {"ot_shift": 0.0, "overtime": 0.0, "callin": 0.0, "remote": 0.0}
    count_by_kind = {"ot_shift": 0, "overtime": 0, "callin": 0, "remote": 0}
    for d, st, payable, kind, actual, ref in ot_items:
        if d in hols:
            amt = payable * rate * 2.5
        elif d.weekday() == 6:
            amt = payable * rate * 2.0
        else:
            used = used_by_day.get(d, 0.0)
            at15 = max(0.0, min(payable, 2.0 - used))
            amt = (at15 * 1.5 + (payable - at15) * 2.0) * rate
            used_by_day[d] = used + payable
        ot_by_kind[kind] += amt
        hrs_by_kind[kind] += payable
        count_by_kind[kind] += 1
        extra_out.append({"id": ref if kind != "ot_shift" else None, "name": ref if kind == "ot_shift" else None,
                          "day": d.isoformat(), "kind": kind, "actual_hours": round(actual, 2),
                          "paid_hours": round(payable, 2), "amount": round(amt, 2)})

    # on-call: one allowance per 24 h or part (cl 10), dated by the start of each 24 h block
    oc_on = ONCALL_ON_DUTY[idx]
    oc_off = ONCALL_OFF_DUTY[idx]
    oc_pay, oc_blocks, oc_hours = 0.0, 0, 0.0
    p_st = datetime.combine(period_start, time())
    p_en = datetime.combine(period_end + timedelta(days=1), time())
    for o in oncall:
        st, en = datetime.fromisoformat(o["start"]), datetime.fromisoformat(o["end"])
        n = max(1, math.ceil((en - st).total_seconds() / 86400 - 1e-9))
        for i in range(n):
            bs = st + timedelta(days=i)
            if p_st <= bs < p_en:
                oc_blocks += 1
                oc_pay += oc_on if bs.date() in rostered_days else oc_off
        oc_hours += max(0.0, (min(en, p_en) - max(st, p_st)).total_seconds() / 3600)

    leave_pay = leave_hours * rate
    overtime = ot_by_kind["ot_shift"] + ot_by_kind["overtime"]
    callins = ot_by_kind["callin"] + ot_by_kind["remote"]
    gross = ord_pay + pen_pay + leave_pay + overtime + callins + oc_pay
    r2 = lambda v: round(v, 2)
    out.update({
        "ordinary_hours": r2(ord_hours),
        "ordinary_pay": r2(ord_pay),
        "penalties": r2(pen_pay),
        "penalty_detail": {k: r2(v) for k, v in pen_detail.items()},
        "leave_hours": r2(leave_hours),
        "leave_pay": r2(leave_pay),
        "overtime_hours": r2(hrs_by_kind["ot_shift"] + hrs_by_kind["overtime"]),
        "overtime_pay": r2(overtime),
        "callin_count": count_by_kind["callin"] + count_by_kind["remote"],
        "callin_hours": r2(hrs_by_kind["callin"] + hrs_by_kind["remote"]),
        "callin_pay": r2(callins),
        "oncall_count": oc_blocks,
        "oncall_hours": r2(oc_hours),
        "oncall_pay": r2(oc_pay),
        "oncall_rates": {"on_duty": oc_on, "off_duty": oc_off},
        "extras": extra_out,
        "contract_hours": WEEKLY_HOURS * 2,
        "gross": r2(gross),
    })
    return out


def classifications() -> list[dict]:
    return [{"key": k, "label": v[0], "weekly": v[1:]} for k, v in RADIOGRAPHER.items()]
