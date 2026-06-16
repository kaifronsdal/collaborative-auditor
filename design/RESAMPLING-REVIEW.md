# Red-team review of `resampling.md`

> **Findings addressed in PR #110 commits `8ad04b3..4f4aa50`; kept as the review record.**
> The three recommended changes (drop recursive `wrap` as the public surface; strengthen the
> desync guard with role-qualified `source`; truncate-at-edit + unify `Step` with `Effect`)
> all landed. `serve`→`replayable`, `sync`→`boundary`, `compose` is a `Tape` method,
> `_mark`/`_wrap` private, `ReplayingModel`/`recording()` deleted in favour of one wrapping
> pattern, `Step.dump()/load()` replaces `SerializedStep`. Names below are pre-refactor.

*Adversarial review of the n-level Tape / `nondet(c,k)` / recursive `replayₖ` design,
2026-06-15. Grounded in petri-meridian @ `origin/controller-refactor`, inspect_ai main, and
ARCHITECTURE.md. The central claim — the channel can run live during a level-2 replay because
`nondet(channel,2)=F` — survived; the holes are in surrounding completeness and a concurrency
assumption.*

---

## Verdict

The core insight is **right and elegant**: resume-from-`.eval` and target-rollback are the same
record/replay idea at different nesting depths, and unifying them removes real duplication. The
`nondet(c,k)` framing correctly captures *why* the channel can run live during a level-2 replay
(the auditor restarts and re-supplies it). **But the design oversells its own generality and
has completeness gaps:** the realism approver calls a model outside `ReplayingModel`, and
variable-call-count paths (compaction's `count_tokens` loop) can desync without the guard
catching it. (An earlier draft of this review claimed `execute_tools` parallel stages were a
blocker; that was a misread — `parallel` defaults to `False`, see C2.) The workbench's actual need (edit-and-resample) is
**always truncate-at-edit**, which simplifies the mechanism. The n-level recursion is more
machinery than petri (n=2) or the workbench (n=3) justifies. **Recommend:** keep the
`nondet`/`sync` formalism and the `Tape` primitive; drop the recursive `wrap` for explicit
2/3-level composition; fix the concurrency and realism holes before claiming determinism; rewrite
substitute-then-replay around truncation.

---

## Correctness holes

**C1 — Single-τ₂ argument is correct for the wrong reason.** The doc says "auditor drains its
prefix first because target's pending₂ reads can't happen until after rendezvous." Actually τ₂'s
`pending` is drained **exclusively** by the auditor — every target→τ₂ call is `_mark`ed
(`nondet(·,2)=F`, never popped); `target.generate` is an L1 call, not τ₂. Documentation
precision, not a mechanism bug, but it masks C2.

**C2 — ~~Concurrent parallel-tool execution desynchronizes channel ordering.~~ WITHDRAWN.**
The reviewer misread `tool/_tool.py:228` — `tool_parallel = parallel is True` evaluates to
**`False`** when `parallel=None` (the `@tool` default). Parallel tool calling landed in inspect
#4013 (2026-05-22) but is opt-in; every petri tool is bare `@tool`/`@tool(viewer=…)` with no
`parallel=` arg, so all run sequentially in declared order. The doc's "rendezvous keeps
lock-step" holds. (A per-channel lock wouldn't help against the hypothetical anyway — it would
serialize the calls but not which coroutine reaches the lock first.) The only residual is
documentation: petri's channel-mutating tools depend on `parallel=False` and nothing says so;
worth a comment on `Channel.request` and/or an explicit `parallel=False` on the decorators as
intent.

**C3 — `nondet(c,k)` is not transitive through arguments; variable call counts desync.**
`count_tokens` is wrapped `nondet=T`, served from `pending₂`. Compaction calls it a *variable*
number of times (loop at `_compaction.py:447-450`) depending on `len(messages)`, which on a
level-2 replay is rebuilt live. If live reconstruction differs even slightly (C2, or any
formatting change), the *count* of `count_tokens` calls changes — but `pending₂` still hands back
old recorded counts in order. `ReplayDesyncError` only checks `source`/`sync`, not call count.
Compaction takes a different decision than recorded; silent divergence. The formalism treats each
call's `nondet` as a fixed bit independent of arguments — it isn't.

**C4 — "Running live is required" is true only for rendezvous calls, not all `nondet=F`.**
Counterexample: a deterministic-at-k call with no waiting party (`today_date` read) — replaying
*or* live both correct. And a case where running live is *wrong*: `MessageMap.shorten`
(`_channel.py:689-696`) — order-dependent counter mutation inside `send_output`. On level-2
replay it re-runs live; if turn order differs (C2/C3), M-short-ids diverge from recorded, and a
persisted `at=M7` resume target points at a different message. The doc lists MessageMap as
"rebuilds itself" — it does, but not necessarily *identically*.

