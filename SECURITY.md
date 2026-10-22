# Security and data boundaries

The reviewer parses local Python; it does not import or execute it. The web review endpoint accepts source text with a bounded request size and does not persist submitted source. Reports omit source literal values by default, but filenames, identifiers, and caller-supplied metadata can still be sensitive. Review artifacts before sharing them.

`applied-opt serve` is a local development/reporting service; keep its default loopback binding. `applied-opt host` is the Gunicorn-backed deployment entry point and requires a token of at least 16 printable ASCII characters without spaces plus a declared public origin (`APPLIED_OPT_PUBLIC_URL`, with `RENDER_EXTERNAL_URL` as a fallback). Host and browser Origin checks use that origin independently of the internal port. Arbitrary forwarded headers do not establish a trusted public origin. Keep the application port private behind the configured HTTPS proxy for public deployments. The VPS Compose file provides Caddy HTTPS and persistent certificate storage; the Render Blueprint uses the platform's HTTPS endpoint. See [hosting instructions](docs/hosting.md).

The HTML shell and health check are public. Report, source review, and graph APIs require the bearer token in hosted mode. Tokens must stay out of public source, static HTML, URLs, and shared screenshots. The bootstrap generates a token in a private environment file without overwriting existing configuration; the browser token field does not persist it to browser storage. Rotate the server token and restart/redeploy if it is exposed. Authentication is one shared token, not user accounts or tenant isolation. Use a separate deployment or an organizational access proxy for distinct access boundaries, and review reports before sharing them.

Requests and report files are bounded, but the service is not a general hostile-code execution sandbox or an unlimited public analysis API. Source is parsed and not executed. Configure ingress capacity and abuse controls to match the exposure of your deployment. The supplied assets do not imply that your provider account, DNS, certificates, or operating environment have been validated by this project.

Only application-controlled adapters execute benchmarks. Do not import untrusted repositories to run tests or metrics. The agent skill follows its host's access and approval policies.

Report vulnerabilities privately to the repository maintainer through an available private channel. Do not publish credentials, private source, or sensitive reproduction data in a public issue.
