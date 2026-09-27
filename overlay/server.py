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


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "auth": "off" if ALLOW_NO_AUTH else "entra"})


app = mcp.streamable_http_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), proxy_headers=True)
