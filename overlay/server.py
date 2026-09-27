"""EPM Copilot stand-in MCP server (streamable HTTP + Entra ID).

A drop-in rehearsal of the on-prem Hyperion/Essbase MCP server: same tool
shape, same Exec/East/West security, synthetic data. Point Copilot Studio or
Claude at it to prove the sign-in chain before the real server is published.

Run:  python -m app.server        (reads settings from environment variables)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from app import cube
from app.auth import ROLE_MARKETS, EntraConfig, EntraTokenVerifier

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
audit = logging.getLogger("epm.audit")

PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8000").rstrip("/")
ALLOW_NO_AUTH = os.environ.get("ALLOW_NO_AUTH", "false").lower() == "true"
NO_AUTH_USER = os.environ.get("NO_AUTH_USER", "local-dev")
NO_AUTH_ROLE = os.environ.get("NO_AUTH_ROLE", "east")

# Demo "simulation mode": users listed here may switch their own role at runtime
# with the switch_demo_persona tool. Everyone else keeps the role from USER_ROLES.
DEMO_ADMINS = {u.strip().lower() for u in os.environ.get("DEMO_ADMINS", "").split(",") if u.strip()}
PERSONAS = {
    "exec": "exec", "cfo": "exec", "all": "exec",
    "east": "east", "east manager": "east",
    "west": "west", "west manager": "west",
}
PERSONA_LABELS = {"exec": "CFO (all markets)", "east": "East market manager", "west": "West market manager"}
_persona_overrides: dict[str, str] = {}  # in memory; resets when the server restarts

INSTRUCTIONS = (
    "EPM Copilot demo server over a synthetic Essbase cube (Year, Measures, Product, Market, Scenario). "
    "Call whoami first to learn which markets the signed-in user may see. Never invent numbers; "
    "if a tool refuses a market, tell the user they do not have access."
)


def _build_mcp() -> FastMCP:
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(os.environ.get("ALLOWED_HOSTS")),
        allowed_hosts=[h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()],
    )
    common = dict(
        instructions=INSTRUCTIONS,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )
    if ALLOW_NO_AUTH:
        print("WARNING: ALLOW_NO_AUTH=true - authentication is OFF. Local testing only.", file=sys.stderr)
        return FastMCP("EPM Copilot (stand-in)", **common)

    cfg = EntraConfig.from_env()
    return FastMCP(
        "EPM Copilot (stand-in)",
        token_verifier=EntraTokenVerifier(cfg),
        auth=AuthSettings(
            issuer_url=cfg.authority,
            resource_server_url=f"{PUBLIC_URL}/mcp",
            required_scopes=[cfg.required_scope] if cfg.required_scope else None,
        ),
        **common,
    )


mcp = _build_mcp()


# --------------------------------------------------------------------------- #
# Identity & security helpers
# --------------------------------------------------------------------------- #
class AccessDenied(Exception):
    pass


def _identity() -> tuple[str, str | None]:
    if ALLOW_NO_AUTH:
        return NO_AUTH_USER, NO_AUTH_ROLE
    tok = get_access_token()
    if tok is None:
        raise AccessDenied("Not signed in.")
    user = getattr(tok, "user", tok.client_id)
    override = _persona_overrides.get(str(user).lower())
    if override:
        return user, override
    return user, getattr(tok, "role", None)


def _is_simulated(user: str) -> bool:
    return str(user).lower() in _persona_overrides


def _allowed_markets() -> tuple[str, list[str]]:
    user, role = _identity()
    markets = ROLE_MARKETS.get(role or "")
    if not markets:
        _audit("denied", user=user, reason="user has no EPM role")
        raise AccessDenied(f"{user} is signed in but has no access to this application.")
    return user, markets


def _audit(event: str, **fields) -> None:
    audit.info(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **fields}))


def _scope_markets(requested: list[str] | None) -> tuple[str, list[str]]:
    """Layer 1 of the two-layer check: refuse markets outside the user's filter."""
    user, allowed = _allowed_markets()
    if not requested:
        return user, allowed
    wanted = [cube.normalize_market(m) for m in requested]
    denied = [m for m in wanted if m not in allowed]
    if denied:
        _audit("denied", user=user, requested=wanted, denied=denied)
        raise AccessDenied(
            f"Access denied: {user} is not authorised for market(s) {', '.join(denied)}. "
            f"Authorised markets: {', '.join(allowed)}."
        )
    return user, wanted


