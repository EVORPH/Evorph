# Security policy

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability.

Use GitHub private vulnerability reporting on this repository: open the
**Security** tab and choose **Report a vulnerability**. The report is visible
only to the maintainers.

## Scope

In scope: the verification kernel and contracts (`backend/evorph/`), the model
channel and spending ledger (`backend/agents/`, `backend/runtime/`), the public
read and submission API (`backend/service/`), the deployment files (`deploy/`),
and the static site (`frontend/dist/`).

Out of scope: model providers, registrars, hosting providers, and any other
third-party service; report those to their operators. A research finding about
a candidate under test is the subject of this project, not a vulnerability in
it — submit it through the public challenge flow described in CONTRIBUTING.md.

## Response

Reports are triaged by the maintainer. A confirmed issue in the deployed
service is fixed, deployed, and recorded in CHANGELOG.md and VERIFICATION.md
with the affected component named. The project does not run a bug bounty.
