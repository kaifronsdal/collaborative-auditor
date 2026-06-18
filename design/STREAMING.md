# Streaming model generation for the workbench UI

*Design memo, 2026-06-16. Grounded in inspect_ai main, petri-meridian `tape-replay`,
collaborative-auditor. The question: how do tokens reach the UI as the model generates, and
how does that compose with `tape.replayable`?*

**Decision (2026-06-16, revised): event-mutation, not a callback.** Providers stream by
mutating the **pending `ModelEvent.output`** (already reachable via `_active_model_event`
ContextVar) and calling `transcript()._event_updated(event)` per coalesced flush. This is the
*existing* `set_active_model_event_call` pattern (`log/_samples.py:415-430`, already called
mid-generate by the Anthropic provider) applied to `output`. No new public API; the live-view
buffer subscriber (`run.py:1063`) already consumes `_event_updated`, so **inspect-view shows
streaming for free**. One provider stack (inspect's); both auditor and target stream through
the same path. The §"What sonde provides" section below is kept as design reference for delta
shapes only.

---

## What inspect_ai provides today — no streaming seam

`Model.generate` is single-awaited, fully-materialized. No `stream=` param
(`_model.py:673-680`), no streaming on `ModelAPI.generate` (`:303-310`), no `GenerateConfig`
field. Providers consume SDK streams **internally** (Anthropic uses `client.messages.stream`
gated by reasoning/`max_tokens≥8192`, `anthropic.py:785-789,556-560`; Google opt-in; OpenAI
doesn't stream at all) and collapse to a final message — no per-token hook. `ModelEvent`
carries the complete output; the only seam is whole-call `on_pending`/`on_complete`. The
Textual viewer skips pending events entirely (`transcript.py:293-294`).

**The plumbing exists; the wire-up doesn't.** Providers already hold the pending `ModelEvent`
(via `_active_model_event`) and `_event_updated` already notifies the live-view buffer — the PR
is connecting "SDK chunk arrived" → "mutate `event.output` → `_event_updated`" inside loops the
providers already have. Both auditor and target stream through the same path once that lands.

## What sonde provides — the API we adopt

`Provider.stream` is an **async generator** (`providers/base.py:53-74`) yielding a
`SurfaceEvent` discriminated union (`events.py:235-248`): `item.added`, `item.updated`
(`assistant_text_delta`/`assistant_thinking_delta`/`tool_input_delta`), `item.done`, `notice`,
`error`, `rate_limit`. Providers translate native SDK streams (`anthropic.py:196-282` etc.).
Transport is SSE + a server-side `apply_to_node` accumulator (`base.py:77-114`) folding deltas
into the persisted Node; partials persist on disconnect; a property test asserts the
accumulator equals the SDK's own `get_final_message()`.

**Reference only.** Sonde's `SurfaceEvent` delta shape informed how to think about chunk
kinds; the layer itself is not adopted (DESIGN §3.5 — one provider stack, inspect's).

## Approach — inspect PR: mutate the pending `ModelEvent` from inside the provider stream

The pending `ModelEvent` is bound *before* `api.generate` (`_model.py:1115`) and held in
`_active_model_event` ContextVar across the call (`log/_samples.py:397-408`) — provider code
can already reach it. The Anthropic SDK exposes an accumulating `stream.current_message_snapshot`
on every iteration. The PR adds, inside the existing stream loop (`anthropic.py:~3647`):

```python
if _should_flush(sdk_event):                   # ~10Hz coalesce on content_block_delta
    me = _active_model_event.get()
    if me is not None:
        partial, _ = await model_output_from_message(
            client, model_name, stream.current_message_snapshot, tools, ...)
        me.output = partial
        transcript()._event_updated(me)
```

`_event_updated` (`_transcript.py:594-605`) fans out to all subscribers — including the
live-view buffer subscriber (`_eval/task/run.py:1063-1088`) → SQLite sample_buffer →
inspect-view's poll. **No `GenerateConfig` change, no `ModelAPI.generate` change.** Same
pattern for Google (`generate_content_stream`) and OpenAI (enable `stream=True`, add the loop).
Throttle lives provider-side in `_should_flush`.

`tape.replayable` is unaffected: it wraps `model.generate`, which still returns the final
`ModelOutput`; the streaming is a side-effect inside that call.

## Workbench consumption

The workbench subscribes to `transcript()` (private `_subscribe`, on the upstream-ask list)
or, when running as an inspect task, gets the same updates via the buffer. Per update it
**replaces** the events array (the ts-mono renderer memoizes on identity, not deep-equals —
`TranscriptVirtualList.tsx:318`); the active node's text is whatever `event.output` currently
holds. The 10 Hz throttle is provider-side, so the workbench just renders what arrives.

## ts-mono — reference, not dependency

`@tsmono/inspect-components` was evaluated and **not adopted as a dependency**. It's reusable
in principle (`ChatMessageRow`/`ModelEventView`/`TranscriptViewNodes`, prop-driven) but: no npm
(git submodule + pnpm workspace, raw `.ts` exports), Bootstrap CSS baseline + heavy peer deps
(`react-router-dom`/`react-virtuoso`/`prismjs`/`mathjax`/`@vscode-elements`), no per-message
action slot (`RenderedEventNode` is a hardcoded switch — we'd compose our own row anyway), and
re-theming Bootstrap to the claude.ai grammar (serif, one-column, accent-dot) would eat the
savings. Once we're building our own row, the only reuse is content-block rendering — and a
~1-2 day switch over `ContentText`/`ContentReasoning`/`ContentImage`/`ToolCall` with
`react-markdown` + `prism-react-renderer` is cheaper than the dependency cost.

**Kept as a reference** for edge-case handling: truncated tool output, reasoning toggles,
attachment resolution. The workbench renders inspect's *event types* directly (which we already
consume via petri) with its own components per UI.md.

**UI hop:** do **not** emit a JSON-Patch op per token — `push_view_state` diffs the full
serialized state (`common.py:71-93`, O(branches×messages); ARCHITECTURE.md melt-risk). Instead:
coalesce deltas in the sink at ~10 Hz, each flush emits one targeted append op on the active
node's streaming-text field (the store knows which node changed — emit the op directly, don't
diff). The final `ModelOutput` push reconciles. Needs one ViewState addition: an
"active-generation" buffer field on the node (`status: streaming`, like sonde's
`AssistantMessageItem.status`).

**Both auditor and target stream via the same hook** — there's no longer a target-first /
auditor-deferred split.

## Replay appears instantly — no fake-stream

On replay `tape.replayable` serves from `pending` without awaiting the fn — the sink never
fires. Streaming masks generation latency; on replay there is none. Fake-streaming would add
latency, make branch forks crawl through already-seen content, require fabricated timing, and
conflate "settled" with "generating now." Replayed content *is* settled; instant rendering says
so. (A client-side cosmetic reveal animation, if anyone wants one, is independent of this.)

## Effort — ~2 days for streaming; ts-mono integration is its own M0/M1 line

- **inspect PR:** per-provider flush hook in the existing stream loop + force streaming on
  when `_active_model_event` is set; make `Transcript.subscribe` public; mockllm grows a
  `stream_chunks` mode. **~1–1.5 d.** Low risk — `set_active_model_event_call` is the existing
  precedent; providers already produce the final `ModelOutput`.
- **Workbench data layer:** subscribe → replace events array → render. **~0.5 d.**
- **inspect-view shows streaming for free** (no workbench-side work for that).
- The frontend (built from scratch over inspect's event types, §3.5a) consumes the
  accumulating `event.output` by replacing the events array per update.

## Tests — deterministic, no sleeps

- `FakeStreamProvider` implementing sonde's `stream` async generator, yielding a scripted
  `SurfaceEvent` list gated by `asyncio.Event`s the test releases. Assert delta 1 received
  *and task not done* → release → delta 2 → release → done + final equals concatenation.
  Ordering proven by control flow, not wall-clock.
- Tape composition: wrap the streaming agent in `context.replayable`; assert one `Step`
  recorded with the accumulated `ModelOutput`. Replay: provider call-count 0, sink call-count 0.
- Accumulation fidelity: port sonde's property test (accumulator == SDK `get_final_message()`).
- Coalescer: fake clock, N tokens in one window → one patch op; final flush emits remainder.
- ViewState patch shape: assert one targeted append op, not a full-state replace.

---

*Key files:* inspect `_model.py:303-310,673-680`, `anthropic.py:785-789`,
`transcript.py:293-294`; sonde `providers/base.py:53-114`, `events.py:159-248`,
`server.py:868-996`; petri `_history.py:137-183`, `_agent.py:16`, `agent.py:126-130`;
workbench `DESIGN.md:306-323`, `ARCHITECTURE.md:378-383`; collaborative-auditor
`view_state.py:67-102`, `common.py:71-93`.

---

## §B — Backend wire architecture

One `Session` per `session_id`. The session owns a single `Transcript`, subscribes once via `_subscribe`, and forwards every inspect `Event` to all connected WebSockets. The only type-specific handling is `ModelEvent`: its `input` (full conversation history, re-sent on every streaming flush) is condensed into an append-only, content-hash-deduped message `pool` and replaced by `input_refs` range lists. The frontend resolves those ranges against the pool with `expandEvents`.

```python
# Session._on_event — sync, fast, no I/O
def _on_event(ev: Event) -> None:
    is_update = ev.uuid in self.events        # ← dedup by uuid, not a `seen` set
    if isinstance(ev, ModelEvent) and not is_update:
        _emit_pool_delta()                    # ship any new pool entries first
    self.events[ev.uuid] = condense(ev)
    self.version += 1
    self._send.send_nowait({"t": "update" if is_update else "event", ...})
```

Key invariants:
- One drain task per session (not per branch): started in `Session.start()`, consumes `_send` queue, broadcasts to all connections. Multiple branches share this one loop.
- `pool` is append-only; the frontend reconstructs it from `pool` deltas sent before the first `ModelEvent` that references new entries.
- `span_role` maps span ids to `(branch_id, role)` for column routing. Populated by `Branch.__init__` before the branch task runs.

## §C — Wire protocol (Down messages, client ← server)

### `Down.state` — full snapshot on connect or after `start`/`branch`/`edit`/`switch`

```typescript
{
  t: "state";
  v: number;                        // monotone version
  pool: ChatMessage[];              // full accumulated pool
  events: Event[];                  // all events (ModelEvent.input condensed to input_refs)
  span_role: Record<string, [BranchId, Role]>;
  queued: QueuedMap;                // per-branch, per-role injected messages awaiting next turn
  current: string | null;           // currently-viewed branch id
  status: Status | null;            // lifecycle of the current branch
  branches: Record<BranchId, BranchMeta>;  // tree metadata for all branches
}
```

### Incremental update messages

| `t` | payload | when |
|---|---|---|
| `pool` | `{v, from, entries}` | new pool entries before a new ModelEvent |
| `event` | `{v, event}` | new event (first time seen) |
| `update` | `{v, event}` | existing event mutated (streaming flush) |
| `queued` | `{v, branch, role, message}` | user-injected message enqueued |
| `status` | `{v, status}` | current branch lifecycle changed (play/pause/end) |
| `error` | `{v, message}` | operation failed (slice_at ValueError, inject into ended branch, etc.) |

### `Up` commands (client → server)

| `t` | params | notes |
|---|---|---|
| `start` | `seed, auditor_model, target_model, max_turns` | creates root branch, sets `current` |
| `step` | — | release one auditor turn |
| `play` | — | run freely |
| `pause` | — | stop after current turn |
| `inject` | `branch, role, message` | enqueue a user message |
| `branch` | `at` | branch at anchor (new child branch, `current` → child) |
| `resample` | `at` | same as branch; semantically "regenerate" |
| `edit` | `at, output` | edit target turn output, creates new branch |
| `switch` | `branch` | change `current` without forking |
