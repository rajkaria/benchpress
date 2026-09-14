# Security policy

Benchpress sits between agents and production systems, so we treat security reports as the highest-priority issue class.

- **Report privately** via [GitHub's private vulnerability reporting](https://github.com/rajkaria/benchpress/security/advisories/new) on this repository. Do not open a public issue.
- You will get an acknowledgement within 48 hours and a fix or mitigation plan within 7 days for anything that lets a write bypass the gate, leaks credentials into receipts, or executes a refused action.
- Supported versions: the latest minor release.
- Scope: the Python package, the TypeScript package, the gateway, the console, the container image and the Helm chart. Benchmark harnesses and provider twins are out of scope unless they affect a shipped artifact.