**C5 — Induction doesn't walk the joint-pending case.** `pending₂≠∅` *and* `pending₁≠∅`
simultaneously — a resume whose recorded auditor turn contains `rollback_conversation`, seeding
L1 from a slice of the live-rebuilt `log₁`. Probably correct, but it's the actual petri
resume-with-rollback path and the doc dismisses it in one line.

**C6 — `cutoff` undefined at tape boundaries.** No following `sync="in"` (tape ends mid-turn) —
the current `_find_cutoff` falls off the loop end; the doc's reformulation doesn't specify. Two
adjacent `sync="in"` — can't happen in the standard agent (Resume → break → `send_output(out)` →
`next_command(in)`), but a custom target (Dish) could. Must be specified.

---

## Missing coverage (unwrapped nondeterminism)

**M1 — Realism approver bypasses `ReplayingModel`. BLOCKER for `realism_filter=True`.**
`_check_realism` calls `get_model(role="realism")` itself (`_realism/approver.py:162-165`) and
hands it to `generate_answer` (inspect_scout) which runs its own retry loop — multiple unwrapped
model calls per check, inside `execute_tools`'s approval hook. The doc says "threaded through new
`model=` param" as if done; it isn't, and `generate_answer`'s internal retry-count nondeterminism
reintroduces C3.

**M2 — Compaction reaches `model.api` directly.** `count_tool_tokens`, `_redacted_reasoning_
tokens_total`, collapse all access `.api` (`_compaction.py:159,247,257-261`). If `ReplayingModel`
overrides only `.generate`/`.count_tokens`, `.api` returns the unwrapped provider.

**M3 — Parallel-tool ordering** (= C2): scheduling nondeterminism the tape can't record. Must be
eliminated, not recorded.

**M4 — Retry/backoff jitter** is correctly *covered* (the wrap point is above retry); the doc
should say so.

**M5 — `Node.span_id = uuid()`** is fresh per branch and not recorded; on outer replay span_ids
differ from the original. Cosmetic unless something keys off them.

**M6 — `eager_resume` strip-on-error branch** isn't covered by "deterministic id, returns a
copy"; the strip decision depends on whether the injected resume errored, which is live state.

**M7 — `count_tokens` IS genuinely nondeterministic** (provider API call with retry) — the doc is
right to wrap it; but its variable call count inside compaction is the worst C3 offender.

---

## The substitute-then-replay question — definitive

**It is always truncate-at-edit. "Substitute one `Step.value` without truncating" is never
correct for a workbench edit.**

Auditor edit at turn k: replace the `Step` at k with an edited `ModelOutput` carrying different
`tool_calls`. On replay, `pending₂` serves the edited output; `execute_tools` runs the new
tool_calls live; target rebuilds differently. But `pending₂` still holds the **old** turn-(k+1)
auditor generate, conditioned on the *old* tool results. It pops next — same `source`
(`"Model.generate"`), same `sync` (`None`), **no desync error** — and you silently get a
turn-(k+1) referencing tool results that no longer exist. → must drop `log₂[k+1:]`.

Target edit: substitute the L1 step at T. The auditor's resume tool (live) formats the *edited*
response — but the auditor's next `generate` is still in `pending₂`, recorded against the *old*
T. Same silent stale replay. → truncate.

So the mechanism is `seed = log[:edit_idx]` + optional boundary-value override + live
continuation. The doc already has `truncate_at`; what's missing is the explicit statement that
this is *the* edit semantics. The one sound substitution-without-truncate: editing the very last
step with nothing after it. Not worth a separate path.

---

## Cleaner alternatives

**E1 — Hardcode 2/3 levels, drop the recursive `wrap`.** The `replayₖ` recursion supports an `n`
no one needs and produces deeply-nested closures whose desync errors are unreadable. The
*reasoning* (replay at k iff nondeterministic there; channel ops are `nondet=F` at outer levels
because the producer restarts) survives intact with explicit composition. **Recommend.**

**E2 — Separate tapes per coroutine (τ₂ᴬ, τ₂ᵀ).** Eliminates the interleaving argument — but
target has *no* level-2 served calls (τ₂ᵀ would be all-`None` metadata), and `cutoff` needs the
*merged* order. Re-merging by sequence reintroduces what you split to avoid. **Reject; keep
single τ₂ once C2 is fixed.**

