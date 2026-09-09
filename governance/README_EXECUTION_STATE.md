# Compiled execution state

This directory does **not** add another coordinator. It compiles existing canonical owners into a disposable machine-readable working state so the coordinator does not reconstruct the building from prose on every turn.

Runtime contract:

1. Read canonical owners and current live evidence.
2. Compile retained scope, truth owners, current job cursor, dependency graph, required functions/lenses, facts and recurrence signatures.
3. Cross-check the execution envelope against the compiled state.
4. Auto-authorize actions inside standing authority; require human authority only for already-defined consequential classes.
5. Execute through the existing mutation gateway.
6. Verify fresh reality and reconcile owning state before completion.
7. Resume the cursor or traverse the next causal dependency.

The compiled output is not a source of truth. It is a build artifact and must be reproducible from current owners. Delete and regenerate rather than manually editing it.

No new scaffold component should be added unless a demonstrated execution failure identifies a missing mechanism that cannot be repaired in an existing owner.
