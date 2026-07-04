"""``wb.plots`` — plotly template + thin chart helpers (M1-PLOTTING.md rev 2).

The agent plots with ``px.*`` directly; these are the handful of helpers
that *significantly* cut agent code vs. a one-liner. Every mark carries
its ``wb://`` ref in ``customdata[0]`` (and, when ``link(log=…)`` was
used, the ``.eval`` path in ``customdata[1]``) so the frontend's
``plotly_click`` handler can ``{t:"import"}`` the transcript.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio

# -- palette (M1-PLOTTING.md §Template) --------------------------------------

INK: dict[str, Any] = dict(
    ink="#1a1a1a",
    mid="#474747",
    dim="#707070",
    faint="#949494",
    ramp=["#f0f0f0", "#c8c8c8", "#888888", "#1a1a1a"],
)
ACCENT, OK, DANGER = "#2f76da", "#479e76", "#b33232"


# -- model → color / label (data in ``model_palette.py``) --------------------

from workbench.m1.model_palette import (  # noqa: E402
    BRAND_CASE,
    BRAND_PREFIX,
    LOWERCASE_TOKENS,
    MODEL_LABELS,
    MODEL_ORDER,
    PROVIDER_ALIAS,
    PROVIDER_HUE,
    TIER_L,
)

#: Lightness ramp per family — flagship darkest, smallest lightest.
#: Saturation co-varies (dark = more saturated) so adjacent shades differ
#: on two axes, not just L, which is what makes opus-4-8/4-7/4-6 legible
#: side-by-side even in a large family.
_L_RAMP = (28, 76)
_S_RAMP = (10, -14)  # delta from provider base S at (dark, light) ends


def _hsl(h: int, s: int, lightness: int) -> str:
    """HSL → ``#rrggbb``. Clamps so nothing goes near-black/white."""
    import colorsys  # noqa: PLC0415

    lightness = max(18, min(84, lightness))
    s = max(0, min(95, s))
    r, g, b = colorsys.hls_to_rgb(h / 360, lightness / 100, s / 100)
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def _resolve_provider(model_id: str) -> tuple[str, str]:
    """→ ``(canonical_provider, name)``. Strips router prefixes and
    resolves aliases; infers provider from brand prefix on bare ids."""
    parts = model_id.split("/")
    # Strip ``openrouter/`` / ``together/`` router prefixes.
    if len(parts) > 2 and parts[0] in {"openrouter", "together", "hf"}:
        parts = parts[1:]
    if len(parts) == 1:
        name = parts[0]
        for pfx, prov in BRAND_PREFIX.items():
            if name.lower().startswith(pfx):
                return prov, name
        return "", name
    provider, name = parts[0], "/".join(parts[1:])
    return PROVIDER_ALIAS.get(provider, provider), name


def _ramp_color(provider: str, t: float) -> str:
    """Color at position ``t ∈ [0, 1]`` along ``provider``'s ramp."""
    h, s0 = PROVIDER_HUE.get(provider, (hash(provider) % 360, 45))
    lightness = _L_RAMP[0] + t * (_L_RAMP[1] - _L_RAMP[0])
    # Greyscale providers stay greyscale — don't let the S-ramp tint them.
    s = 0 if s0 == 0 else s0 + _S_RAMP[0] + t * (_S_RAMP[1] - _S_RAMP[0])
    return _hsl(h, round(s), round(lightness))


def model_color(model_id: str) -> str:
    """Deterministic hex color for a model id.

    Provider decides hue (16 providers spread across the wheel;
    ``model_palette.PROVIDER_HUE``). Within a provider, position along
    the L+S ramp is the model's index in ``MODEL_ORDER[provider]``
    (flagship=0 → darkest/saturated; smallest=N-1 → lightest/pale) —
    so N models get N *evenly-spaced* shades. Ids not in the order list
    fall back to the ``TIER_L`` keyword heuristic.
    """
    provider, name = _resolve_provider(model_id)
    order = MODEL_ORDER.get(provider, [])
    canonical = f"{provider}/{name}" if provider else name
    # Known model → interpolate on the ordered ramp.
    for candidate in (model_id, canonical):
        if candidate in order:
            i, n = order.index(candidate), len(order)
            return _ramp_color(provider, i / max(1, n - 1))
    # Unknown model in a known/unknown provider → tier-keyword heuristic.
    lname = name.lower()
    tier_l = next((dl for kw, dl in TIER_L if kw in lname), 0)
    # Map tier_l ∈ [-18, +22] onto the ramp position.
    t = (tier_l + 18) / 40
    return _ramp_color(provider, min(1.0, max(0.0, t)))