def _error(exc: Exception) -> dict:
    return {"error": str(exc)}


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@mcp.tool()
def whoami() -> dict:
    """Show the signed-in user, their EPM security role and the markets they are allowed to see."""
    try:
        user, role = _identity()
        markets = ROLE_MARKETS.get(role or "", [])
        _audit("whoami", user=user, role=role, simulated=_is_simulated(user))
        out = {"user": user, "role": role or "none", "authorised_markets": markets}
        if _is_simulated(user):
            out["simulation_mode"] = f"Demo persona active: {PERSONA_LABELS.get(role, role)}"
        return out
    except AccessDenied as exc:
        return _error(exc)


@mcp.tool()
def switch_demo_persona(persona: str) -> dict:
    """DEMO ONLY. Switch the signed-in presenter to a simulated persona to show row-level security.

    persona: "cfo" (all markets), "east" (East manager), "west" (West manager) or "reset"
    (back to the user's real role). Only users listed in DEMO_ADMINS may switch; security for
    everyone else is unchanged. Call this when the user says e.g. "switch to West manager".
    """
    try:
        user, _ = _identity()
    except AccessDenied as exc:
        return _error(exc)
    key = str(user).lower()
    if key not in DEMO_ADMINS:
        _audit("denied", user=user, reason="persona switch not allowed")
        return {"error": f"{user} is not allowed to switch personas. Simulation mode is limited to demo presenters."}
    p = persona.strip().lower()
    if p in ("reset", "real", "me", "off"):
        _persona_overrides.pop(key, None)
        _audit("persona", user=user, persona="reset")
        return {"user": user, "simulation_mode": "off", "message": "Back to your real role."}
    role = PERSONAS.get(p)
    if not role:
        return {"error": f"Unknown persona '{persona}'. Use cfo, east, west or reset."}
    _persona_overrides[key] = role
    _audit("persona", user=user, persona=role)
    return {"user": user, "simulation_mode": f"Demo persona active: {PERSONA_LABELS[role]}",
            "role": role, "authorised_markets": ROLE_MARKETS[role]}


@mcp.tool()
def list_dimensions() -> dict:
    """List the cube dimensions and their members (Year, Measures, Product, Market, Scenario)."""
    try:
        _, allowed = _allowed_markets()
    except AccessDenied as exc:
        return _error(exc)
    dims = dict(cube.DIMENSIONS)
    dims["Market"] = ["Market"] + allowed  # only show members the user may see
    return {"application": "Demo.Basic (synthetic stand-in)", "dimensions": dims}


@mcp.tool()
def query_data(
    measures: list[str],
    period: str = "Year",
    scenario: str = "Actual",
    markets: list[str] | None = None,
    products: list[str] | None = None,
) -> dict:
    """Retrieve figures from the cube.

    measures: e.g. ["Sales", "Margin", "Profit", "Margin %"]
    period: "Year", "Qtr1".."Qtr4" or a month "Jan".."Dec"
    scenario: "Actual" or "Budget"
    markets: e.g. ["East"]; omit for all markets the user may see
    products: e.g. ["Colas"]; omit for all products
    Returns one row per market x product.
    """
    try:
        user, mkts = _scope_markets(markets)
        months = cube.resolve_months(period)
        prod_codes = [c for p in (products or ["Product"]) for c in cube.resolve_product(p)]
        rows = []
        for market in mkts:
            for code in prod_codes:
                row = {"market": market, "product": f"{code} {cube.PRODUCTS[code]}"}
                for m in measures:
                    row[m] = cube.value(m, months, [market], [code], scenario)
                rows.append(row)
        _audit("query", user=user, measures=measures, period=period, scenario=scenario, markets=mkts)
        return {"period": period, "scenario": scenario, "rows": rows, "source": "synthetic Demo.Basic"}
    except (AccessDenied, ValueError) as exc:
        return _error(exc)


@mcp.tool()
def variance_actual_vs_budget(
    measure: str = "Sales",
    period: str = "Year",
    by: str = "Market",
    markets: list[str] | None = None,
) -> dict:
    """Actual vs Budget variance for one measure, broken down by Market or Product.

    by: "Market" or "Product". Positive variance = Actual above Budget.
    """
    try:
        user, mkts = _scope_markets(markets)
        months = cube.resolve_months(period)
        if by.lower() == "market":
            groups = [(m, [m], list(cube.PRODUCTS)) for m in mkts]
        elif by.lower() == "product":
            groups = [(f"{c} {n}", mkts, [c]) for c, n in cube.PRODUCTS.items()]
        else:
            raise ValueError("by must be 'Market' or 'Product'.")
        rows = []
        for label, gm, gp in groups:
            act = cube.value(measure, months, gm, gp, "Actual")
            bud = cube.value(measure, months, gm, gp, "Budget")
            var = round(act - bud, 2)
            rows.append({by: label, "Actual": act, "Budget": bud, "Variance": var,
                         "Variance %": round(var / bud * 100, 2) if bud else None})
        _audit("variance", user=user, measure=measure, period=period, by=by, markets=mkts)
        return {"measure": measure, "period": period, "rows": rows, "source": "synthetic Demo.Basic"}
    except (AccessDenied, ValueError) as exc:
        return _error(exc)


