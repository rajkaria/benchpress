# RFCs

A breaking change to a public contract goes through a short RFC before any code lands. Public contracts are the
Python API listed in the PyPI readme, the CLI flags, the receipt schema (`benchpress-receipt/1`), the guard policy
file format, the gate's refusal rules and the `ToolSpec` fields.

Additive changes (a new optional key, a new CLI flag, a new policy pack) do not need an RFC; a PR with a CHANGELOG
entry is enough.

## Process

1. **Open an issue** describing the problem and the change you want, and say it needs an RFC.
2. **Write the RFC** as a pull request adding `docs/rfcs/NNNN-short-title.md`, numbered after the highest existing
   RFC. Keep it short:
   - **Summary**: one paragraph.
   - **Motivation**: what breaks or is impossible today, with a concrete example.
   - **Design**: the new behavior, including exact API, schema or file-format changes.
   - **Compatibility**: who breaks, how they migrate, and whether a deprecation period is possible.
   - **Safety**: whether the change can let a write bypass the gate or skip read-back, and why it cannot.
   - **Alternatives**: what else was considered and why it lost.
3. **Discussion** happens on the PR. The RFC stays open for at least 7 days so users can weigh in.
4. **Decision**: the maintainer merges the RFC as *accepted* or closes it with the reason. An accepted RFC is a
   commitment to the design, not to a date; implementation PRs link to it.

No RFCs have been filed yet.