def model_colormap(models: Sequence[str]) -> dict[str, str]:
    """``{model_id: hex}`` — pass as ``color_discrete_map=`` to ``px.*``."""
    return {m: model_color(m) for m in models}


#: Derived: every id in ``MODEL_ORDER`` → its ramp color. Exposed for
#: back-compat and for direct dict lookup in tests.
MODEL_COLORS: dict[str, str] = {
    m: _ramp_color(p, i / max(1, len(ms) - 1))
    for p, ms in MODEL_ORDER.items()
    for i, m in enumerate(ms)
}


# -- model → display label ---------------------------------------------------

import re  # noqa: E402

_DATE_SUFFIX = re.compile(r"-20\d{6}$|-\d{2}-20\d{2}$|-0[1-9]\d{2}$|-1[0-2]\d{2}$")
_CLAUDE_VER = re.compile(r"\b(\d)-(\d)\b")


def model_label(model_id: str) -> str:
    """Canonical human-facing display name for a model id.

    Looks up ``MODEL_LABELS`` first; falls back to a rule-based
    prettifier: strip provider/router prefix, strip ``-YYYYMMDD`` /
    ``-MMDD`` snapshot suffixes, ``4-8 → 4.8`` for Claude, split on
    ``-``, title-case each token, then apply brand-casing exceptions
    (``Gpt→GPT``, ``Deepseek→DeepSeek``, ``mini`` stays lowercase, …).
    """
    provider, name = _resolve_provider(model_id)
    canonical = f"{provider}/{name}" if provider else name
    if (label := MODEL_LABELS.get(model_id) or MODEL_LABELS.get(canonical)):
        return label
    # Heuristic fallback.
    n = _DATE_SUFFIX.sub("", name)
    n = n.removesuffix("-latest").removesuffix("-preview")
    if provider == "anthropic":
        n = _CLAUDE_VER.sub(r"\1.\2", n)
    parts = [p for p in n.replace("_", "-").split("-") if p]
    out: list[str] = []
    for p in parts:
        if p in LOWERCASE_TOKENS:
            out.append(p)
        elif re.fullmatch(r"\d+(\.\d+)?[a-z]?", p):  # 5.4, 4o, 3n
            out.append(p)
        else:
            t = p[:1].upper() + p[1:]
            out.append(BRAND_CASE.get(t, t))
    return " ".join(out)


def model_labelmap(models: Sequence[str]) -> dict[str, str]:
    """``{model_id: label}`` — for ``px.*`` axis relabelling or a
    ``df.assign(label=df.model.map(model_labelmap(df.model)))`` column."""
    return {m: model_label(m) for m in models}


def by_model(
    df: pd.DataFrame, col: str = "model", *, order: bool = True
) -> dict[str, Any]:
    """Kwargs to splat into any ``px.*`` call for model-colored charts.

        >>> px.bar(df, x="model", y="score", **wb.plots.by_model(df))

    Sets ``color=col`` + ``color_discrete_map`` (keyed on both raw ids
    *and* their ``model_label``, so it works whether the axis shows ids
    or labels) + ``category_orders`` (provider hue then ramp position,
    so same-provider models sit adjacent and flagship-first).
    """
    models = list(dict.fromkeys(df[col]))
    labels = model_labelmap(models)
    cmap = model_colormap(models)
    # Also key the map on labels so ``x=df.model.map(model_label)`` or a
    # pre-labelled column still picks up the right colors.
    cmap |= {labels[m]: cmap[m] for m in models}
    kw: dict[str, Any] = {"color": col, "color_discrete_map": cmap}
    if order:
        def _key(m: str) -> tuple[int, float]:
            p, _ = _resolve_provider(m)
            h = PROVIDER_HUE.get(p, (999, 0))[0]
            ms = MODEL_ORDER.get(p, [])
            return (h, ms.index(m) / len(ms) if m in ms else 0.5)

        ordered = sorted(models, key=_key)
        kw["category_orders"] = {col: ordered + [labels[m] for m in ordered]}
    return kw


