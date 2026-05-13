# v2 license decision

Per `docs/v2-design.md` Part VIII, the license question must be settled
before `v2.0.0` ships. Three options were on the table:

1. **Stay PolyForm Noncommercial 1.0.0** — the v1 license.
2. **Move to Business Source License (BSL) 1.1** with a 2-year
   conversion to Apache 2.0.
3. **Open-core split** — keep the core (CLI, verifier, schema) under
   PolyForm NC; release the daemon + UI under a paid commercial
   license.

Decision: **stay on PolyForm Noncommercial 1.0.0 for v2.0.** Re-open
the question for v3 once we have actual commercial-interest signal.

## Reasoning

**Why not BSL.** BSL is the "right" license for a project that
expects strong commercial adoption now and wants a deferred-OSS
conversion to land on a familiar Apache-2.0 surface later.
projmem doesn't have that adoption pressure yet — the v1 PolyForm
NC posture has produced exactly the personal / research / educational
usage the license is shaped for, with no observable commercial-use
ambiguity. BSL adds a 2-year-conversion overhead (annual review of
"change date", dependency-package scanning to flag downstream
projects relying on conversion, etc.) without a corresponding upside
for v2.0 specifically. Park BSL for v3 if the corp-scale path opens
up.

**Why not open-core.** The open-core split (free CLI + paid daemon /
UI) is the most monetizable option but the worst fit for the v2
product story. The v2 narrative is *integrated* — the announce-before-
action pipeline depends on the constitution + the hook + the daemon +
the UI being one continuous surface. Carving the daemon + UI out as
a paid-only component fragments the demo: "here's projmem, but to
see it work you have to pay." That's exactly the friction the
free-CLI tier was supposed to eliminate. Open-core is a fine 2026/27
move once `v2.0` has proven the integrated story; doing it at the
release boundary kills the demo before it lands.

**Why stay on PolyForm NC.**
- The license has worked for v1 — no community pushback, no
  commercial-use ambiguity surfaced in the v1 cycle.
- It cleanly permits exactly the use the v2 narrative aims at:
  individual developers, research, education, open-source projects.
- It blocks corporate use without explicit license — the
  conversation gate we want during the v2.0 → v2.x transition while
  we figure out what commercial usage actually looks like.
- It's reversible. Re-licensing the project to BSL or Apache later
  is mechanically straightforward (contributors agree, file the
  change, ship). Going the other direction — open-sourcing then
  rolling back — is socially impossible.

## Operative consequences

- `LICENSE` stays PolyForm NC 1.0.0.
- `pyproject.toml::project.license` stays `PolyForm-Noncommercial-1.0.0`.
- README hero language stays "free for personal, research,
  educational, noncommercial use; commercial use requires a separate
  license." No corporate-friendly framing yet.
- `CITATION.cff` carries the unchanged license metadata.
- Commercial inquiries: existing channel via the email in
  `CITATION.cff`.

## When to re-open

Re-open the license question when ANY of these fires:

- ≥ 3 separate corp-scale teams ask about commercial licensing.
- A clear competitive surface emerges where Apache-2.0 / MIT-licensed
  alternatives are eating into adoption purely on license grounds.
- A funding event or sponsor specifically conditions on a more
  permissive license.
- `v3` planning starts and the team genuinely wants to expand the
  contributor pool beyond what a noncommercial license permits.

Until any of those: PolyForm NC continues to do its job.
