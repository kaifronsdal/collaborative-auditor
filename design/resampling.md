# Audit Resampling / Resume

**Status:** design · **Branch:** `controller-refactor`

## Problem

A user looking at a finished audit in the Inspect viewer wants to point at a turn and say *"regenerate from here, N times."* Everything before that point — the auditor's plan and tool history, the target's conversation and tools, the `M1..Mk` short-id map — is restored exactly, then both sides continue live.

- **Mode A** — resume from a Petri `.eval` log at a chosen message; both auditor and target context restored.
- **Mode B** — start from an arbitrary `list[ChatMessage]` for the target (e.g. a production transcript); the auditor gets a synthetic history that sets up the same conversation.

Petri already has record/replay for one level: the target's `Trajectory` records its calls and replays them on `rollback_conversation`. Resume is the same idea **one level up** — replay the whole audit's prefix, not just the target's. The design below generalises to `n` nested levels so both fall out of one mechanism.

---

## The model

### Levels

Fix `n ≥ 1` nested levels, numbered `1` (innermost) to `n` (outermost). **Branching at level `m`** means: re-execute everything at levels `≤ m` from scratch; levels `> m` continue unchanged on their existing history.

```
  level n   ┌─ outermost (held fixed when any inner level branches) ──────┐
  level n-1 │   ┌─ tree of branches; each re-runs levels ≤ n-1 ───────┐   │
   ...      │   │   ┌─ ... ──────────────────────────────────────┐    │   │
  level 1   │   │   │   ┌─ innermost; branches re-run level 1 ─┐ │    │   │
            │   │   │   └──────────────────────────────────────┘ │    │   │
            │   │   └────────────────────────────────────────────┘    │   │
            │   └─────────────────────────────────────────────────────┘   │
            └─────────────────────────────────────────────────────────────┘
```

### Calls

A **call** is one invocation of a wrapped function whose result we may need to record (a model API call, a channel read, …). Each call `c` has:

- `home(c)` — the innermost level at which `c` is wrapped.
- `nondet(c, k)` for each level `k ≥ home(c)` — true iff `c` depends on something that does **not** restart when level `k` branches. In other words: if we branch at `k` and run `c` live, could we get a different result than last time? If yes, `c` must be served from a recording at level `k`; if no, running it live reproduces the original result (because everything `c` depends on restarted too).

`nondet(c, k)` is the one thing each call must declare. Everything else is derived.

Examples:

- `model.generate` depends on a model API, which is outside every level → never restarts → `nondet(·, k) = T` for all `k`.
- `channel.next_command` depends on the auditor coroutine at level 2. Branching at level 1 leaves the auditor untouched → `nondet(·, 1) = T`. Branching at level 2 restarts the auditor too → it re-sends the same command → `nondet(·, 2) = F`.

### Tapes

Each level `k` has a **tape** `τₖ = (logₖ, pendingₖ)`:

- `logₖ : list[Step]` — the complete ordered record of values observed at level `k` during this run. Starts empty; appended on every wrapped call. This is the source for future replays at level `k`: branching at `k` takes a prefix `logₖ[:cut]` and uses it as the input to `pendingₖ` (e.g. `log₁` is sliced to seed a target rollback; `log₂` is persisted and sliced to seed an audit resume).
- `pendingₖ : deque[Step]` — values queued for replay, taken from a previous run's `logₖ` prefix. Consumed front-to-back; when empty, level `k` is "caught up" and goes live.

After branching at level `m`:

```
        level k:    1     2    …    m-1      m        m+1   …   n
   pendingₖ  :      ∅     ∅         ∅       seedₘ      ·         ·
                    └────── fresh ──────┘   └seeded┘  └─ unchanged ─┘
       logₖ  :      []    []        []       []        ·         ·
```

where `seedₘ` is a prefix of some previous run's `logₘ`.

---

## The mechanism

### Replay, recursively

A call `c` with `h := home(c)` is evaluated as `replayₕ(c)`:

```
  replayₙ₊₁(c)  :=  c()                                                  (base: live)

  replayₖ(c)    :=  v  ←  pendingₖ.popleft()    if  pendingₖ ≠ ∅  ∧  nondet(c, k)
                          replayₖ₊₁(c)          otherwise
                    logₖ.append(v)
                    return v
                                                                         (h ≤ k ≤ n)
```

In words: **level `k` serves `c` from its pending buffer iff `c` is nondeterministic at level `k`.** If `c` is deterministic at `k` (or `pendingₖ` is empty), level `k` is transparent — it recurses to `k+1` and just logs the result on the way back.

### Correctness

After branching at `m`, `pendingₖ = ∅` for `k < m` and `pendingₘ = seedₘ`, so `replayₕ(c)` recurses through `h, …, m-1` (all transparent) and reaches `m`. There:

