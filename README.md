# N0TE

Current clean-room N0TE implementation repository.

## Authority boundary

- Product semantics are owned by the current canonical N0TE semantic owners in the clean Google Drive environment.
- This repository owns implementation, executable governance, machine-readable evidence/current state, and non-commanding migration provenance.
- Presence of source or a passing construction test does not by itself establish integration, reachability, verification, recovery, authority safety, consumer acceptance, or value evidence.
- Parent acceptance is derived from required child obligations.
- Fresh contradictory evidence reopens the affected current claim.
- Historical migration material is non-commanding.

## Coordinator execution gate

Stateful coordinator work is designed to pass through a fail-closed execution gate before a registered mutation executor is called. The gate validates the execution envelope, cross-checks it against a server-owned trusted context snapshot, binds authority to one exact `ActionIntent`, and consumes a short-lived one-time permit before mutation.

`n0te.coordinator_mcp` exposes the gate over MCP v2. It intentionally exposes no mutation executor by itself. A deployment is not load-bearing until: (1) a trusted canonical sync produces current snapshots, (2) real provider mutation adapters are registered only behind `CoordinatorMutationGateway`, and (3) the coordinator host cannot bypass the gateway through independent write tools. Repository code cannot enforce the third condition inside an external host that separately exposes direct write connectors.

Cutover is complete. Temporary bootstrap authority is expired. Normal operation begins from the clean N0TE START HERE and this repository's current governance/evidence state.