def install_template() -> None:
    """Register ``pio.templates["workbench"]`` and layer it on ``plotly_white``.

    Layering (``"plotly_white+workbench"``) means the ~25 trace types we
    don't style here inherit sane defaults instead of raw plotly.js. The
    overrides below are the deltas: Okabe-Ito colorway (CVD-safe — the
    prior palette collapsed green/red and blue/purple under
    deuteranopia), y-grid only, WCAG-AA tick contrast, ``automargin`` /
    ``autotickangles``, and explicit ``hovermode="closest"`` (``x
    unified`` breaks the per-mark ``plotly_click`` → ``wb://`` nav that
    ``link()`` depends on).

    Idempotent. The ``notebook_connected`` renderer is set separately in
    ``orchestrator._prewarm`` — don't touch it here.
    """
    default = "plotly_white+workbench"
    if "workbench" in pio.templates and pio.templates.default == default:
        return
    axis = dict(
        showgrid=True,
        gridcolor="#e8e8ec",
        gridwidth=1,
        linecolor="#d4d4d8",
        zerolinecolor="#d4d4d8",
        zerolinewidth=1,
        automargin=True,
        tickfont=dict(color=INK["dim"], size=11),
        title=dict(font=dict(color=INK["dim"], size=12), standoff=8),
    )
    pio.templates["workbench"] = go.layout.Template(
        layout=dict(
            font=dict(
                # Match ``frontend-wb/src/styles.css`` ``--sans`` exactly so
                # in-column figures use the same face as the surrounding UI.
                family=(
                    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
                    "Inter, sans-serif"
                ),
                size=12,
                color=INK["mid"],
            ),
            # Title pinned to container-top so the horizontal legend
            # (paper-relative, ``y=1.02``) sits below it without overlap.
            title=dict(
                font=dict(color=INK["ink"], size=14),
                x=0,
                xanchor="left",
                yref="container",
                y=0.97,
                yanchor="top",
            ),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=40, r=20, t=52, b=40),
            height=320,
            # Okabe-Ito (Wong 2011, Nature Methods) with app ACCENT
            # substituted for O-I blue #0072B2 so single-series charts
            # match the UI. CVD-safe for prot/deuter/tritanopia; distinct
            # in greyscale. OK/DANGER stay as *explicit* semantic
            # constants — not in the categorical rotation (else the 2nd
            # series in any 2-trace chart is "green = good").
            colorway=[
                ACCENT, "#e69f00", "#009e73", "#cc79a7",
                "#56b4e9", "#d55e00", "#f0e442", "#999999",
            ],
            colorscale=dict(sequential=[[0, INK["faint"]], [1, INK["ink"]]]),
            xaxis={**axis, "showgrid": False, "autotickangles": [0, -45, -90]},
            yaxis=axis,
            bargap=0.25,
            hovermode="closest",
            hoverlabel=dict(
                bgcolor="#fff",
                bordercolor="#e8e8ec",
                font=dict(color=INK["mid"], size=11),
            ),
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="left",
                x=0,
                font=dict(color=INK["dim"], size=11),
                bgcolor="rgba(0,0,0,0)",
            ),
            modebar=dict(remove=["toImage", "autoScale2d", "resetScale2d"]),
        ),
        data=dict(
            bar=[
                go.Bar(
                    marker=dict(line=dict(width=0)),
                    hovertemplate="%{x}: %{y}<extra></extra>",
                )
            ],
            scatter=[
                go.Scatter(marker=dict(size=7, opacity=0.85), line=dict(width=2))
            ],
            histogram=[go.Histogram()],
        ),
    )
    pio.templates.default = default


# -- primitives ---------------------------------------------------------------


def link(
    fig: go.Figure,
    ids: pd.Series | Sequence[str],
    *,
    log: str | None = None,
    scheme: str = "wb://audit/",
) -> go.Figure:
    """Retrofit ``customdata = [wb://audit/{id}, log?]`` onto traces lacking it.

    ``customdata[0]`` is the ``wb://`` ref (shown in the hover);
    ``customdata[1]`` is the ``.eval`` file path when ``log`` is given —
    the frontend's ``plotly_click`` reads it to send ``{t:"import", path:
    log, sample_id}``. Without ``log`` the click handler warns and no-ops
    (points reference *finished* samples, so it needs the file, not a dir).

    Prefer passing ``custom_data=["audit_id"]`` to ``px.*`` directly; this
    is for figures built without it. Assumes single-trace or traces that
    share ``ids`` order — for figures with ``color=``/``facet=``
    (multi-trace), pass ``custom_data=[id_col]`` to ``px.*`` directly
    instead.
    """
    if log is None:
        refs = np.asarray([[f"{scheme}{i}"] for i in ids])
    else:
        refs = np.asarray([[f"{scheme}{i}", log] for i in ids])
    for tr in fig.data:
        if tr.customdata is None:
            tr.customdata = refs
        ht = tr.hovertemplate
        tr.hovertemplate = (ht or "") + "%{customdata[0]}<extra></extra>"
    return fig


