# Generation legacy cleanup implementation

- [x] L1 Durable task defaults, read-only legacy audit, and execution guards
- [x] L2 Ephemeral durable binding for standalone Java and Python
- [x] L3 Durable-only outer graph, state, config, and backend protocol
- [x] L4 Neutral harness injection; delete runtime/session compatibility bridge
- [x] L5 Extract retained Java/Python domain helpers and delete composites
- [x] L6 Replace/remove legacy `resume-gates`
- [x] L7 Delete legacy prompt writer/execution paths; retain the read-only
  compatibility pruner for one 30-day window
- [x] L8 Update README/operator/history docs and source boundaries
- [ ] L9 Focused/full verification, review, detailed commits, and push

K2 beta capacity/replay is explicitly skipped and is not a prerequisite for
these repository changes. Production deployment remains a separate authorized
operation; the hard cut requires a drained queue and zero audit blockers.
