# Live NICSFCC connection (pending configuration and live validation)

The current FCCS server uses generated financials, journals, IC matches and close statuses.
This adds a separate read-only live server without altering the deployed demo.

## Render configuration

Switch the Dockerfile path to `overlay/Dockerfile.fccs-live` only after review and configuration.
Keep the existing Entra tenant, audience, required scope and public URL settings.
Entra authentication is mandatory; this server has no demo persona tool or auth bypass.

Set these in Render's Environment settings; never commit passwords:

| Variable | Value |
| --- | --- |
| EPM_BASE_URL | https://i8fccs-test-nicsintegr8.epm.uk-gov-cardiff-1.ocs.oraclegovcloud.uk |
| EPM_APPLICATION | NICSFCC |
| EPM_CUBE | Actual plan type, verified from Oracle |
| EPM_ENTRA_USER | Exactly one approved Entra user identity matching the token's `user` claim |
| EPM_USERNAME | Approved Oracle REST account |
| EPM_PASSWORD | Enter its existing password directly in Render |

This initial integration supports Oracle Basic authentication over verified TLS and exactly one
explicit Entra-to-Oracle account binding. The Oracle account's permissions govern returned cells.
Do not enable it for multiple users through shared credentials or simulated East/West roles.
If OAuth is required, configure a supported Oracle OAuth grant and implement token refresh
before deployment; an Entra access token is not an Oracle REST credential.

## Copilot Studio

Refresh the MCP tool list after deployment. The live server exposes `whoami`,
`list_dimensions`, and `get_financials`. Demo close, journals, IC and variance tools are absent
until actual Oracle endpoints and application-specific mappings are implemented and validated.
Remove the persona-switch instructions and fixed USD assertion. Ask for a year and all required
POV members. The financial tool requires literal Account and Entity members, a Period, Year,
Scenario, View, and `pov` for all remaining dimensions. It returns the original Oracle grid;
missing values remain missing, and consolidated parent values are never recreated by summing entities.

## Validation required before activation

1. Run `python3 -m unittest test_oracle_fccs.py` from the repository root.
2. In a dependency-installed build, start the live server with actual Entra configuration;
   verify the pinned MCP package and inherited Entra verifier support its initialization.
3. Reject unauthenticated, wrong-audience, wrong-scope and unbound-user requests.
4. Read live dimensions and confirm the cube and full POV against NICSFCC.
5. Read one agreed cell and compare with Smart View using identical POV and Oracle user.
6. Verify restricted/missing intersections are not returned as zero.
7. Confirm the agent only offers the live tools, and that no demo persona is callable.

Oracle exportdataslice exports cells for which the Oracle user has read-write access. It may
omit cells visible with read-only permissions; an empty response is not proof of zero or no data.
The `/health` route reports process health only and explicitly does not certify Oracle connectivity.

References:
- https://docs.oracle.com/en/cloud/saas/enterprise-performance-management-common/prest/authentication.html
- https://docs.oracle.com/en/cloud/saas/enterprise-performance-management-common/prest/export_dataslices.html
- https://docs.oracle.com/en/cloud/saas/enterprise-performance-management-common/prest/get_dim_plan_types.html