**E3 — Record everything at level 2 (drop `_mark`).** Kills all rebuild-must-match fragility (C3,
C4, M-map drift). **But it's incompatible with "continue live after the prefix"**: if channel ops
replay as pure values, the target coroutine never runs, and when `pending₂` empties there's no
live target to continue against. Size is a red herring; correctness kills it. **Reject — and
this is the load-bearing reason `nondet=F` exists. The doc should state it as the justification
for `_mark`.**

**E4 — Event-sourcing / `Effect` log.** An `Effect` log is "`Step` log with a `kind` tag" once
you add `recorded_output`. **Unify `Step` and `Effect` into one durable type** — `Step` is closer
to the replay mechanism; add serialization to it, drop the parallel `Effect` log. ARCHITECTURE.md
already converged here from the other side.

**E5 — Prior art: Temporal.** Temporal forbids non-determinism outside activities and *enforces*
it by asserting every replayed decision matches — petri's `ReplayDesyncError` checks only
`source`+`sync`, which is too weak (C2/C3/§d slip through). **Strengthen toward "assert match at
every step"; enumerate wrapped primitives; CI fails on a new unwrapped one.**

---

## Implementation hazards (ranked)

1. ~~Parallel-tool concurrency~~ — withdrawn (see C2). `parallel` defaults to `False`; petri's
   tools run sequentially. Residual: a comment on `Channel.request` documenting the dependency.
2. **Realism model bypass (M1)** — blocker for `realism_filter=True`. Fix: wrap `_check_realism`
   as one τ₂ step (record the `Approval`), or run resumable audits with `realism_filter=False`.
3. **`ReplayDesyncError` source non-uniqueness** — auditor-generate vs realism-generate are both
   `"Model.generate"`, `sync=None`, on τ₂. Guard gives false confidence. Fix: `source = (role,
   qualname)`.
4. **`deepcopy` per boundary** — twice per served step on large `ModelOutput`s. Anchor identity
   survives (`.id` is a plain field, copied verbatim). `Slot.__deepcopy__→self` is essential;
   audit for other identity-keyed singletons.
5. **`config_digest` insufficiency** — add model ids (auditor + target) and `GenerateConfig`.
6. **"Wrapped exactly once" unenforced** — two `TargetContext`s from the same `Node` →
   double-wrap → double-pop. Add `assert not node._wrapped`.
7. **Test migration** — 168 sites, not ~50; `test_history.py` (107) tests `_Step` internals
   (`anchor_id`/`deterministic`/`_find_cutoff`) that don't map. Black-box context tests can shim
   (`Trajectory = lambda: Node(Tape())`); white-box history tests must be rewritten.
8. **`ReplayingModel` doesn't exist on `controller-refactor` yet** — must proxy `.api`,
   `.count_tool_tokens`, `.name`, `.config`, `isinstance`.

---

## Three changes

1. **Drop recursive `wrap`/`replayₖ`; keep the formalism, write explicit 2/3-level wiring.** The
   recursion is the doc's intellectual centerpiece and its biggest liability — composed-closure
   stack traces for an `n` no one needs. The reasoning survives intact.
2. **Strengthen the desync guard (role-qualified source, Temporal-style per-step assert, CI
   enumerate-wrapped-primitives).** Determinism that isn't tested rots. Adopt the
   wrap-the-decision pattern — `_check_realism`, compaction's compact-or-not become single τ₂
   steps — so variable internal call counts (C3/M1/M7) can't desync.
3. **Rewrite around truncate-at-edit; unify `Step` with `Effect`.** The honest mechanism is
   `log[:edit_idx]` + boundary override + live; arbitrary mid-tape substitution is a phantom
   requirement. One durable serializable type unblocks the workbench's edit-turn-k with the least
   machinery.

**Tried hard and couldn't break:** the central live-channel-at-level-2 claim. Given serialized
ops (fix C2), the auditor restarting *does* re-supply each command live, the rendezvous *does*
keep lock-step, and the target rebuilds deterministically provided replayed generates are
byte-identical (which `pending₂` guarantees for the model calls). The core is correct and
elegant; the holes are completeness and concurrency, not the idea.

---

*Key files: `resampling.md`; `petri-meridian/src/inspect_petri/target/_history.py`,
`_context.py`, `_channel.py`, `_agent.py` (origin/controller-refactor); `_auditor/agent.py`,
`compaction.py`; `_realism/approver.py`; `tools/_*.py`; `inspect_ai/model/_call_tools.py`,
`_model.py`, `_compaction/_compaction.py`, `tool/_tool.py`; `ARCHITECTURE.md`.*
