"""Read-only Oracle FCCS adapter. No synthetic data or demo role overrides."""
import base64
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler

HOST = "https://i8fccs-test-nicsintegr8.epm.uk-gov-cardiff-1.ocs.oraclegovcloud.uk"


class OracleError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def literal(value):
    # Only literal member names; no Essbase expressions or selection functions.
    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        raise OracleError("Provide a nonempty literal member name.")
    if any(c in value for c in '()@"\\\r\n'):
        raise OracleError("Member selection expressions are not supported.")
    return value


class OracleFCCS:
    def __init__(self, env=None, opener=None):
        self.env = os.environ if env is None else env
        self.opener = opener or build_opener(HTTPSHandler(), NoRedirect())

    def authorize(self, entra_user):
        allowed = self.env.get("EPM_ENTRA_USER", "").strip().casefold()
        if not allowed or str(entra_user).casefold() != allowed:
            raise OracleError("This Entra identity has no configured Oracle account binding.")
        if not self.env.get("EPM_USERNAME") or not self.env.get("EPM_PASSWORD"):
            raise OracleError("Oracle REST credentials are not configured.")
        return self.env["EPM_USERNAME"]

    def request(self, user, suffix, payload=None):
        self.authorize(user)
        base = self.env.get("EPM_BASE_URL", HOST).rstrip("/")
        if base != HOST:
            raise OracleError("The Oracle host must match the approved NICSFCC test environment.")
        application = self.env.get("EPM_APPLICATION", "NICSFCC")
        root = base + "/HyperionPlanning/rest/v3/applications/" + quote(application, safe="")
        cred = self.env["EPM_USERNAME"] + ":" + self.env["EPM_PASSWORD"]
        headers = {"Authorization": "Basic " + base64.b64encode(cred.encode()).decode(),
                   "Accept": "application/json", "Content-Type": "application/json"}
        data = json.dumps(payload).encode() if payload is not None else None
        req = Request(root + suffix, data=data, headers=headers,
                      method="POST" if payload is not None else "GET")
        try:
            with self.opener.open(req, timeout=45) as response:
                result = json.loads(response.read(4_000_001))
        except HTTPError as exc:
            raise OracleError(f"Oracle REST returned HTTP {exc.code}; verify credentials and EPM permissions.") from None
        except (URLError, TimeoutError):
            raise OracleError("Oracle REST connection failed or timed out.") from None
        except (ValueError, UnicodeError):
            raise OracleError("Oracle returned an invalid JSON response.") from None
        if not isinstance(result, dict):
            raise OracleError("Oracle returned an unexpected response structure.")
        if result.get("status", 0) not in (0, "0", None):
            raise OracleError("Oracle rejected the request; check the application and POV members.")
        return result

    def cube_path(self):
        cube = self.env.get("EPM_CUBE", "").strip()
        if not cube:
            raise OracleError("Set EPM_CUBE to a verified NICSFCC plan type.")
        return "/plantypes/" + quote(cube, safe="")

    def dimensions(self, user):
        result = self.request(user, self.cube_path() + "/dimensions")
        return {"source": "Oracle FCCS REST", "application": self.env.get("EPM_APPLICATION", "NICSFCC"),
                "cube": self.env["EPM_CUBE"], "metadata": result}

    def financials(self, user, accounts, period, view, scenario, entities, year, pov):
        self.authorize(user)
        if not accounts or not entities or not year:
            raise OracleError("Provide actual Account, Entity and Year members; demo defaults are not used.")
        if len(accounts) * len(entities) > 200:
            raise OracleError("Limit the request to 200 Account x Entity intersections.")
        if view not in ("Periodic", "YTD", "FCCS_Periodic", "FCCS_YTD"):
            raise OracleError("View must be Periodic or YTD.")
        members = {"Year": literal(year), "Scenario": literal(scenario),
                   "View": view if view.startswith("FCCS_") else "FCCS_" + view}
        extra = dict(pov or {})
        if any(k in extra for k in ("Entity", "Account", "Period", "Year", "Scenario", "View")):
            raise OracleError("POV cannot override tool arguments or axes.")
        members.update({literal(k): literal(v) for k, v in extra.items()})
        dims_result = self.request(user, self.cube_path() + "/dimensions")
        items = dims_result.get("items")
        if not isinstance(items, list) or not items:
            raise OracleError("Oracle did not return dimension metadata.")
        dimensions = {item["name"] for item in items}
        assigned = set(members) | {"Account", "Entity", "Period"}
        if dimensions != assigned:
            raise OracleError("Complete the POV for all cube dimensions. Missing: " +
                              ", ".join(sorted(dimensions - assigned)) + "; unexpected: " +
                              ", ".join(sorted(assigned - dimensions)))
        grid = {"suppressMissingRows": False, "suppressMissingColumns": False,
                "pov": {"dimensions": list(members), "members": [[v] for v in members.values()]},
                "columns": [{"dimensions": ["Account"], "members": [[literal(a) for a in accounts]]}],
                "rows": [{"dimensions": ["Entity", "Period"],
                          "members": [[literal(e) for e in entities], [literal(period)]]}]}
        result = self.request(user, self.cube_path() + "/exportdataslice",
                              {"exportPlanningData": False, "gridDefinition": grid})
        if not isinstance(result.get("rows"), list) or not isinstance(result.get("columns"), list):
            raise OracleError("Oracle returned an unexpected data grid.")
        # Preserve Oracle grid, missing values, currency POV and parent consolidation values.
        # Do not infer zeros, currency, or sum parent entities into a synthetic total.
        return {"source": "Oracle FCCS REST", "application": self.env.get("EPM_APPLICATION", "NICSFCC"),
                "cube": self.env["EPM_CUBE"], "year": year, "period": period,
                "view": members["View"], "scenario": scenario, "pov": members, "grid": result}
