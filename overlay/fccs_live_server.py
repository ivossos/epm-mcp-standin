"""Entra-protected live FCCS reader; demo tools are deliberately absent."""
import os
from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from app.auth import EntraConfig, EntraTokenVerifier
from app.oracle_fccs import OracleFCCS, OracleError

if os.environ.get("ALLOW_NO_AUTH", "false").lower() == "true":
    raise RuntimeError("Live FCCS requires Entra authentication.")
cfg = EntraConfig.from_env()
public_url = os.environ["PUBLIC_URL"].rstrip("/")
mcp = FastMCP("EPM Copilot - FCCS", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
    stateless_http=True, json_response=True,
    instructions="Read real FCCS data only. Ask for the year and complete POV. Never infer USD or totals.",
    token_verifier=EntraTokenVerifier(cfg),
    auth=AuthSettings(issuer_url=cfg.authority, resource_server_url=public_url + "/mcp",
                      required_scopes=[cfg.required_scope] if cfg.required_scope else None),
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
        allowed_hosts=[public_url.split("//", 1)[1]]))
oracle = OracleFCCS()


def user():
    token = get_access_token()
    if token is None or not getattr(token, "user", None):
        raise OracleError("An authenticated user token is required.")
    oracle.authorize(token.user)
    return token.user


@mcp.tool()
def whoami() -> dict:
    """Show the authenticated identity and its explicitly bound Oracle account."""
    try:
        identity = user()
        return {"user": identity, "oracle_user": oracle.authorize(identity),
                "source": "Oracle FCCS REST", "simulation_mode": "off",
                "access": "Oracle account permissions; no demo persona switching"}
    except OracleError as exc:
        return {"error": str(exc)}


@mcp.tool()
def list_dimensions() -> dict:
    """Read actual NICSFCC cube dimensions. No demo members."""
    try:
        return oracle.dimensions(user())
    except OracleError as exc:
        return {"error": str(exc)}


@mcp.tool()
def get_financials(accounts: list[str], period: str, year: str,
                   entities: list[str], pov: dict[str, str],
                   view: str = "YTD", scenario: str = "Actual") -> dict:
    """Read Oracle FCCS cells. Supply actual members and every remaining dimension in pov.
    Include Currency, Consolidation, Movement, Data Source, Intercompany and custom dimensions
    as applicable to the cube. Oracle exportdataslice returns cells with read-write access.
    Missing or restricted cells must never be reported as zero. No calculated group totals.
    """
    try:
        return oracle.financials(user(), accounts, period, view, scenario, entities, year, pov)
    except OracleError as exc:
        return {"error": str(exc)}


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    return JSONResponse({"status": "ok", "auth": "entra", "data_mode": "oracle",
                         "oracle_connectivity": "not_checked"})


app = mcp.streamable_http_app()
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), proxy_headers=True)
