# Security Policy

## Supported versions

projmem is pre-1.0. The latest commit on `main` is the only supported
version. Older tags are archived and not patched.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security-relevant findings.

Email the maintainers at <m4ll0k@protonmail.com> (replace with actual
address before publishing) or use GitHub's private vulnerability reporting:

    Repository → Security → Report a vulnerability

Include:
- A clear description of the issue
- Reproducer steps (code snippet, command line, CVE-style if applicable)
- Affected version (git SHA or tag)
- Suggested remediation if you have one

We aim to:
- Acknowledge within 7 days
- Provide a plan / fix within 30 days for high-severity issues
- Credit the reporter in the release notes unless you prefer anonymity

## Scope

In-scope:
- Code execution via crafted index inputs, config files, or notes
- Reading files outside the configured repo root
- Privilege escalation via the CLI

Out-of-scope:
- Hallucinations / wrong answers from claim verification (these are
  correctness issues, not security issues — please open a regular bug)
- Denial of service via extremely large repos (use `max_file_bytes` and
  `--timeout`)
- Issues in dependencies (report upstream)
