# Security Policy

## Supported version

Security fixes are applied to the current `main` branch. Older commits, local data,
third-party model gateways, and user-supplied deployment infrastructure are not
maintained as separate supported releases.

## Reporting a vulnerability

Please use GitHub's **Report a vulnerability** private reporting form for this
repository. Do not open a public issue for suspected credential exposure,
authentication bypass, cross-user data access, remote code execution, or a flaw
that could reveal portfolio, research, audit, or model-configuration data.

Include the affected commit, reproducible steps, impact, and any suggested
mitigation. Do not include real credentials, tokens, cookies, personal data, or
production datasets in the report.

## Operational scope

This repository is a research, backtesting, and paper-portfolio system. It does
not authorize automatic live trading. Public deployment requires HTTPS, secure
cookies, non-default database and Redis passwords, trusted-host restrictions,
and secrets stored outside Git. See `docs/DOCKER_DEPLOY.md` for the deployment
security checklist.