# --------------------------------------------------------------------------- #
# Planning skill: rolling forecast, gap to budget, what-if
# --------------------------------------------------------------------------- #
_BASE = ["Sales", "COGS", "Marketing", "Payroll"]


def _derive(t: dict[str, float]) -> dict[str, float]:
    margin = t["Sales"] - t["COGS"]
    exp = t["Marketing"] + t["Payroll"]
    return {"Sales": round(t["Sales"], 2), "Margin": round(margin, 2), "Total Expenses": round(exp, 2),
            "Profit": round(margin - exp, 2),
            "Margin %": round(margin / t["Sales"] * 100, 2) if t["Sales"] else 0.0}


def _monthly(market: str, scenario: str) -> dict[str, list[float]]:
    prods = list(cube.PRODUCTS)
    return {k: [cube.value(k, [m], [market], prods, scenario) for m in cube.MONTHS] for k in _BASE}


def _project(series: list[float], n_actual: int, method: str) -> list[float]:
    """Project months n_actual..11 from the first n_actual actual months."""
    hist = series[:n_actual]
    remaining = 12 - n_actual
    if method == "run_rate":
        avg = sum(hist[-3:]) / len(hist[-3:])
        return [avg] * remaining
    # linear trend (least squares) on the actual months
    n = len(hist)
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(hist) / n
    den = sum((x - mx) ** 2 for x in xs) or 1.0
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, hist)) / den
    return [max(0.0, my + slope * (x - mx)) for x in range(n, 12)]


@mcp.tool()
def forecast(
    measure: str = "Sales",
    actuals_through: str = "Sep",
    method: str = "budget_phasing",
    markets: list[str] | None = None,
) -> dict:
    """PLANNING. Rolling full-year forecast = actuals to date + projection of the remaining months.

    measure: Sales, Margin, Total Expenses, Profit or Margin %
    actuals_through: last closed month, e.g. "Jun" or "Sep" (default Sep = Q3 closed)
    method: "budget_phasing" (default: remaining budget months scaled by YTD actual vs budget
            performance, keeps seasonality), "run_rate" (average of the last 3 actual months)
            or "trend" (linear trend of actual months; ignores seasonality)
    markets: omit for all markets the user may see
    Returns per market: YTD actual, remaining forecast, full-year forecast, full-year budget,
    gap to budget and the uplift needed in the remaining months to still hit budget.
    """
    try:
        user, mkts = _scope_markets(markets)
        months = cube.resolve_months(actuals_through)
        if len(months) != 1:
            raise ValueError("actuals_through must be a single month, e.g. 'Sep'.")
        n = cube.MONTHS.index(months[0]) + 1
        if n >= 12:
            raise ValueError("All months are actuals; pick an earlier month to forecast the rest of the year.")
        if method not in ("trend", "run_rate", "budget_phasing"):
            raise ValueError("method must be trend, run_rate or budget_phasing.")
        if measure.strip().lower() not in {m.lower() for m in ("Sales", "Margin", "Total Expenses", "Profit", "Margin %")}:
            raise ValueError("measure must be Sales, Margin, Total Expenses, Profit or Margin %.")
        key = next(m for m in ("Sales", "Margin", "Total Expenses", "Profit", "Margin %") if m.lower() == measure.strip().lower())

        rows, tot = [], {"ytd": {k: 0.0 for k in _BASE}, "rem": {k: 0.0 for k in _BASE},
                         "bud": {k: 0.0 for k in _BASE}, "bud_rem": {k: 0.0 for k in _BASE}}
        for mk in mkts:
            act, bud = _monthly(mk, "Actual"), _monthly(mk, "Budget")
            ytd = {k: sum(act[k][:n]) for k in _BASE}
            if method == "budget_phasing":
                rem = {k: (sum(bud[k][n:]) * (ytd[k] / sum(bud[k][:n]) if sum(bud[k][:n]) else 1.0)) for k in _BASE}
            else:
                rem = {k: sum(_project(act[k], n, method)) for k in _BASE}
            fy_bud = {k: sum(bud[k]) for k in _BASE}
            bud_rem = {k: sum(bud[k][n:]) for k in _BASE}
            for k in _BASE:
                tot["ytd"][k] += ytd[k]; tot["rem"][k] += rem[k]; tot["bud"][k] += fy_bud[k]; tot["bud_rem"][k] += bud_rem[k]
            rows.append(_forecast_row(mk, key, ytd, rem, fy_bud))
        if len(mkts) > 1:
            rows.append(_forecast_row("Total", key, tot["ytd"], tot["rem"], tot["bud"]))
        _audit("forecast", user=user, measure=key, actuals_through=months[0], method=method, markets=mkts)
        return {"measure": key, "actuals_through": months[0], "method": method,
                "remaining_months": cube.MONTHS[n:], "rows": rows, "source": "synthetic Demo.Basic",
                "note": "Forecast is a projection, not a submitted plan. Nothing is written to the cube."}
    except (AccessDenied, ValueError) as exc:
        return _error(exc)


