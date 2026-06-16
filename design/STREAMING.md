# Streaming model generation for the workbench UI

*Design memo, 2026-06-16. Grounded in inspect_ai main, sonde-alpha-may-2026, petri-meridian
`tape-replay`, collaborative-auditor. The question: how do tokens reach the UI as the model
generates, and how does that compose with `tape.replayable`?*

---

## What inspect_ai provides today — no streaming seam

`Model.generate` is single-awaited, fully-materialized. No `stream=` param
(`_model.py:673-680`), no streaming on `ModelAPI.generate` (`:303-310`), no `GenerateConfig`
field. Providers consume SDK streams **internally** (Anthropic uses `client.messages.stream`
gated by reasoning/`max_tokens≥8192`, `anthropic.py:785-789,556-560`; Google opt-in; OpenAI
doesn't stream at all) and collapse to a final message — no per-token hook. `ModelEvent`
carries the complete output; the only seam is whole-call `on_pending`/`on_complete`. The
Textual viewer skips pending events entirely (`transcript.py:293-294`).

**To stream the target, bypass `Model.generate`** — which DESIGN §3.5 already plans (sonde
provider layer for the target). Auditor streaming is blocked on the §3.5 unification; inspect
can't stream it today.

## What sonde provides — the API we adopt

`Provider.stream` is an **async generator** (`providers/base.py:53-74`) yielding a
`SurfaceEvent` discriminated union (`events.py:235-248`): `item.added`, `item.updated`
(`assistant_text_delta`/`assistant_thinking_delta`/`tool_input_delta`), `item.done`, `notice`,
`error`, `rate_limit`. Providers translate native SDK streams (`anthropic.py:196-282` etc.).
Transport is SSE + a server-side `apply_to_node` accumulator (`base.py:77-114`) folding deltas
into the persisted Node; partials persist on disconnect; a property test asserts the
accumulator equals the SDK's own `get_final_message()`.

**We adopt the provider stream + SurfaceEvent shape, not the SSE transport** — DESIGN §3.6
already replaces SSE-per-generation with WS+JSON-Patch ViewState (SSE can't represent multiple
concurrent Runs).

## Approach — stream inside the wrapped fn (option c)

The stream happens *inside* the replayable-wrapped target agent: it consumes `provider.stream`,
pushes deltas to a side-channel, accumulates into a `ModelOutput`, returns it.
`context.replayable(...)` records only the final value — `tape.replayable` is single-value
(`_history.py:137-183`) and stays untouched. Reject (a) (two model calls, breaks provenance)
and (b) (rewrites the replay contract the rollback/resume invariant depends on).

**Side-channel = a contextvar sink**, set by the Run loop around each generation:

```python
StreamSink = Callable[[StreamDelta], None]   # {node_id, kind: text|thinking|tool_input, delta}
_active_sink: ContextVar[StreamSink | None]
@contextmanager
def stream_to(sink: StreamSink): ...
```

Threading a `stream_to=` kwarg through every call site (and across the `replayable` boundary)
is invasive; the contextvar matches how inspect resolves model roles/transcripts.

**UI hop:** do **not** emit a JSON-Patch op per token — `push_view_state` diffs the full
serialized state (`common.py:71-93`, O(branches×messages); ARCHITECTURE.md melt-risk). Instead:
coalesce deltas in the sink at ~10 Hz, each flush emits one targeted append op on the active
node's streaming-text field (the store knows which node changed — emit the op directly, don't
diff). The final `ModelOutput` push reconciles. Needs one ViewState addition: an
"active-generation" buffer field on the node (`status: streaming`, like sonde's
`AssistantMessageItem.status`).

**Target streaming first; auditor streaming deferred** to the §3.5 sonde-provider unification.

## Replay appears instantly — no fake-stream

On replay `tape.replayable` serves from `pending` without awaiting the fn — the sink never
fires. Streaming masks generation latency; on replay there is none. Fake-streaming would add
latency, make branch forks crawl through already-seen content, require fabricated timing, and
conflate "settled" with "generating now." Replayed content *is* settled; instant rendering says
so. (A client-side cosmetic reveal animation, if anyone wants one, is independent of this.)

## Effort — ~4–6 days; risk in the target-agent reimplementation

- Sonde-backed `target_agent` over `Provider.stream`, accumulating to `ModelOutput`, keeping
  `TargetContext`/replay intact: **2–3 d**. Riskiest piece — accumulated `ModelOutput` must be
  byte-equivalent to a non-streaming call (port sonde's `get_final_message()` property test);
  `ChatMessage.id` anchors and staging through the new agent are where replay-invariant bugs
  hide.
- Contextvar sink + `StreamDelta` + 10 Hz coalescer: **0.5 d**.
- ViewState active-generation field + targeted append-patch + finalize-on-complete: **1–1.5 d**.
- Auditor streaming: deferred.

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
