# Audit Resampling / Resume

**Status:** landed (PR #110) · **Branch:** `tape-replay`

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
class Node:
    span_id: str
    branched_from: str | None
    parent: Node | None
    children: list[Node]
    prefix_len: int          # len(pending) at construction — log[:prefix_len] is the replayed prefix
    tape: Tape
```

`prefix_len` is fixed at construction; `tape.log[prefix_len:]` is what this branch recorded *fresh*. `Node.message_in_steps(id)` checks only the fresh suffix, so for any key there is exactly one **origin** node — the one that first recorded it. Branching always parents the new node at the origin (N resamples at M produce N siblings, not a chain) and slices the prefix from the origin's log:

```python
def History.branch(message_id: str, from_node: Node) -> Node:
    if message_id == "":
        return Node(branched_from="", parent=root, pending=[])
    origin = self._locate(message_id, from_node)      # walk lineage, then BFS from root
    cutoff = _find_cutoff(origin.tape.log, message_id)
    return Node(branched_from=message_id, parent=origin, pending=origin.tape.log[: cutoff + 1])
```

Slicing `origin.tape.log` (not `from_node.tape.log`) is required: the origin's log is complete through M by definition, but the current node's replayed prefix may not carry M forward (a restart branch has `prefix_len=0`). `_locate` returns just the origin `Node`; the tree exists only so `_locate` can find it in a sibling branch (cross-branch rollback). It is in-memory only — on an outer-level replay it is rebuilt from scratch.

**`cutoff`** picks where to end the prefix so the next replayed call is a safe re-sync point — a call whose `nondet(c, k) = T` only at `k = h` and which *blocks* on level `h+1` (i.e. an incoming-from-`h+1` call). In code we tag such calls `boundary = "in"` and `cutoff` advances from the last `message_id == at` step to just before the next `boundary == "in"` step. This subsumes the old `deterministic` flag.

---

## Petri's two levels

```
              ┌──────────── level 2: audit (restarts on resume) ────────────┐
              │           audit_tape : Tape  (single, both tasks)            │
              │   ▲ via node.tape.compose(audit_tape)  ▲ via tape.replayable │
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

**Single `audit_tape`, both tasks.** The auditor and target coroutines are serialized by the channel rendezvous, so their accesses to `audit_tape` are in a fixed order. Target's only `pending₂` reads are for `target.generate` (`nondet(·, 2) = T`), which cannot happen until after the rendezvous (target must receive `Resume` first), so the auditor always drains its prefix first regardless of scheduler order.

**During a level-2 replay the channel runs live.** `next_command` / `send_response` have `nondet(·, 2) = F`, so `replay₂` is transparent for them — `replay₃ = c()` runs the real channel I/O. The rendezvous keeps the coroutines lock-step; `MessageMap`, `controller().state`, the level-1 `Node` tree, and tool definitions all rebuild themselves. Only model calls come from `pending₂`.

---

## Implementation

### `Tape`

```python
Boundary = Literal["in", "out"] | None    # external(c) ⟺ boundary ≠ None; "in" marks re-sync points for cutoff

class Replayable(Protocol):
    def __call__(self, fn, *, boundary: Boundary = ..., source: str | None = ...) -> Callable[..., Awaitable]: ...

@dataclass(frozen=True, slots=True, eq=False)
class Step:
    value: Any | None       # None where nondet(c, k) = F (value not needed for replay there)
    source: str             # desync-guard key — fn.__qualname__ unless overridden
    boundary: Boundary = None
    message_id: str | None = None   # branch/truncation key

    def dump(self) -> dict: ...     # ModelOutput | int | float | str | None — the only persisted-tape values
    @staticmethod
    def load(d: dict) -> Step: ...


class Tape:
    def __init__(self, pending: Iterable[Step] = ()) -> None:
        self.log: list[Step] = []
        self.pending: deque[Step] = deque(pending)

    def replayable(self, fn, *, boundary: Boundary = None, source: str | None = None):
        """nondet(c, k) = T at this level: serve from pending if available, else live; log full value.

        The single-level public entry point — symmetric with `TargetContext.replayable`."""
        src = source if source is not None else fn.__qualname__
        @functools.wraps(fn)
        async def w(*a, **kw):
            if self.pending:
                s = self.pending.popleft()
                if s.source != src or s.boundary != boundary:
                    raise ReplayDesyncError(...)
                if s.value is None:
                    raise ReplayDesyncError(...)   # seed must be filtered to value-bearing steps
                self.log.append(s)
                return isolate(s.value)
            v = await fn(*a, **kw)
            self.log.append(Step(isolate(v), src, boundary, _extract_message_id(v)))
            return v
        return w

    def compose(self, *outer: Tape) -> Replayable:
        """Build the Replayable for calls whose home level is `self`, with `outer` tapes above it."""
        r: Replayable = self.replayable
        for t in outer:
            r = t._wrap(r)
        return r

    def _mark(self, fn, *, boundary: Boundary, source: str | None = None):
        """nondet(c, k) = F at this level: never serve; log metadata only. Reached only via compose."""
        src = source if source is not None else fn.__qualname__
        @functools.wraps(fn)
        async def w(*a, **kw):
            v = await fn(*a, **kw)
            self.log.append(Step(None, src, boundary, _extract_message_id(v)))
            return v
        return w

    def _wrap(self, inner: Replayable) -> Replayable:
        """One level of compose: apply this tape's serve-or-mark (by boundary), then inner."""
        def composed(fn, *, boundary: Boundary = None, source: str | None = None):
            o = self.replayable(fn, source=source) if boundary is None else self._mark(fn, boundary=boundary, source=source)
            return inner(o, boundary=boundary, source=source)
        return composed
```

`nondet(c, k)` is never a parameter — it is determined by *how* the tape is reached:

| route | `nondet(c, k)` | primitive |
|---|---|---|
| home level (`compose` starts at `self.replayable`) | `T` — this is the home level | `replayable` |
| via `_wrap`, `boundary is None` | `T` — internal calls are nondet at every level | `replayable` |
| via `_wrap`, `boundary ≠ None` | `F` — external calls are det at every outer level | `_mark` |

Nesting is composed **purely** — there is no composition state on `Tape`, so double-composition is structurally impossible (no `_wrapped` guard needed):

```python
replay₁ = L1.compose(L2, ..., Ln)      # home = L1
replay₂ = L2.compose(..., Ln)          # home = L2 (auditor uses this)
```

`replay₁(fn, boundary=B)` expands to `L1.replayable(L2.{replayable|_mark}(...Ln.{replayable|_mark}(fn)...))` — exactly `replayₕ` from the formalism. `L2.compose()` with no outer is just `L2.replayable`, so code at home level 2 (the auditor) calls `audit_tape.replayable(fn, source=…)` directly.

`Step` is frozen and has no `__post_init__` — `replayable`/`_mark` compute `message_id` (via `_extract_message_id(value)`, which reads `value.message.id` or `value.value.id`) and pass it explicitly. `source` defaults to `fn.__qualname__` but takes an optional override so two wrapped functions sharing a qualname (e.g. several `Model.generate` instances on one tape) can be distinguished by the desync guard. Serialization lives on `Step` itself (`dump()`/`load()`); there is no separate `SerializedStep` type.

### Isolation

`isolate = copy.deepcopy`. `replayable` deep-copies on the value boundary (stored and returned) so `Step.value` is never aliased with caller-held objects. Immutable atoms (`str`, `int`) are shared by `deepcopy`; only mutable shells (lists, pydantic models) are duplicated. Identity-keyed singletons (`Slot`) override `__deepcopy__` to return `self`.

### Seeding

`Tape(pending=seed)` with `log=[]`. `seed` for level `k` is a prefix of a previous run's `logₖ`, **filtered to steps with `value is not None`** — only steps that were nondeterministic at `k` are replayable there; `_mark` steps (`value=None`) are present in `logₖ` for truncation lookup but are never served, so they are dropped from `pending` (and `replayable` raises if one slips through). After replay `logₖ = [popped refs…, fresh…]` — re-persistable as-is.

### Petri wiring

```python
# audit_solver
audit_tape = Tape(pending=_read_resume_seed(state))   # filtered to value-bearing
init_audit_tape(audit_tape)                           # contextvar — audit_tape() reads it
history = History()

# target task — TargetContext composes; target/_agent.py is unchanged
node: Node | None = history.root
while node is not None:
    state = AgentState(messages=[])
    ctx = TargetContext(ch, node, audit_tape, messages=state.messages, tools=tools)
    try:
        async with span(id=node.span_id, ...):
            await target(state, ctx)
        node = None
    except RollbackSignal as rb:
        node = history.branch(rb.message_id, node)
```

`TargetContext.__init__(channel, node: Node, *outer: Tape, messages, tools)` keeps `self._node` (for cross-branch rollback validation via `node.message_in_history(id)`) and builds the composed `Replayable` once, held on the context (not on any `Tape`):

```python
self._replay        = node.tape.compose(*outer)
self._next_command  = self._replay(channel.next_command,  boundary="in")
self._send_response = self._replay(channel.send_response, boundary="out")

def replayable(self, fn) -> ...:
    return self._replay(fn, boundary=None)
```

The auditor side reaches `audit_tape` via a contextvar rather than parameter threading: `audit_solver` calls `init_audit_tape(audit_tape)`, and `auditor_agent.execute` wraps the *function*, not the model object — symmetric with `context.replayable()`:

```python
agent_model = get_model(role="auditor")
tape = audit_tape()
generate = (
    tape.replayable(agent_model.generate, source="auditor:Model.generate")
    if tape is not None
    else agent_model.generate
)
```

There is no `ReplayingModel` subclass and no `recording()` helper — the auditor and target use the same one wrapping pattern (`tape.replayable(fn, source=…)`). When `audit_tape()` returns `None` (running `auditor_agent` standalone outside `audit_solver`), `agent_model.generate` is used unwrapped with no behaviour change. Compaction's summarisation generate is wrapped the same way so it also lands on the tape. **Note (tape-replay-v2):** `auditor_agent` is called with `compaction=False` in the workbench and resumable-audit flows; the summariser tape-wrap is therefore not exercised on those paths. The `resample` task should also default to `compaction=False` or document this limitation.

### Persistence

`AuditTape(StoreModel)` with `steps: list[dict]`, `seed_instructions: str`, `today_date: str`, `config_digest: str`, `synthetic: bool`. Each dict is `Step.dump() = {source, boundary, message_id, value}` where `value: ModelOutput | int | float | str | None`. Steps with `nondet(·, 2) = F` have `value = None`, so `Slot`/`Stage`/`Command` never need serialising. `audit_solver` writes `AuditTape().steps = [s.dump() for s in audit_tape.log]` in its `finally` block.

`config_digest = config_digest(max_turns, eager_resume, realism_filter, compaction is not False, system_message, user_message, auditor_model.name, target_model.name)` is computed in `auditor_agent.execute`: stored on first run, compared on resume so a mismatch raises `ValueError` immediately instead of desyncing mid-replay. `synthetic=True` is set by `tape_from_messages` so a `ReplayDesyncError` on a synthetic tape can give a "your message list doesn't match `target_agent`'s loop shape" diagnosis.

### Auditor-side determinism

Same discipline as the target — every nondeterministic call goes through the tape:

- auditor model calls via `tape.replayable(agent_model.generate, source="auditor:Model.generate")`.
- `_eager_resume_inject(output, turn)`: deterministic id `f"eager-resume-{turn}"`; mutates `output.message.tool_calls` in place *after* `replayable` has deep-copied into the tape, so the recorded value is the pre-injection output and replay reproduces the same injection.
- `skills.py`: stable temp dir.
- `today_date`: read from `AuditTape().today_date` (set by `_read_resume_seed` on resume, `today_default()` on first run), not `datetime.now()`.
- `seed_instructions`: persisted on `AuditTape` so the resample task can re-use it as the sample input.

---

## Entry points

```python
@task
def resample(ref: str, at: str | None = None, **audit_kwargs) -> Task: ...
```

```
inspect eval inspect_petri/resample -T ref=<viewer-url-or-log-path> -T at=<msg-id> --epochs 10
```

- `parse_viewer_ref(ref) -> (log_path, sample_id, message_id)`; accepts an inspect-view "copy link" URL (hash-routed: `…#/logs/<log>/samples/sample/<sid>/<epoch>/<tab>?message=<mid>` or `?event=<eid>`), `<log>#<sample>@<id>`, or a bare log path.
- `load_tape(log, sample_id=None) -> AuditTape` — reads `sample.store_as(AuditTape)` from one sample of a `.eval` log; `sample_id=None` reads the first sample.
- `truncate_serialized(steps: list[dict], at: str | None) -> list[dict]` — `steps[:idx]` (exclusive — `at` is the message regenerated); `at=None` returns the full list (resume at the end and continue live). In-process branching uses `_find_cutoff` directly via `History.branch`; there is no separate exported `truncate_at`.
- **Mode B**: `tape_from_messages(messages, *, tools=(), seed_instructions="", today_date="") -> AuditTape` — synthesise auditor `ModelOutput` steps with `tool_calls = [set_system_message, create_tool*, send_message, resume]` (turn 0) or `[send_tool_call_result*, send_message?, resume]` (later); target steps wrap each assistant message; channel `_mark` placeholders carry `value=None` and the message ids from `messages`. Returns a tape with `synthetic=True`.

The `resample` task builds a single-sample dataset with `metadata={"resume": seed, "today_date": ..., "config_digest": ..., "synthetic": ...}` (where `seed` is already the `Step.dump()` dict list from `truncate_serialized`) and delegates to `audit_solver`; `_read_resume_seed` lifts those into `AuditTape()` and returns the value-bearing steps (via `Step.load`) for `Tape(pending=...)`.

---

## Out of scope / migration

- **Dish**: subprocess state (working directory, open files, session) lives outside the staging replay and cannot be restored by it. The right path is the scaffold's own session-resume (e.g. `claude --resume <session>`) on the target side, with `tape_from_messages` supplying the auditor-side prefix.
- **Tests**: ~50 sites in `tests/target/` construct `TargetContext(channel, Trajectory())` and `_Step(..., deterministic=True)`; provide a `Trajectory`/`History` shim or bulk-migrate.
- **Unchanged**: `target/_agent.py`, `target/_channel.py`, `target/_controller.py`.