def _forecast_row(label: str, key: str, ytd: dict, rem: dict, fy_bud: dict) -> dict:
    fy = {k: ytd[k] + rem[k] for k in _BASE}
    a, r, f, b = _derive(ytd), _derive(rem), _derive(fy), _derive(fy_bud)
    row = {"market": label, "YTD Actual": a[key], "Remaining Forecast": r[key],
           "Full-Year Forecast": f[key], "Full-Year Budget": b[key],
           "Gap to Budget": round(f[key] - b[key], 2)}
    if key != "Margin %":
        needed = b[key] - a[key]
        row["Needed in Remaining Months"] = round(needed, 2)
        row["Change Needed vs Forecast %"] = round((needed / r[key] - 1) * 100, 2) if r[key] else None
    return row


@mcp.tool()
def what_if(
    sales_change_pct: float = 0.0,
    cogs_change_pct: float = 0.0,
    marketing_change_pct: float = 0.0,
    payroll_change_pct: float = 0.0,
    period: str = "Year",
    scenario: str = "Budget",
    markets: list[str] | None = None,
) -> dict:
    """PLANNING. What-if simulation: apply % changes to Sales, COGS, Marketing and Payroll and see
    the impact on Margin, Profit and Margin %.

    Example: "what if West sales grow 5% and marketing rises 10%" ->
    sales_change_pct=5, marketing_change_pct=10, markets=["West"].
    COGS moves with Sales volume, so a sales change also scales COGS unless cogs_change_pct
    overrides the unit cost change on top of that.
    scenario: base to simulate on, "Budget" (default) or "Actual"
    Nothing is written to the cube; results are a sandbox simulation.
    """
    try:
        user, mkts = _scope_markets(markets)
        months = cube.resolve_months(period)
        if scenario not in cube.SCENARIOS:
            raise ValueError("scenario must be Actual or Budget.")
        f = {"Sales": 1 + sales_change_pct / 100,
             "COGS": (1 + sales_change_pct / 100) * (1 + cogs_change_pct / 100),
             "Marketing": 1 + marketing_change_pct / 100,
             "Payroll": 1 + payroll_change_pct / 100}
        rows = []
        groups = [(m, [m]) for m in mkts] + ([("Total", mkts)] if len(mkts) > 1 else [])
        for label, gm in groups:
            base = {k: cube.value(k, months, gm, list(cube.PRODUCTS), scenario) for k in _BASE}
            sim = {k: base[k] * f[k] for k in _BASE}
            b, s = _derive(base), _derive(sim)
            rows.append({"market": label,
                         **{f"{k} (base)": b[k] for k in ("Sales", "Profit", "Margin %")},
                         **{f"{k} (what-if)": s[k] for k in ("Sales", "Profit", "Margin %")},
                         "Profit Impact": round(s["Profit"] - b["Profit"], 2)})
        _audit("what_if", user=user, period=period, scenario=scenario, markets=mkts,
               changes={"sales": sales_change_pct, "cogs": cogs_change_pct,
                        "marketing": marketing_change_pct, "payroll": payroll_change_pct})
        return {"period": period, "base_scenario": scenario,
                "assumptions_pct": {"Sales": sales_change_pct, "COGS unit cost": cogs_change_pct,
                                    "Marketing": marketing_change_pct, "Payroll": payroll_change_pct},
                "rows": rows, "source": "synthetic Demo.Basic",
                "note": "Sandbox simulation. Nothing is written to the cube."}
    except (AccessDenied, ValueError) as exc:
        return _error(exc)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "auth": "off" if ALLOW_NO_AUTH else "entra"})


app = mcp.streamable_http_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), proxy_headers=True)
