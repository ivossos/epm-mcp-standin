"""EPM Copilot - FCCS stand-in MCP server (synthetic Financial Consolidation and Close data).

Entities NewYork, Boston (East) and LosAngeles, Seattle (West), FY2026, Actual closed
through September. Security: exec sees the group, east/west only their entities.
Run:  python -m app.fccs_server
"""

from __future__ import annotations

import random

from app.common import (ALL_ENTITIES, REGIONS, AccessDenied, build_mcp, log, run,
                        scope_entities)

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
CLOSED_THROUGH = 9  # Sep is the current close period
SCENARIOS = ["Actual", "Budget", "Prior Year"]
PL_BASE = ["Revenue", "COGS", "Opex", "D&A", "Interest", "Tax"]
PL_DERIVED = ["Gross Margin", "EBITDA", "EBIT", "Net Income", "Gross Margin %"]
BS = ["Cash", "Accounts Receivable", "Inventory", "IC Receivable", "Accounts Payable", "IC Payable", "Equity"]
ACCOUNTS = PL_BASE + PL_DERIVED + BS
SIZE = {"NewYork": 1.4, "Boston": 0.8, "LosAngeles": 1.2, "Seattle": 0.7}
IC_PAIRS = [("NewYork", "LosAngeles"), ("Boston", "Seattle"), ("NewYork", "Boston")]

INSTRUCTIONS = ("EPM Copilot for Oracle FCCS (synthetic demo data). Entities: NewYork, Boston (East), "
                "LosAngeles, Seattle (West); FY2026; Actual closed through Sep. Call whoami first. "
                "Never invent numbers; if a tool refuses an entity, tell the user they lack access.")


def _build():
    rng = random.Random(20261002)
    data = {}
    for e in ALL_ENTITIES:
        s = SIZE[e]
        for mi, m in enumerate(MONTHS):
            season = 1 + 0.12 * (mi in (10, 11)) - 0.05 * (mi in (0, 1))
            base = 1_000_000 * s * season
            for scen, drift in (("Actual", rng.uniform(0.94, 1.08)), ("Budget", 1.0),
                                ("Prior Year", rng.uniform(0.88, 0.97))):
                rev = round(base * drift * rng.uniform(0.97, 1.03))
                data[(e, m, scen)] = {
                    "Revenue": rev,
                    "COGS": round(rev * (rng.uniform(0.55, 0.61) if scen != "Budget" else 0.57)),
                    "Opex": round(rev * (rng.uniform(0.19, 0.24) if scen != "Budget" else 0.21)),
                    "D&A": round(45_000 * s),
                    "Interest": round(12_000 * s),
                    "Tax": 0,
                }
                d = data[(e, m, scen)]
                ebt = d["Revenue"] - d["COGS"] - d["Opex"] - d["D&A"] - d["Interest"]
                d["Tax"] = round(max(ebt, 0) * 0.25)
    ic = {}
    for a, b in IC_PAIRS:
        for mi, m in enumerate(MONTHS[:CLOSED_THROUGH]):
            amt = round(rng.uniform(80_000, 220_000), -2)
            diff = 0
            if m == "Sep" and (a, b) != ("NewYork", "Boston"):
                diff = round(rng.uniform(3_000, 18_000), -2)
            ic[(a, b, m)] = (amt, amt - diff)  # (receivable at a, payable at b)
    return data, ic


_DATA, _IC = _build()

STATUS = {e: {m: ("Locked" if i < CLOSED_THROUGH - 1 else None) for i, m in enumerate(MONTHS)} for e in ALL_ENTITIES}
STATUS["NewYork"]["Sep"] = "OK"
STATUS["Boston"]["Sep"] = "Impacted"
STATUS["LosAngeles"]["Sep"] = "OK"
STATUS["Seattle"]["Sep"] = "Not Started"

JOURNALS = [
    {"id": "JE-0912", "entity": "NewYork", "period": "Sep", "status": "Posted", "amount": 42_000,
     "description": "Accrued bonus true-up"},
    {"id": "JE-0915", "entity": "Boston", "period": "Sep", "status": "Submitted", "amount": 18_500,
     "description": "Inventory reserve adjustment"},
    {"id": "JE-0917", "entity": "Boston", "period": "Sep", "status": "Working", "amount": 7_200,
     "description": "Reclass freight to COGS"},
    {"id": "JE-0921", "entity": "LosAngeles", "period": "Sep", "status": "Approved", "amount": 25_000,
     "description": "Lease amortisation"},
    {"id": "JE-0923", "entity": "Seattle", "period": "Sep", "status": "Working", "amount": 11_800,
     "description": "IC recharge - shared services"},
    {"id": "JE-0811", "entity": "Seattle", "period": "Aug", "status": "Posted", "amount": 9_400,
     "description": "Prepaid insurance"},
]

mcp = build_mcp("EPM Copilot - FCCS (stand-in)", INSTRUCTIONS)


