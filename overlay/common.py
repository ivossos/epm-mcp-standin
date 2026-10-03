"""Shared plumbing for the EPM Copilot stand-in MCP servers (FCCS, EPBCS).

Same security model as the Essbase stand-in: Entra ID bearer tokens, a role per
user (USER_ROLES / GROUP_ROLES), and a presenter-only persona switch.
Roles map to entities: exec = all, east = East entities, west = West entities.
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

from app.auth import EntraConfig, EntraTokenVerifier

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
audit = logging.getLogger("epm.audit")

PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8000").rstrip("/")
ALLOW_NO_AUTH = os.environ.get("ALLOW_NO_AUTH", "false").lower() == "true"
NO_AUTH_USER = os.environ.get("NO_AUTH_USER", "local-dev")
NO_AUTH_ROLE = os.environ.get("NO_AUTH_ROLE", "exec")
DEMO_ADMINS = {u.strip().lower() for u in os.environ.get("DEMO_ADMINS", "").split(",") if u.strip()}

REGIONS = {"East": ["NewYork", "Boston"], "West": ["LosAngeles", "Seattle"]}
ALL_ENTITIES = REGIONS["East"] + REGIONS["West"]
ROLE_ENTITIES = {"exec": ALL_ENTITIES, "east": REGIONS["East"], "west": REGIONS["West"]}
ROLE_LABELS = {"exec": "CFO / Group (all entities)", "east": "East region manager", "west": "West region manager"}
PERSONAS = {"exec": "exec", "cfo": "exec", "all": "exec", "east": "east", "east manager": "east",
            "west": "west", "west manager": "west"}
_persona_overrides: dict[str, str] = {}


class AccessDenied(Exception):
    pass


def build_mcp(name: str, instructions: str) -> FastMCP:
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(os.environ.get("ALLOWED_HOSTS")),
        allowed_hosts=[h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()],
    )
    common = dict(instructions=instructions, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
                  stateless_http=True, json_response=True, transport_security=security)
    if ALLOW_NO_AUTH:
        print("WARNING: ALLOW_NO_AUTH=true - authentication is OFF. Local testing only.", file=sys.stderr)
        mcp = FastMCP(name, **common)
    else:
        cfg = EntraConfig.from_env()
        mcp = FastMCP(name, token_verifier=EntraTokenVerifier(cfg),
                      auth=AuthSettings(issuer_url=cfg.authority, resource_server_url=f"{PUBLIC_URL}/mcp",
                                        required_scopes=[cfg.required_scope] if cfg.required_scope else None),
                      **common)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "app": name, "auth": "off" if ALLOW_NO_AUTH else "entra"})

    @mcp.tool()
    def whoami() -> dict:
        """Show the signed-in user, their security role and the entities they are allowed to see."""
        try:
            user, role = identity()
        except AccessDenied as exc:
            return {"error": str(exc)}
        out = {"user": user, "role": role or "none", "authorised_entities": ROLE_ENTITIES.get(role or "", [])}
        if str(user).lower() in _persona_overrides:
            out["simulation_mode"] = f"Demo persona active: {ROLE_LABELS.get(role, role)}"
        log("whoami", user=user, role=role)
        return out

    @mcp.tool()
    def switch_demo_persona(persona: str) -> dict:
        """DEMO ONLY. Switch the presenter to "cfo", "east", "west" or "reset" to show entity security.
        Only users listed in DEMO_ADMINS may switch."""
        try:
            user, _ = identity()
        except AccessDenied as exc:
            return {"error": str(exc)}
        key = str(user).lower()
        if key not in DEMO_ADMINS:
            log("denied", user=user, reason="persona switch not allowed")
            return {"error": f"{user} is not allowed to switch personas."}
        p = persona.strip().lower()
        if p in ("reset", "real", "me", "off"):
            _persona_overrides.pop(key, None)
            return {"user": user, "simulation_mode": "off"}
        role = PERSONAS.get(p)
        if not role:
            return {"error": "Unknown persona. Use cfo, east, west or reset."}
        _persona_overrides[key] = role
        log("persona", user=user, persona=role)
        return {"user": user, "simulation_mode": f"Demo persona active: {ROLE_LABELS[role]}",
                "authorised_entities": ROLE_ENTITIES[role]}

    return mcp


def identity() -> tuple[str, str | None]:
    if ALLOW_NO_AUTH:
        return NO_AUTH_USER, NO_AUTH_ROLE
    tok = get_access_token()
    if tok is None:
        raise AccessDenied("Not signed in.")
    user = getattr(tok, "user", tok.client_id)
    return user, _persona_overrides.get(str(user).lower()) or getattr(tok, "role", None)


def log(event: str, **fields) -> None:
    audit.info(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **fields}))


def normalize_entity(name: str) -> list[str]:
    """Accepts an entity, a region (East/West) or Total/Group."""
    n = name.strip().lower().replace(" ", "").replace("_", "")
    if n in ("total", "group", "all", "consolidated", "totalgroup"):
        return list(ALL_ENTITIES)
    for region, ents in REGIONS.items():
        if n == region.lower():
            return list(ents)
    for e in ALL_ENTITIES:
        if n in (e.lower(), "e" + e.lower()):
            return [e]
    raise ValueError(f"Unknown entity '{name}'. Use one of: {', '.join(ALL_ENTITIES)}, East, West or Total.")


def scope_entities(requested: list[str] | None) -> tuple[str, list[str]]:
    """Refuse any entity outside the user's role; default = all the user may see."""
    user, role = identity()
    allowed = ROLE_ENTITIES.get(role or "")
    if not allowed:
        log("denied", user=user, reason="no role")
        raise AccessDenied(f"{user} is signed in but has no access to this application.")
    if not requested:
        return user, list(allowed)
    wanted: list[str] = []
    for r in requested:
        for e in normalize_entity(r):
            if e not in wanted:
                wanted.append(e)
    denied = [e for e in wanted if e not in allowed]
    if denied:
        log("denied", user=user, requested=wanted, denied=denied)
        raise AccessDenied(f"Access denied: {user} is not authorised for entity(ies) {', '.join(denied)}. "
                           f"Authorised: {', '.join(allowed)}.")
    return user, wanted


def run(mcp: FastMCP) -> None:
    import uvicorn
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
                proxy_headers=True)
