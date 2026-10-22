# Security and data boundaries

The reviewer parses local Python; it does not import or execute it. The web review endpoint accepts source text with a bounded request size and does not persist submitted source. Reports omit source literal values by default, but filenames, identifiers, and caller-supplied metadata can still be sensitive. Review artifacts before sharing them.

The local server is a development/reporting service. Keep its default loopback binding. Non-loopback use requires a token. Host/Origin validation currently expects the server's port, so a proxy using a different public port requires reviewed integration changes. The supplied local/Docker setup is not turnkey Internet-facing deployment; production exposure also requires appropriate transport security and access controls. Authentication is not tenant isolation. Do not expose production reports through a public static dashboard.

Only application-controlled adapters execute benchmarks. Do not import untrusted repositories to run tests or metrics. The agent skill follows its host's access and approval policies.

Report vulnerabilities privately to the repository maintainer through an available private channel. Do not publish credentials, private source, or sensitive reproduction data in a public issue.