def _months(period: str, view: str) -> list[str]:
    p = period.strip()[:3].title()
    if p not in MONTHS:
        raise ValueError("period must be a month Jan..Dec.")
    i = MONTHS.index(p)
    return MONTHS[: i + 1] if view.lower() == "ytd" else [p]


def _bs(e: str, month: str) -> dict:
    """Simple balance sheet built from cumulative P&L so it ties out."""
    i = MONTHS.index(month)
    ytd = [_DATA[(e, m, "Actual")] for m in MONTHS[: i + 1]]
    rev = sum(d["Revenue"] for d in ytd)
    ni = sum(d["Revenue"] - d["COGS"] - d["Opex"] - d["D&A"] - d["Interest"] - d["Tax"] for d in ytd)
    icr = sum(v[0] for (a, b, m), v in _IC.items() if a == e and m == month)
    icp = sum(v[1] for (a, b, m), v in _IC.items() if b == e and m == month)
    ar, inv, ap = round(rev / (i + 1) * 1.4), round(SIZE[e] * 650_000), round(rev / (i + 1) * 0.9)
    equity = round(SIZE[e] * 3_000_000 + ni)
    cash = equity + ap + icp - ar - inv - icr
    return {"Cash": cash, "Accounts Receivable": ar, "Inventory": inv, "IC Receivable": icr,
            "Accounts Payable": ap, "IC Payable": icp, "Equity": equity}


def _value(account: str, entities: list[str], months: list[str], scenario: str) -> float:
    acc = next((a for a in ACCOUNTS if a.lower() == account.strip().lower()), None)
    if not acc:
        raise ValueError(f"Unknown account '{account}'. Use: {', '.join(ACCOUNTS)}.")
    if scenario not in SCENARIOS:
        raise ValueError("scenario must be Actual, Budget or Prior Year.")
    if acc in BS:
        if scenario != "Actual":
            raise ValueError("Balance sheet accounts are available for Actual only.")
        return float(sum(_bs(e, months[-1])[acc] for e in entities))
    t = {k: sum(_DATA[(e, m, scenario)][k] for e in entities for m in months) for k in PL_BASE}
    gm = t["Revenue"] - t["COGS"]
    ebitda = gm - t["Opex"]
    ebit = ebitda - t["D&A"]
    vals = {**t, "Gross Margin": gm, "EBITDA": ebitda, "EBIT": ebit,
            "Net Income": ebit - t["Interest"] - t["Tax"],
            "Gross Margin %": round(gm / t["Revenue"] * 100, 2) if t["Revenue"] else 0.0}
    return float(vals[acc])


def _err(exc: Exception) -> dict:
    return {"error": str(exc)}


@mcp.tool()
def list_dimensions() -> dict:
    """List the FCCS dimensions and members the user may see (Entity, Account, Period, Scenario, View)."""
    try:
        _, ents = scope_entities(None)
    except AccessDenied as exc:
        return _err(exc)
    return {"application": "FCCS Demo (synthetic stand-in)", "Entity": ents,
            "Region": {r: [e for e in es if e in ents] for r, es in REGIONS.items()},
            "Account": ACCOUNTS, "Period": MONTHS, "Year": "FY26", "Scenario": SCENARIOS,
            "View": ["Periodic", "YTD"], "Currency": "USD", "current_close_period": MONTHS[CLOSED_THROUGH - 1]}


@mcp.tool()
def get_financials(accounts: list[str], period: str = "Sep", view: str = "YTD", scenario: str = "Actual",
                   entities: list[str] | None = None) -> dict:
    """Retrieve consolidated financial data.

    accounts: e.g. ["Revenue", "EBITDA", "Net Income"] or balance sheet ["Cash", "IC Receivable"]
    period: Jan..Dec; view: "YTD" (default) or "Periodic"; scenario: Actual, Budget, Prior Year
    entities: e.g. ["NewYork"], ["East"] or ["Total"]; omit for every entity the user may see.
    Returns one row per entity plus a total when more than one entity is returned.
    """
    try:
        user, ents = scope_entities(entities)
        months = _months(period, view)
        rows = [{"entity": e, **{a: _value(a, [e], months, scenario) for a in accounts}} for e in ents]
        if len(ents) > 1:
            label = "Total Group" if set(ents) == set(ALL_ENTITIES) else "Total (authorised entities)"
            rows.append({"entity": label, **{a: _value(a, ents, months, scenario) for a in accounts}})
        log("get_financials", user=user, accounts=accounts, period=period, view=view, scenario=scenario, entities=ents)
        return {"period": period, "view": view, "scenario": scenario, "currency": "USD", "rows": rows,
                "source": "synthetic FCCS"}
    except (AccessDenied, ValueError) as exc:
        return _err(exc)


