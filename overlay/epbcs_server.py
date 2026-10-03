"""EPM Copilot - EPBCS stand-in MCP server (synthetic Planning and Budgeting data).

Driver-based plan: Revenue = Units x Avg Price, Cost of Sales = Units x unit cost, Salaries = Headcount x cost per head.
Scenarios Actual (Jan-Sep FY26), Plan (FY26, Final) and Forecast (Working: actuals through Sep
plus Oct-Dec forecast). Security: exec = all entities, east/west only their entities, including writes.
Run:  python -m app.epbcs_server
"""

from __future__ import annotations

import copy
import random

from app.common import (ALL_ENTITIES, REGIONS, AccessDenied, build_mcp, log, run, scope_entities)

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
ACTUALS_THROUGH = 9
SCENARIOS = ["Actual", "Plan", "Forecast"]
DRIVERS = ["Units", "Avg Price", "Headcount", "Marketing", "Rent"]
ACCOUNTS = ["Units", "Avg Price", "Revenue", "Cost of Sales", "Gross Margin", "Headcount", "Salaries", "Marketing",
            "Rent", "Total Opex", "Operating Income", "Operating Margin %"]
COST_PER_HEAD = 7_500  # monthly fully loaded
UNIT_COST = 130.0
SIZE = {"NewYork": 1.4, "Boston": 0.8, "LosAngeles": 1.2, "Seattle": 0.7}

INSTRUCTIONS = ("EPM Copilot for Oracle EPBCS / Planning (synthetic demo data). Entities NewYork, Boston (East), "
                "LosAngeles, Seattle (West); FY2026; Actual through Sep, Plan (Final) and Forecast (Working). "
                "Call whoami first. Never invent numbers. Forecast edits only go to the Working version.")


def _build():
    rng = random.Random(20261003)
    d = {}
    for e in ALL_ENTITIES:
        s = SIZE[e]
        for mi, m in enumerate(MONTHS):
            season = 1 + 0.15 * (mi in (10, 11)) - 0.06 * (mi in (0, 1))
            plan = {"Units": round(4_000 * s * season), "Avg Price": 250.0,
                    "Headcount": round(40 * s), "Marketing": round(60_000 * s), "Rent": round(35_000 * s)}
            d[(e, m, "Plan")] = plan
            act = {"Units": round(plan["Units"] * rng.uniform(0.9, 1.1)), "Avg Price": round(250 * rng.uniform(0.96, 1.04), 2),
                   "Headcount": plan["Headcount"] + rng.choice([-2, -1, 0, 0, 1, 2]),
                   "Marketing": round(plan["Marketing"] * rng.uniform(0.85, 1.2)), "Rent": plan["Rent"]}
            d[(e, m, "Actual")] = act if mi < ACTUALS_THROUGH else None
            if mi < ACTUALS_THROUGH:
                d[(e, m, "Forecast")] = dict(act)
            else:  # forecast = plan adjusted by YTD run-rate, filled below
                d[(e, m, "Forecast")] = None
    for e in ALL_ENTITIES:
        ratio = (sum(d[(e, m, "Actual")]["Units"] for m in MONTHS[:ACTUALS_THROUGH]) /
                 sum(d[(e, m, "Plan")]["Units"] for m in MONTHS[:ACTUALS_THROUGH]))
        for m in MONTHS[ACTUALS_THROUGH:]:
            p = d[(e, m, "Plan")]
            d[(e, m, "Forecast")] = {**p, "Units": round(p["Units"] * ratio)}
    return d


_BASE = _build()
_DATA = copy.deepcopy(_BASE)
APPROVALS = {"NewYork": "Approved", "Boston": "Under Review", "LosAngeles": "First Pass", "Seattle": "Not Started"}

mcp = build_mcp("EPM Copilot - EPBCS (stand-in)", INSTRUCTIONS)


def _err(exc: Exception) -> dict:
    return {"error": str(exc)}


def _months(period: str) -> list[str]:
    p = period.strip()
    if p.lower() in ("fy", "year", "fy26", "total"):
        return list(MONTHS)
    if p.upper() in ("Q1", "Q2", "Q3", "Q4"):
        q = int(p[1]) - 1
        return MONTHS[q * 3:(q + 1) * 3]
    if p.lower() == "ytd":
        return MONTHS[:ACTUALS_THROUGH]
    m = p[:3].title()
    if m in MONTHS:
        return [m]
    raise ValueError("period must be FY, Q1..Q4, YTD or a month Jan..Dec.")


def _acc(account: str) -> str:
    acc = next((a for a in ACCOUNTS if a.lower() == account.strip().lower()), None)
    if not acc:
        raise ValueError(f"Unknown account '{account}'. Use: {', '.join(ACCOUNTS)}.")
    return acc


