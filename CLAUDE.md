# openbench_eg

A fork of [AndyGrant/OpenBench](https://github.com/AndyGrant/OpenBench), the engine testing framework, run on bobby to
test Elephant. It adds a reporter that sends every test and every upload of games to nyayo, a worker that names and
tags each game before it uploads, and a full smoke test; everything else is upstream's.

## Specs

What this repo does is specified here, in `docs/spec.md`: Purpose, Scope, Current and Interfaces. **Keep it true in
the same pull request as the code**: a change that alters a command, an endpoint, a schema, a file format or anything
the spec describes updates the spec in that pull request. When the code and the spec disagree, the code wins and the
spec is fixed.

Where this repo is going lives in ramani (github.com/looak/ramani, private, sibling checkout at `../ramani`): the
README for the system and the contracts table, `openbench.md` for this repo's Direction (planned work, open
decisions), and `plans/` for work spanning repos. **Read ramani, never edit it**, nor any other sibling checkout. A
pull request that does a plan's step says `Closes looak/<repo>#<sub-issue>`. When it settles one of ramani's open
decisions, say so in the final report. This repo owns no contract: it consumes nyayo's match schema
(`docs/match-schema.md` in nyayo), which is changed only there, by agreement with the owner.
