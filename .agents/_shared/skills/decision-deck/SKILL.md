---
name: decision-deck
description: Use when an agent has decisions waiting on its human and wants them answered fast without overloading them — "bring me the decisions", "make a deck", "decision cards", "swipe through what you need from me" — or when a decision message would carry three or more open decisions. Publishes one card per decision, recommended option marked, as a claude.ai page whose answers the agent reads back.
---

# Decision deck: one decision at a time, the pick marked

The human's attention is the scarce resource. A deck shows one decision per card, with the
agent's pick marked and everything else folded away, and records how each was answered so
the shape can be tuned on evidence. It complements the one decision message (where a repo's
`AGENTS.md` asks for one); it does not replace it: the message keeps the decisions
in a few lines and links the deck.

## 1. Pick what goes in

- **Decisions only**, not tasks. "Run this command" or "sign in there" is a task; it stays in the message.
- Every card is answerable without opening anything else. If it needs a fact you don't have, find it first.
- **Short queues.** Five to ten cards; keep to what is really open.
- **Shuffle the order** when you seed: acceptance drifts along a swiping queue, so no card
  should always sit last. High-stakes cards still go first.

## 2. Write each card

A card is one document in the `cards` collection. `example-card.json` beside this file is a full one.

| Field | Rule |
|---|---|
| `order` | Position in the deck. |
| `source` | Where it comes from, in a few words (the plan or repo). |
| `question` | One line, a real question. |
| `options` | 2 or 3, never more: `{id, label, action, why}`. `action` is the button text, a verb phrase of 1–4 words. `why` is one checkable reason, not a sales line. |
| `pick` | The option you recommend. Always set. |
| `with_yes` / `with_no` | What happens with your pick, and without it. One line each. |
| `reversible` | `false` for anything that sends, pays, deletes or publishes. |
| `stakes` | `high` for what costs real money, time or trust if wrong: the page hides your pick until the human has chosen, then shows it and lets them keep theirs or switch (it records `blind_choice` and `agreed_blind`). Leave it out otherwise; a marked pick is accepted heavily, even when wrong, and this is the check for the cards where that matters. |
| `mode` | `swipe` or `buttons`. A card with `reversible: false` or `stakes: high` always gets buttons, whatever this says. On a computer people press the button beside the card rather than drag it, so swipe is mostly for the phone. |
| `visual` | Optional inline SVG, only when the decision turns on time, money, a before/after or what depends on what. Use the page's classes, not colours (`f-card f-sunk f-ink f-muted f-wait f-ok f-no f-pick`, `s-line s-ink s-muted s-wait s-pick`, text `t-ink t-on`), so it works in both themes. Scripts, links, images and `style` are stripped. |
| `context`, `quote {text, from}`, `sources [{label, url}]` | Folded behind «All the context». Paragraphs split on blank lines; `url` must be `https://`. |

About 30 words show before the human taps anything: question, options, the pick's reason.
Anything longer belongs in `context`.

## 3. Publish, seed and open

1. Copy `page.html` to the scratchpad and publish it with the Artifact tool:
   `capabilities: {db: {rules: [{path: "cards", read: "view", write: "owner"}, {path: "deck", read: "view", write: "owner"}, {path: "answers", read: "view", write: "admin"}]}, user: {}}`.
   One artifact per deck; a later round is a new artifact.
2. Seed with one `ArtifactData` batch: `deck/meta` `{title}` and each card as `cards/<id>`.
3. Open the link for the human (the owning repo's rule for opening links applies).
4. `ArtifactData list answers` once. An empty list is expected before they answer.

## 4. Read the answers back

When the human says they're done, or on your next turn:

- `list answers`. Each answered card has `{choice, label, followed_pick, ms, opened_context,
  mode, via, note, taken_back, deferred, stakes, blind_choice, agreed_blind}`. `active: false` means taken back or deferred, not answered.
- `choice: "other"` carries their own words in `note`. Treat it as an instruction to clarify, not a pick.
- Write each answer where its decision lives (the plan's gate and body, in the repo that owns it), the same turn.
- Report the round in one line: cards answered, median seconds, picks taken, contexts opened,
  take-backs. Where the owner keeps a log of rounds, append it there.

## Common mistakes

- Four options because the fourth is interesting. Drop it or fold it into `context`.
- A `why` that repeats the label. Give the reason a sceptic would check.
- Swipe on something final. Mark it `reversible: false`.
- A picture for decoration. No quantity, time or dependency in the decision means no `visual`.
- Re-asking a decision the human already gave. Only put it in a deck to confirm when they asked for that.
