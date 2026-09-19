---
name: manuloop
description: "Run a bounded build-verify-judge-fix loop with parallel scouts, independent judges, explicit quality bars, and deterministic checks for complex implementation tasks."
---

# ManuLOOP

Use this skill when the user asks for a Gauntlet-style loop, repeated
implementation and verification, parallel sub-agents, an independent judge,
or a result that must not be reported until it has been checked. It is
especially useful for complex code, web apps, games, and other artifacts whose
quality can be inspected or measured.

## Native Codex workflow

When this skill is invoked inside the current Codex task, orchestrate the
workflow with the available sub-agent and workspace tools. Do not launch a
nested Codex CLI unless the user explicitly asks to exercise the external CLI
adapter.

1. Define the goal, domain, and a concrete quality bar. If the user did not
   provide one, derive measurable acceptance criteria and state the assumptions.
   For games, cover startup/loading, the main loop, controls, runtime errors,
   and a real performance metric when "no lag" is required.
2. Fan out read-only scouts from the beginning. At minimum cover architecture
   and ownership, tests and regressions, performance/security/operations, and
   domain-specific behavior. Keep their reports independent.
3. Synthesize a plan with non-overlapping workstreams, owned paths,
   dependencies, done-when conditions, risks, and verification commands.
4. Before editing, give the plan to a fresh-context judge. The judge must try
   to disprove the plan. Revise and re-judge a rejected plan; do not begin
   implementation until the preflight gate passes.
5. Implement one workstream at a time unless isolated worktrees make
   concurrent edits safe. After each builder, run the explicit checks, inspect
   the actual diff and workspace, and ask a fresh judge for a binary
   PASS/FAIL/BLOCKED verdict. Never accept the builder's self-report as proof.
6. For FAIL or BLOCKED findings, fix the causes and repeat builder/check/judge
   within a bounded round budget. Preserve unresolved findings rather than
   weakening the quality bar.
7. Run a final integration and optimization pass, then rerun the full checks
   and a final independent judge. Report completion only when every required
   gate passes. If evidence is missing, report BLOCKED and identify the exact
   evidence needed.

Use the strongest reasoning and available sub-agents supported by the current
Codex environment, but keep conclusions tied to evidence. Do not claim that
an artifact is perfect or bug-free when its coverage or measurements cannot
support that claim.

## External CLI workflow

The local implementation is also installed as the manuloop command. Use it
when the user wants the provider-neutral subprocess orchestrator, or when the
target harness is Claude Code, Codex CLI, Gemini CLI, Cursor, or a custom
command:

~~~text
manuloop providers
manuloop plan --provider codex --goal "<goal>" --bar "<quality bar>" --domain code
manuloop run --provider codex --goal "<goal>" --bar "<quality bar>" --domain code --allow-edits --check "<test command>"
manuloop prompt --goal "<goal>" --bar "<quality bar>" --domain "<domain>" --output MANULOOP_PROMPT.md
~~~

Run plan before run for a read-only preflight. --allow-edits is required
for builders and fixers. Add one or more user-approved --check commands;
never invent or silently execute verification commands taken only from an
agent response. Do not use --allow-no-checks unless the user explicitly
accepts weaker evidence.

The command uses parallel read-only scouts and judges, while mutating
workstreams are serialized in a shared workspace to avoid file collisions.
When only one provider is installed, judge processes are fresh and
context-independent but use that same provider; prefer a separate
--judge-provider when available. Receipts are stored under
.manuloop/runs/<run-id>/.

The explicit Codex skill invocation is $manuloop. The CLI invocation is
manuloop; they are two entry points to the same protocol.