def _totals(entities: list[str], months: list[str], scenario: str, adj: dict | None = None) -> dict:
    if scenario not in SCENARIOS:
        raise ValueError("scenario must be Actual, Plan or Forecast.")
    adj = adj or {}
    t = {"Units": 0.0, "Revenue": 0.0, "Headcount": 0.0, "Salaries": 0.0, "Marketing": 0.0, "Rent": 0.0}
    n = 0
    for e in entities:
        for m in months:
            c = _DATA[(e, m, scenario)]
            if c is None:
                continue
            n += 1
            units = c["Units"] * (1 + adj.get("units_pct", 0) / 100)
            price = c["Avg Price"] * (1 + adj.get("price_pct", 0) / 100)
            hc = c["Headcount"] + adj.get("headcount_change", 0)
            t["Units"] += units
            t["Revenue"] += units * price
            t["Headcount"] += hc
            t["Salaries"] += hc * COST_PER_HEAD
            t["Marketing"] += c["Marketing"] * (1 + adj.get("marketing_pct", 0) / 100)
            t["Rent"] += c["Rent"]
    months_with_data = n / max(len(entities), 1)
    cos = t["Units"] * UNIT_COST
    opex = t["Salaries"] + t["Marketing"] + t["Rent"]
    oi = t["Revenue"] - cos - opex
    return {"Units": round(t["Units"]), "Avg Price": round(t["Revenue"] / t["Units"], 2) if t["Units"] else 0.0,
            "Revenue": round(t["Revenue"], 2), "Cost of Sales": round(cos, 2),
            "Gross Margin": round(t["Revenue"] - cos, 2),
            "Headcount": round(t["Headcount"] / months_with_data, 1) if months_with_data else 0.0,
            "Salaries": round(t["Salaries"], 2), "Marketing": round(t["Marketing"], 2), "Rent": round(t["Rent"], 2),
            "Total Opex": round(opex, 2), "Operating Income": round(oi, 2),
            "Operating Margin %": round(oi / t["Revenue"] * 100, 2) if t["Revenue"] else 0.0}


@mcp.tool()
def list_dimensions() -> dict:
    """List the EPBCS dimensions and members the user may see."""
    try:
        _, ents = scope_entities(None)
    except AccessDenied as exc:
        return _err(exc)
    return {"application": "EPBCS Demo (synthetic stand-in)", "Entity": ents,
            "Region": {r: [e for e in es if e in ents] for r, es in REGIONS.items()},
            "Account": ACCOUNTS, "Period": MONTHS + ["Q1", "Q2", "Q3", "Q4", "YTD", "FY"], "Year": "FY26",
            "Scenario": SCENARIOS, "Version": {"Plan": "Final", "Forecast": "Working", "Actual": "Final"},
            "actuals_through": MONTHS[ACTUALS_THROUGH - 1], "drivers": DRIVERS}


@mcp.tool()
def get_plan_data(accounts: list[str], scenario: str = "Plan", period: str = "FY",
                  entities: list[str] | None = None) -> dict:
    """Retrieve planning data. accounts e.g. ["Revenue", "Operating Income", "Headcount"];
    scenario Actual, Plan or Forecast; period FY, Q1..Q4, YTD or a month; entities e.g. ["East"] or omit."""
    try:
        user, ents = scope_entities(entities)
        months = _months(period)
        accs = [_acc(a) for a in accounts]
        rows = [{"entity": e, **{a: _totals([e], months, scenario)[a] for a in accs}} for e in ents]
        if len(ents) > 1:
            tot = _totals(ents, months, scenario)
            rows.append({"entity": "Total", **{a: tot[a] for a in accs}})
        log("get_plan_data", user=user, accounts=accs, scenario=scenario, period=period, entities=ents)
        return {"scenario": scenario, "period": period, "rows": rows, "source": "synthetic EPBCS"}
    except (AccessDenied, ValueError) as exc:
        return _err(exc)


@mcp.tool()
def plan_vs_actual(account: str = "Operating Income", period: str = "YTD",
                   entities: list[str] | None = None) -> dict:
    """Actual vs Plan for one account by entity (default YTD = Jan..Sep). Positive variance = above plan."""
    try:
        user, ents = scope_entities(entities)
        acc, months = _acc(account), _months(period)
        rows = []
        for label, g in [(e, [e]) for e in ents] + ([("Total", ents)] if len(ents) > 1 else []):
            a, p = _totals(g, months, "Actual")[acc], _totals(g, months, "Plan")[acc]
            rows.append({"entity": label, "Actual": a, "Plan": p, "Variance": round(a - p, 2),
                         "Variance %": round((a - p) / abs(p) * 100, 2) if p else None})
        log("plan_vs_actual", user=user, account=acc, period=period, entities=ents)
        return {"account": acc, "period": period, "rows": rows, "source": "synthetic EPBCS"}
    except (AccessDenied, ValueError) as exc:
        return _err(exc)