@mcp.tool()
def variance_analysis(account: str = "Net Income", period: str = "Sep", view: str = "YTD",
                      compare_to: str = "Budget", entities: list[str] | None = None) -> dict:
    """Actual vs Budget or Actual vs Prior Year for one account, by entity. Positive = Actual above comparison."""
    try:
        user, ents = scope_entities(entities)
        months = _months(period, view)
        rows = []
        groups = [(e, [e]) for e in ents] + ([("Total", ents)] if len(ents) > 1 else [])
        for label, g in groups:
            act, cmp_ = _value(account, g, months, "Actual"), _value(account, g, months, compare_to)
            rows.append({"entity": label, "Actual": act, compare_to: cmp_, "Variance": round(act - cmp_, 2),
                         "Variance %": round((act - cmp_) / abs(cmp_) * 100, 2) if cmp_ else None})
        log("variance", user=user, account=account, period=period, compare_to=compare_to, entities=ents)
        return {"account": account, "period": period, "view": view, "rows": rows, "source": "synthetic FCCS"}
    except (AccessDenied, ValueError) as exc:
        return _err(exc)


@mcp.tool()
def consolidation_status(period: str = "Sep") -> dict:
    """Consolidation / calculation status per entity for a period (Locked, OK, Impacted, Not Started)."""
    try:
        user, ents = scope_entities(None)
        p = _months(period, "Periodic")[0]
        rows = [{"entity": e, "status": STATUS[e][p] or "No Data"} for e in ents]
        log("consolidation_status", user=user, period=p, entities=ents)
        return {"period": p, "rows": rows,
                "legend": {"Locked": "closed and locked", "OK": "consolidated, no changes since",
                           "Impacted": "data changed after the last consolidation - re-run needed",
                           "Not Started": "no consolidation run yet"}}
    except (AccessDenied, ValueError) as exc:
        return _err(exc)


@mcp.tool()
def intercompany_matching(period: str = "Sep", tolerance: float = 1000) -> dict:
    """Intercompany matching report: receivable vs partner payable for each IC pair.
    Pairs with a partner outside the user's access show the partner side as restricted."""
    try:
        user, ents = scope_entities(None)
        p = _months(period, "Periodic")[0]
        rows = []
        for a, b in IC_PAIRS:
            if a not in ents and b not in ents:
                continue
            rec, pay = _IC.get((a, b, p), (0, 0))
            full = a in ents and b in ents
            rows.append({
                "entity": a, "partner": b,
                "IC Receivable (entity)": rec if a in ents else "restricted",
                "IC Payable (partner)": pay if b in ents else "restricted",
                "difference": round(rec - pay, 2) if full else "restricted",
                "status": ("Matched" if abs(rec - pay) <= tolerance else "Mismatch") if full else "Partner outside your access",
            })
        log("ic_matching", user=user, period=p, entities=ents)
        return {"period": p, "tolerance": tolerance, "rows": rows, "source": "synthetic FCCS"}
    except (AccessDenied, ValueError) as exc:
        return _err(exc)


@mcp.tool()
def list_journals(period: str = "Sep", status: str | None = None) -> dict:
    """List journals for a period, optionally filtered by status (Working, Submitted, Approved, Posted)."""
    try:
        user, ents = scope_entities(None)
        p = _months(period, "Periodic")[0]
        rows = [j for j in JOURNALS if j["entity"] in ents and j["period"] == p
                and (not status or j["status"].lower() == status.lower())]
        log("list_journals", user=user, period=p, status=status, entities=ents)
        return {"period": p, "journals": rows, "count": len(rows)}
    except (AccessDenied, ValueError) as exc:
        return _err(exc)


@mcp.tool()
def close_readiness(period: str = "Sep") -> dict:
    """Close readiness summary for the period: consolidation status, IC mismatches and pending journals,
    with the open items that block closing for the entities the user may see."""
    try:
        user, ents = scope_entities(None)
        p = _months(period, "Periodic")[0]
        blockers = []
        for e in ents:
            st = STATUS[e][p] or "No Data"
            if st in ("Impacted", "Not Started", "No Data"):
                blockers.append(f"{e}: consolidation {st}")
        for a, b in IC_PAIRS:
            if a in ents and b in ents:
                rec, pay = _IC.get((a, b, p), (0, 0))
                if abs(rec - pay) > 1000:
                    blockers.append(f"IC mismatch {a}/{b}: {rec - pay:,.0f}")
        pending = [j for j in JOURNALS if j["entity"] in ents and j["period"] == p and j["status"] in ("Working", "Submitted")]
        blockers += [f"Journal {j['id']} ({j['entity']}) is {j['status']}" for j in pending]
        ready = sum(1 for e in ents if STATUS[e][p] in ("OK", "Locked"))
        log("close_readiness", user=user, period=p, entities=ents)
        return {"period": p, "entities_ready": f"{ready}/{len(ents)}",
                "ready_to_close": not blockers, "open_items": blockers}
    except (AccessDenied, ValueError) as exc:
        return _err(exc)


app = mcp.streamable_http_app()

if __name__ == "__main__":
    run(mcp)
