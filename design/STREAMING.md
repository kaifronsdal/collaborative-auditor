# Streaming model generation for the workbench UI

*Design memo, 2026-06-16. Grounded in inspect_ai main, petri-meridian `tape-replay`,
collaborative-auditor. The question: how do tokens reach the UI as the model generates, and
how does that compose with `tape.replayable`?*

**Decision (2026-06-16): one provider stack — inspect's.** inspect's provider conversions are
faithful enough; the earlier plan to adopt sonde's `Provider.stream` for the target is dropped.
Streaming lands as an **inspect PR** (per-chunk callback the providers invoke from the SDK
streams they already open), so both auditor and target stream through the same path with no
`target_agent` reimplementation. The §"What sonde provides" section below is kept as design
reference for the delta-event shape only.

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

## Approach — inspect PR: per-chunk callback on `GenerateConfig`

`GenerateConfig` grows one optional field; each provider's existing SDK-stream loop calls it
per chunk while it accumulates the final `ModelOutput`:

```python
class ContentDelta(BaseModel):
    kind: Literal["text", "thinking", "tool_input"]
    index: int          # content-block index
    delta: str

# GenerateConfig
on_content: Callable[[ContentDelta], None] | None = None
```

Providers already open the SDK stream (Anthropic when reasoning is on or `max_tokens≥8192`;
Google opt-in); the PR just adds `if config.on_content: config.on_content(delta)` inside those
loops, and forces the streaming path on when `on_content` is set. OpenAI's chat path needs
`stream=True` added. `Model.generate` returns `ModelOutput` exactly as before.

Both sides then read identically — `tape.replayable` records the final value, the callback is
the UI side-channel inside the wrapped call:

```python
generate = tape.replayable(agent_model.generate, source="…")
state.output = await generate(input=msgs, tools=tools, config=GenerateConfig(on_content=sink))
```

The workbench's Run loop sets `sink` per generation (knows which node id is being filled),
buffers per-node, flushes at ~10 Hz as one targeted append-patch op.

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

## Effort — ~2–3 days

- **inspect PR:** `ContentDelta` type + `GenerateConfig.on_content` + per-provider loop hook
  (Anthropic/Google: add the callback inside the existing stream consumer; OpenAI chat +
  responses: enable `stream=True` and add the consumer). mockllm grows a `stream_chunks` mode
  for tests. **~1–1.5 d.** Low risk — providers already produce the final `ModelOutput`; the
  callback is a side-effect inside that loop.
- **Workbench:** sink + 10 Hz coalescer + ViewState active-generation field + targeted
  append-patch + finalize-on-complete. **~1–1.5 d.**
- Auditor streaming comes for free (same `GenerateConfig` field).

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