def annotate_top(
    fig: go.Figure, df: pd.DataFrame, x: str, y: str, label: str, n: int = 3
) -> go.Figure:
    """Label the top-``n`` rows by ``y`` at their ``(x, y)`` data position."""
    top = df.nlargest(n, y)
    for _, r in top.iterrows():
        fig.add_annotation(
            x=r[x],
            y=r[y],
            text=str(r[label]),
            showarrow=True,
            arrowhead=2,
            ax=0,
            ay=-18,
            font=dict(color=ACCENT, size=10),
            arrowcolor=ACCENT,
        )
    return fig


# -- bespoke layouts ----------------------------------------------------------


def paired_slope(
    df: pd.DataFrame,
    *,
    x: str,
    y: str,
    pair: str,
    hue: str | Callable[[pd.Series], bool] | None = None,
) -> go.Figure:
    """One line per ``pair`` connecting its observations across ``x`` levels."""
    d = df.sort_values([pair, x])
    if callable(hue):
        d = d.assign(_h=d.apply(hue, axis=1))
        hue = "_h"
    fig = px.line(
        d,
        x=x,
        y=y,
        line_group=pair,
        color=hue,
        markers=True,
        custom_data=[pair],
        color_discrete_sequence=[INK["dim"], ACCENT],
    )
    fig.update_traces(line=dict(width=1))
    return fig


def replicate_grid(
    df: pd.DataFrame, *, x: str, y: str, facet: str, **kw: Any
) -> go.Figure:
    """Small multiples: one panel per ``facet`` value."""
    fig = px.scatter(
        df,
        x=x,
        y=y,
        facet_col=facet,
        facet_col_wrap=kw.pop("wrap", 4),
        custom_data=kw.pop("custom_data", [facet]),
        **kw,
    )
    fig.for_each_annotation(lambda a: a.update(text=a.text.split("=")[-1]))
    return fig


def survival(
    df: pd.DataFrame, *, event: str, time: str, group: str | None = None
) -> go.Figure:
    """KM-style step: ``S(t) = 1 - cum_events / N`` per group (no censoring)."""
    dashes = ("solid", "dash", "dot", "dashdot")
    fig = go.Figure()
    for i, (name, g) in enumerate(df.groupby(group) if group else [("", df)]):
        g = g.sort_values(time)
        s = 1.0 - g[event].cumsum() / len(g)
        fig.add_scatter(
            x=g[time],
            y=s,
            mode="lines",
            name=str(name),
            line=dict(shape="hv", dash=dashes[i % 4], color=INK["mid"]),
        )
        if group:
            fig.add_annotation(
                x=g[time].iloc[-1],
                y=s.iloc[-1],
                text=str(name),
                showarrow=False,
                xanchor="left",
                font=dict(color=INK["mid"]),
            )
    return fig.update_layout(yaxis=dict(range=[0, 1.02], title="S(t)"))


# -- namespace ----------------------------------------------------------------


class Plots:
    """The ``wb.plots.*`` surface — thin wrappers over ``px``."""

    INK, ACCENT, OK, DANGER = INK, ACCENT, OK, DANGER
    MODEL_COLORS, MODEL_LABELS = MODEL_COLORS, MODEL_LABELS

    link = staticmethod(link)
    annotate_top = staticmethod(annotate_top)
    paired_slope = staticmethod(paired_slope)
    replicate_grid = staticmethod(replicate_grid)
    survival = staticmethod(survival)
    model_color = staticmethod(model_color)
    model_colormap = staticmethod(model_colormap)
    model_label = staticmethod(model_label)
    model_labelmap = staticmethod(model_labelmap)
    by_model = staticmethod(by_model)

    def __repr__(self) -> str:
        return (
            "<wb.plots · link annotate_top paired_slope replicate_grid "
            "survival model_color model_colormap model_label model_labelmap>"
        )
