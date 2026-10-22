---
name: grill-me
description: Interview the user relentlessly about a plan, design, or ADR until reaching shared understanding, walking each branch of the decision tree one decision at a time. Use when the user says "grill me", "interview me", "stress-test this plan", "poke holes", or when a proposal for the CI platform is still vague.
---

# Grill me

One question at a time. Resolve dependencies between decisions one-by-one. Walk down each branch of the design tree until you have a shared understanding.

## Rules

- **One question per turn.** Multi-part questions get hand-waved.
- **Recommend an answer.** Don't just ask — propose the answer you'd pick and why, then let the user override. A grilling that only asks is procrastination.
- **Explore the codebase instead of asking when you can.** If "does the scheduler already do Y?" is a grep away, grep — don't make the user remember.
- **Resolve dependencies in order.** Don't ask about the compose wiring before the trust boundary is settled. Don't ask about rollout before the failure story.
- **Stop when you have enough.** Grilling is a means to a plan or a decision, not a ritual. When the next question doesn't change any downstream decision, stop and summarize.

## What to grill on

Walk these in roughly this order, skipping ones already settled:

1. **Problem framing** — whose pain, how often, what's the cost of doing nothing. Which incident in `docs/incidents/` is this, if any?
2. **Scope boundary** — what's explicitly in, what's explicitly out, where the user would be tempted to scope-creep.
3. **Which component** — dispatcher, scheduler, lane host, runner image, review bot, role. A change that needs two of them is often a sign the split is being crossed.
4. **Trust boundary** — does this give the scheduler a credential, a dispatcher authority over another org's placement, or a lane host a secret? (AGENTS.md invariants 1–3.) If yes, the design is wrong regardless of how much simpler it looks.
5. **Operator neutrality** — does any code path pick a host, a path, a user or an address by default? (Invariant 4.) What does the *other* operator have to set to use this?
6. **Failure modes** — what happens when the scheduler is down, a lane host vanishes mid-job, a reservation leaks, the sidecar restarts. Which of these needs a reaper, which needs an incident note.
7. **Rollout** — single PR or phased? Does it change the pool config schema, the compose file, or a role interface that consumers pin by SHA? What does a consumer bumping their pin have to do?
8. **Test strategy** — which behaviours matter, and in which harness (see the `fix-bug` table). Tests here are the specification; a decision without a test that encodes it is not yet decided.
9. **Out-of-scope explicit** — capture the "no" answers as loudly as the "yes" ones; they prevent scope creep mid-implementation.

## When grilling for a plan

End by handing back a structured summary of decisions (problem, scope, component, trust boundary, operator surface, tests, out-of-scope). That lands in `docs/plans/ideas/<topic>.md` per `managing-plans-lifecycle` — don't write the plan inside this skill.

## Don't

- Don't ask questions whose answer doesn't change anything you'll write down.
- Don't pile up "and also…" questions in one turn.
- Don't ask the user to invent something the repo already has a convention for — read `AGENTS.md`, the relevant ADR in `docs/decisions/`, and the incident that motivated the code first.
- Don't keep grilling after the answers have stopped changing the design. Diminishing returns is the stop signal.