@mcp.tool()
def full_year_outlook(account: str = "Operating Income", entities: list[str] | None = None) -> dict:
    """Full-year outlook: Forecast (actuals Jan-Sep + Working forecast Oct-Dec) vs Plan, with the gap."""
    try:
        user, ents = scope_entities(entities)
        acc = _acc(account)
        rows = []
        for label, g in [(e, [e]) for e in ents] + ([("Total", ents)] if len(ents) > 1 else []):
            f, p = _totals(g, MONTHS, "Forecast")[acc], _totals(g, MONTHS, "Plan")[acc]
            rows.append({"entity": label, "FY Forecast": f, "FY Plan": p, "Gap to Plan": round(f - p, 2),
                         "Q4 Forecast": _totals(g, MONTHS[9:], "Forecast")[acc]})
        log("outlook", user=user, account=acc, entities=ents)
        return {"account": acc, "rows": rows, "note": "Forecast = Working version. Projection, not a submitted plan."}
    except (AccessDenied, ValueError) as exc:
        return _err(exc)


@mcp.tool()
def what_if(units_pct: float = 0, price_pct: float = 0, headcount_change: int = 0, marketing_pct: float = 0,
            scenario: str = "Forecast", period: str = "Q4", entities: list[str] | None = None) -> dict:
    """Driver-based what-if (sandbox, nothing saved): % change in units, price, marketing and an absolute
    headcount change per entity per month. Example: "what if West units grow 5% and we add 2 heads" ->
    units_pct=5, headcount_change=2, entities=["West"]."""
    try:
        user, ents = scope_entities(entities)
        months = _months(period)
        adj = {"units_pct": units_pct, "price_pct": price_pct, "headcount_change": headcount_change,
               "marketing_pct": marketing_pct}
        rows = []
        for label, g in [(e, [e]) for e in ents] + ([("Total", ents)] if len(ents) > 1 else []):
            b, s = _totals(g, months, scenario), _totals(g, months, scenario, adj)
            rows.append({"entity": label, "Revenue (base)": b["Revenue"], "Revenue (what-if)": s["Revenue"],
                         "Operating Income (base)": b["Operating Income"],
                         "Operating Income (what-if)": s["Operating Income"],
                         "Impact": round(s["Operating Income"] - b["Operating Income"], 2)})
        log("what_if", user=user, scenario=scenario, period=period, entities=ents, changes=adj)
        return {"scenario": scenario, "period": period, "assumptions": adj, "rows": rows,
                "note": "Sandbox simulation. Nothing is written to the plan."}
    except (AccessDenied, ValueError) as exc:
        return _err(exc)


@mcp.tool()
def update_forecast(entity: str, driver: str, month: str, value: float) -> dict:
    """WRITE. Update one forecast driver (Units, Avg Price, Headcount, Marketing, Rent) for one entity and one
    open month (Oct, Nov, Dec) in the Working Forecast. Only entities the user is authorised for; actuals and
    the Final Plan are locked. Confirm with the user before calling."""
    try:
        user, ents = scope_entities([entity])
        if len(ents) != 1:
            raise ValueError("Give a single entity, e.g. NewYork.")
        e = ents[0]
        drv = next((x for x in DRIVERS if x.lower() == driver.strip().lower()), None)
        if not drv:
            raise ValueError(f"driver must be one of {', '.join(DRIVERS)}.")
        m = _months(month)
        if len(m) != 1 or MONTHS.index(m[0]) < ACTUALS_THROUGH:
            raise ValueError("Only open forecast months Oct, Nov or Dec can be changed.")
        if APPROVALS.get(e) == "Approved":
            raise ValueError(f"{e} forecast is Approved and locked. Ask the approver to reject it first.")
        cell = _DATA[(e, m[0], "Forecast")]
        old = cell[drv]
        cell[drv] = round(float(value), 2) if drv == "Avg Price" else round(float(value))
        log("update_forecast", user=user, entity=e, driver=drv, month=m[0], old=old, new=cell[drv])
        return {"entity": e, "driver": drv, "month": m[0], "old": old, "new": cell[drv], "version": "Working",
                "fy_operating_income_forecast": _totals([e], MONTHS, "Forecast")["Operating Income"]}
    except (AccessDenied, ValueError) as exc:
        return _err(exc)


@mcp.tool()
def approval_status() -> dict:
    """Approval status of the Q4 forecast planning units for the entities the user may see."""
    try:
        user, ents = scope_entities(None)
        log("approval_status", user=user, entities=ents)
        return {"process": "FY26 Q4 Forecast", "rows": [{"entity": e, "status": APPROVALS[e]} for e in ents]}
    except AccessDenied as exc:
        return _err(exc)


@mcp.tool()
def reset_demo_forecast() -> dict:
    """DEMO. Restore the Working forecast to its original values (exec role only)."""
    try:
        user, ents = scope_entities(None)
        if set(ents) != set(ALL_ENTITIES):
            raise AccessDenied("Only the CFO/exec role can reset the demo forecast.")
        _DATA.clear()
        _DATA.update(copy.deepcopy(_BASE))
        log("reset_forecast", user=user)
        return {"status": "Working forecast restored"}
    except AccessDenied as exc:
        return _err(exc)


app = mcp.streamable_http_app()

if __name__ == "__main__":
    run(mcp)