| `nondet(c, m)` | `replayₘ` does | which is correct because |
|---|---|---|
| `T` | `pendingₘ.popleft()` — replay | re-running level `m` cannot reproduce `c`'s value, so it must come from the recording |
| `F` | recurse past `m` → … → `c()` live | re-running level `m` *does* reproduce `c`'s value (whatever `c` depends on also restarted), so running it live is correct — and required, since replaying would skip the live interaction the dependency is waiting on |

The argument is definitional: replay at `m` iff nondeterministic at `m`.

**Induction on `n`.** Adding level `n+1` extends the recursion by one frame. If `pendingₙ₊₁ = ∅` (i.e. branching at `m ≤ n`), `replayₙ₊₁` reduces to `v ← c(); logₙ₊₁.append(v); v` — the old base case plus a log append — so the `n`-level behaviour is preserved. If `m = n+1`, the table above applies directly.

### What `logₖ` ends up containing

`replayₖ` always appends to `logₖ`, whether the value came from `pendingₖ`, from `replayₖ₊₁`, or live. So after any run, `logₖ` is the **full sequence of values level `k` observed**, in order — replayed prefix (as shared `Step` references) followed by fresh values. This means:

- `logₖ` is what you persist for later resume at level `k`.
- An inner level's branch tree is *not* stored in `logₖ` — it is reconstructed during a level-`k` replay because the replayed values include whatever triggered those inner branches (e.g. an auditor `ModelOutput` whose tool calls contain `rollback_conversation(...)`).
- When level `j < k` replays from its own `pendingⱼ`, `replayⱼ` returns before reaching `replayₖ`, so `logₖ` is **not** appended — each value appears in `logₖ` exactly once across all inner-level branches.

---

## The n ↔ n±1 simplification

If level `k` only communicates with levels `k±1`, then for any call `c` there is a **boundary** `b(c) ∈ {h+1, …, n+1, ∞}` such that

```
  nondet(c, k)  ⟺  k < b(c)
```

— `c` is nondeterministic exactly at levels below its boundary. Two cases cover everything:

- `b(c) = ∞` (**internal**): `c` depends on something outside all levels (model API, RNG). Nondeterministic everywhere.
- `b(c) = h+1` (**external**): `c` depends on level `h+1` (a channel op). Nondeterministic only at `h`; at every `k > h` the dependency restarts too, so `c` is reproduced live.

So under n±1, `nondet(c, k) = internal(c) ∨ k = home(c)`, and the per-call declaration collapses to one bit `external(c)`. Petri satisfies n±1; if a level-skipping call ever appears, pass `b(c)` instead.

---

## Branching within a level

A level that supports branching (Petri's level 1) keeps a **tree of nodes**, one per branch:

```python
@dataclass
class Node:
    tape: Tape
    parent: Node | None = None
    branched_from: str | None = None
    children: list[Node] = field(default_factory=list)
    span_id: str = field(default_factory=uuid)
```

Because `logₖ` is the full root→here lineage (replayed prefix + fresh), branching is a slice:

```python
def branch(current: Node, at: str) -> Node:
    src = current if has(current, at) else find_node(root_of(current), at)
    prefix = src.tape.log[: cutoff(src.tape.log, at) + 1] if at else []
    child = Node(Tape(pending=deque(prefix)), parent=current, branched_from=at)
    current.children.append(child)
    return child
```

The tree exists only so `find_node` can locate `at` in a sibling branch (cross-branch rollback). It is in-memory only; on an outer-level replay it is rebuilt from scratch.

**`cutoff`** picks where to end the prefix so the next replayed call is a safe re-sync point — a call whose `nondet(c, k) = T` only at `k = h` and which *blocks* on level `h+1` (i.e. an incoming-from-`h+1` call). In code we tag such calls `sync = "in"` and `cutoff` advances from the last `message_id == at` step to just before the next `sync == "in"` step. This subsumes the old `deterministic` flag.

---

## Petri's two levels

```
              ┌──────────── level 2: audit (restarts on resume) ────────────┐
              │             τ₂ : Tape  (single, both tasks)                  │
              │              ▲ via τ₂.wrap            ▲ via τ₂.replayable    │
   ┌──────────┴──────────────┴──┐         ┌────────────┴────────────────────┤
   │ target coroutine            │         │ auditor coroutine               │
   │  ┌─ level 1: Node tree ──┐  │         │  (home = 2; no level-1 tape)    │
   │  │ restarts on rollback  │  │         │                                 │
   │  └───────────────────────┘  │         │                                 │
   └──────────────────┬──────────┘         └─────────┬───────────────────────┘
                      └────────── Channel (live) ────┘
```

| call `c` | `home(c)` | `nondet(c, 1)` | `nondet(c, 2)` | why |
|---|---|---|---|---|
| `target_model.generate` | 1 | T | T | model is outside all levels |
| `channel.next_command` | 1 | T | **F** | depends on auditor (level 2); on a level-2 branch the auditor restarts and re-supplies it live |
| `channel.send_response` | 1 | T | **F** | same |
| `auditor_model.generate` (and realism/compaction model calls) | 2 | — | T | model is outside all levels |
| `channel.request` (auditor side) | — | — | — | not wrapped: depends on level 1 which is *inside* level 2, so deterministic at 2; auditor has no level below 2 to record at |

**Single `τ₂`, both tasks.** The auditor and target coroutines are serialized by the channel rendezvous, so their accesses to `τ₂` are in a fixed order. Target's only `pending₂` reads are for `target.generate` (`nondet(·, 2) = T`), which cannot happen until after the rendezvous (target must receive `Resume` first), so the auditor always drains its prefix first regardless of scheduler order.

**During a level-2 replay the channel runs live.** `next_command` / `send_response` have `nondet(·, 2) = F`, so `replay₂` is transparent for them — `replay₃ = c()` runs the real channel I/O. The rendezvous keeps the coroutines lock-step; `MessageMap`, `controller().state`, the level-1 `Node` tree, and tool definitions all rebuild themselves. Only model calls come from `pending₂`.

---

## Implementation

### `Tape`

```python
Sync = Literal["in", "out"] | None        # external(c) ⟺ sync ≠ None; "in" marks re-sync points for cutoff
Replayable = Callable[..., Callable[..., Awaitable[Any]]]

@dataclass(slots=True)
class Step:
    value: Any | None       # None where nondet(c, k) = F (value not needed for replay there)
    source: str             # fn.__qualname__ — desync guard
    sync: Sync
    message_id: str | None  # branch/truncation key, from message_id_of(value)


class Tape:
    def __init__(self, pending: Iterable[Step] = ()) -> None:
        self.log: list[Step] = []
        self.pending: deque[Step] = deque(pending)
        self.replayable: Replayable = self._serve     # instance attr; reassign via wrap()

    def pop(self, src: str, sync: Sync) -> Step | None:
        if not self.pending:
            return None
        s = self.pending.popleft()
        if s.source != src or s.sync != sync:
            raise ReplayDesyncError(src, sync, s.source, s.sync)
        return s

    def _serve(self, fn, *, sync: Sync = None):
        """nondet(c, k) = T at this level: serve from pending if available, else live; log full value."""
        src = fn.__qualname__
        @functools.wraps(fn)
        async def w(*a, **kw):
            if (s := self.pop(src, sync)) is not None:
                self.log.append(s)
                return isolate(s.value)
            v = await fn(*a, **kw)
            self.log.append(Step(isolate(v), src, sync, message_id_of(v)))
            return v
        return w

    def _mark(self, fn, *, sync: Sync):
        """nondet(c, k) = F at this level: never serve; log metadata only."""
        src = fn.__qualname__
        @functools.wraps(fn)
        async def w(*a, **kw):
            v = await fn(*a, **kw)
            self.log.append(Step(None, src, sync, message_id_of(v)))
            return v
        return w

    def wrap(self, inner: Replayable) -> Replayable:
        """Return a Replayable where `self` is one level outer than `inner`'s home."""
        def composed(fn, *, sync: Sync = None):
            outer = self._serve(fn) if sync is None else self._mark(fn, sync=sync)
            return inner(outer, sync=sync)
        return composed
```

`nondet(c, k)` is never a parameter — it is determined by *how* the tape is reached:

| route | `nondet(c, k)` | primitive |
|---|---|---|
| `tape.replayable` (the instance attr, used directly) | `T` — this is the home level | `_serve` |
| via `wrap`, `sync is None` | `T` — internal calls are nondet at every level | `_serve` |
| via `wrap`, `sync ≠ None` | `F` — external calls are det at every outer level | `_mark` |

Nesting is composed by reassigning `replayable` on the home tape:

```python
L1.replayable = L2.wrap(L1.replayable)
# N levels:
for outer in (L2, ..., Ln):
    L1.replayable = outer.wrap(L1.replayable)
```

`L1.replayable(fn, sync=S)` then expands to `L1._serve(L2.{_serve|_mark}(...Ln.{_serve|_mark}(fn)...))` — exactly `replayₕ` from the formalism. `L2.replayable` itself remains `L2._serve`, so code at home level 2 (the auditor) uses it directly.

### Isolation

`isolate = copy.deepcopy`. `replayable` deep-copies on the value boundary (stored and returned) so `Step.value` is never aliased with caller-held objects. Immutable atoms (`str`, `int`) are shared by `deepcopy`; only mutable shells (lists, pydantic models) are duplicated. Identity-keyed singletons (`Slot`) override `__deepcopy__` to return `self`.

### Seeding

`Tape(pending=seed)` with `log=[]`. `seed` for level `k` is a prefix of a previous run's `logₖ`, **filtered to steps with `value is not None`** — only steps that were nondeterministic at `k` are replayable there; `_mark` steps (`value=None`) are present in `logₖ` for truncation lookup but are never served, so they are dropped from `pending`. After replay `logₖ = [popped refs…, fresh…]` — re-persistable as-is.

### Petri wiring

```python
# audit_solver
τ₂ = Tape(pending=(s for s in seed if s.value is not None))

auditor_model = ReplayingModel(get_model(role="auditor"), τ₂)   # wraps .generate / .count_tokens via τ₂.replayable
realism_model = ReplayingModel(get_model(role="realism"), τ₂)

# target task — TargetContext does the wrap; target/_agent.py is unchanged
root = Node(Tape())
node = root
while node is not None:
    state = AgentState(messages=[])
    ctx = TargetContext(ch, node, τ₂, messages=state.messages, tools=tools)
    try:
        async with span(id=node.span_id, ...):
            await target(state, ctx)
        node = None
    except RollbackSignal as rb:
        node = branch(node, rb.message_id)
```

`TargetContext.__init__(channel, node: Node, *outer: Tape, messages, tools)` keeps `self._node` (for cross-branch rollback validation via `find_node(root_of(self._node), at)`) and composes in place:

```python
for t in outer:
    node.tape.replayable = t.wrap(node.tape.replayable)

self._next_command  = node.tape.replayable(channel.next_command,  sync="in")
self._send_response = node.tape.replayable(channel.send_response, sync="out")

def replayable(self, fn) -> ...:
    return self._node.tape.replayable(fn)   # sync=None
```

Each `Node` is constructed and wrapped exactly once (fresh `Tape` per branch), so there is no double-wrapping.

### Persistence

`AuditTape(StoreModel)` with `steps: list[SerializedStep]`, `config_digest`, `seed_instructions`, `today_date`. `SerializedStep = {source, sync, message_id, value}` where `value: ModelOutput | int | float | None`. Steps with `nondet(·, 2) = F` have `value = None`, so `Slot`/`Stage`/`Command` never need serialising. `config_digest` (hash of `realism_filter`, `compaction`, `eager_resume`, `target_tools`, `skills`, `max_turns`, `system_message`, `user_message`) is checked on resume so misconfiguration fails fast instead of desyncing mid-run.

### Auditor-side determinism

Same discipline as the target — every nondeterministic call is wrapped:

- model calls via `ReplayingModel(model, τ₂)` (subclasses/proxies `Model` so inspect's `isinstance` checks pass; threaded through new optional `model=` params on `auditor_agent`, `resolve_compaction`, `realism_approver`, `auditor_approval`).
- `_eager_resume_inject(output, turn)`: deterministic id `f"eager-resume-{turn}"`, returns a copy.
- `skills.py`: stable temp dir.
- `today_date`: read from `AuditTape`, not `datetime.now()`.

---

## Entry points

```python
@task
def resample(ref: str, at: str | None = None, **audit_kwargs): ...
```

```
inspect eval inspect_petri/resample -T ref=<viewer-url-or-log-path> -T at=<msg-id> --epochs 10
```

- `parse_viewer_ref(ref)` → `(log_path, sample_id, message_id)`; accepts a viewer URL (`…?event=<uuid>` / `…?message=<id>`), a bare log path, or `<log>#<sample>@<id>`.
- `load_tape(log, sample_id)` → `list[SerializedStep]` from `EvalSample.store`.
- `truncate_at(steps, message_id)` → `steps[:idx]` (exclusive — the named message is the one regenerated).
- **Mode B**: `tape_from_messages(messages, tools)` — synthesise auditor `ModelOutput` steps with `tool_calls = [set_system_message, create_tool*, send_message, resume]` (turn 0) or `[send_tool_call_result*, send_message?, resume]` (later); target steps wrap each assistant message; sync placeholders carry `value=None` and the message ids from `messages`.

The `resample` task builds a single-sample dataset with `metadata={"resume": steps}` and delegates to `audit_solver`. `audit(..., resume=...)` is also accepted directly.

---

## Out of scope / migration

- **Dish**: subprocess `prompt()`/`ready.wait()` are unwrapped; a fresh subprocess on resume diverges → `ReplayDesyncError`. Resume support requires wrapping those.
- **Tests**: ~50 sites in `tests/target/` construct `TargetContext(channel, Trajectory())` and `_Step(..., deterministic=True)`; provide a `Trajectory`/`History` shim or bulk-migrate.
- **Unchanged**: `target/_agent.py`, `target/_channel.py`, `target/_controller.py`.
