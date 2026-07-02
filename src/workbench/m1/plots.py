"""``wb.plots`` — plotly template + thin chart helpers (M1-PLOTTING.md rev 2).

The agent plots with ``px.*`` directly; these are the handful of helpers
that *significantly* cut agent code vs. a one-liner. Every mark carries
its ``wb://`` ref in ``customdata[0]`` so the frontend's ``plotly_click``
handler can navigate to the transcript.
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


# -- model → color (M1-PLOTTING.md §Model palette) ---------------------------
#
# One hue per provider (brand-adjacent, ≥30° apart so provider is the
# dominant visual grouping); lightness varies by tier within a provider
# (flagship = darker/saturated, small = lighter), then a small nudge by
# version so e.g. opus-4-8 vs opus-4-6 are distinguishable side-by-side.
# The explicit ``MODEL_COLORS`` dict pins common ids to stable hexes;
# ``model_color()`` falls through to the hue/tier heuristic for anything
# not listed. Update the dict as new models ship.

_PROVIDER_HUE: dict[str, tuple[int, int]] = {
    # provider → (H°, S%). Anthropic clay-orange ≈ H24; Gemini gradient
    # ≈ H270; user spec: openai=blue, google=purple.
    "anthropic": (24, 68),
    "openai": (210, 62),
    "google": (270, 55),
    "x-ai": (0, 0),  # grok — greyscale
    "meta-llama": (195, 55),  # cyan (dodge openai-blue)
    "meta": (195, 55),
    "mistral": (45, 70),  # amber (dodge anthropic-orange)
    "deepseek": (245, 60),
    "moonshotai": (330, 55),  # kimi — magenta
    "z-ai": (165, 50),  # glm — teal
    "cohere": (300, 50),
    "together": (150, 45),
    "mockllm": (0, 0),
}

#: Tier keywords → lightness delta from L=50%. Flagship darker; distilled
#: lighter. Matched as substrings against the model id (first hit wins).
_TIER_L: tuple[tuple[str, int], ...] = (
    ("deep-think", -14), ("ultra", -14),
    ("opus", -10), ("pro", -8), ("o3", -8), ("o1", -6), ("large", -8),
    ("mythos", -4), ("grok-4", -4),
    ("sonnet", 0), ("gpt-5", 0), ("gpt-4", 6), ("medium", 0), ("chat", 4),
    ("gemini", 0), ("glm", 0), ("kimi", 0), ("llama", 0), ("grok", 0),
    ("flash", 10), ("haiku", 12), ("mini", 14), ("small", 12),
    ("nano", 18), ("lite", 16), ("oss", 8),
)


def _hsl(h: int, s: int, lightness: int) -> str:
    """HSL → ``#rrggbb``. Clamps L to [15, 85] so nothing goes near-black/white."""
    import colorsys  # noqa: PLC0415

    lightness = max(15, min(85, lightness))
    r, g, b = colorsys.hls_to_rgb(h / 360, lightness / 100, s / 100)
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def _version_nudge(name: str) -> int:
    """Small L offset from the trailing version number so adjacent
    releases within a tier are distinguishable (newer → slightly darker)."""
    import re  # noqa: PLC0415

    if m := re.search(r"(\d+)[.\-]?(\d+)?", name.rsplit("/", 1)[-1]):
        major = int(m.group(1))
        minor = int(m.group(2) or 0)
        # Clamp so a runaway version number doesn't blow past the tier band.
        return -min(8, major + minor // 2)
    return 0


def model_color(model_id: str) -> str:
    """Deterministic hex color for a model id.

    Provider decides hue; tier keyword decides lightness band; version
    number nudges within the band. Unknown providers hash to a hue so
    every id gets *something* stable. Prefer the explicit
    ``MODEL_COLORS`` entry when present.
    """
    if (c := MODEL_COLORS.get(model_id)) is not None:
        return c
    provider, _, name = model_id.partition("/")
    if not name:
        name, provider = provider, ""
    # Normalise a few aliases inspect uses.
    provider = {"vertex": "google", "azure": "openai", "bedrock": "anthropic"}.get(
        provider, provider
    )
    if provider in _PROVIDER_HUE:
        h, s = _PROVIDER_HUE[provider]
    else:
        # Try to infer from the name (e.g. bare ``claude-opus-4-8``).
        for key, (h, s) in _PROVIDER_HUE.items():
            if key in name or name.startswith(
                {"anthropic": "claude", "openai": "gpt", "google": "gemini",
                 "x-ai": "grok", "moonshotai": "kimi", "z-ai": "glm"}.get(key, key)
            ):
                break
        else:
            h, s = (hash(provider or name) % 360, 45)
    tier_l = next((dl for kw, dl in _TIER_L if kw in name.lower()), 0)
    return _hsl(h, s, 50 + tier_l + _version_nudge(name))


def model_colormap(models: Sequence[str]) -> dict[str, str]:
    """``{model_id: hex}`` — pass as ``color_discrete_map=`` to ``px.*``."""
    return {m: model_color(m) for m in models}


#: Explicit pins for models we know about today (CLAUDE.md §Recent
#: Frontier Models). Everything else falls through to ``model_color``'s
#: heuristic. Update as new models ship.
MODEL_COLORS: dict[str, str] = {
    # Anthropic — clay orange, opus darkest → haiku lightest
    "anthropic/claude-opus-4-8": "#c85a2e",
    "anthropic/claude-opus-4-7": "#cf6236",
    "anthropic/claude-opus-4-6": "#d56a3f",
    "anthropic/claude-mythos-preview": "#d9744b",
    "anthropic/claude-sonnet-5": "#df7d54",
    "anthropic/claude-sonnet-4-6": "#e4885f",
    "anthropic/claude-haiku-4-5-20251001": "#eda57f",
    # OpenAI — blue
    "openai/gpt-5.4-pro": "#2a5f9e",
    "openai/gpt-5.4": "#3a6fad",
    "openai/gpt-5.3-codex": "#4278b4",
    "openai/gpt-5.3-chat-latest": "#4f84bd",
    "openai/gpt-5.2": "#5b8fc5",
    "openai/gpt-5-mini": "#7fa9d4",
    "openai/gpt-oss-120b": "#94b7dc",
    # Google — purple
    "google/gemini-3.1-deep-think": "#5d3fa8",
    "google/gemini-3.1-pro": "#6d50b5",
    "google/gemini-3.5-flash": "#9a80d1",
    "google/gemini-3-flash-preview": "#a892d9",
    # xAI — greyscale
    "x-ai/grok-4.3": "#3a3a3a",
    "x-ai/grok-4.1": "#525252",
    "x-ai/grok-4": "#6b6b6b",
    # Zhipu — teal
    "z-ai/glm-5.1": "#3f9e8a",
    "z-ai/glm-5": "#52ab98",
    # Moonshot — magenta
    "moonshotai/kimi-k2.6": "#b84a8f",
    "moonshotai/kimi-k2.5": "#c25e9c",
    "moonshotai/kimi-k2-0905": "#cc72a9",
}


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
    fig: go.Figure, ids: pd.Series | Sequence[str], *, scheme: str = "wb://audit/"
) -> go.Figure:
    """Retrofit ``customdata[0] = wb://audit/{id}`` onto traces lacking it.

    Prefer passing ``custom_data=["audit_id"]`` to ``px.*`` directly; this
    is for figures built without it. Assumes single-trace or traces that
    share ``ids`` order — for figures with ``color=``/``facet=``
    (multi-trace), pass ``custom_data=[id_col]`` to ``px.*`` directly
    instead.
    """
    refs = np.asarray([[f"{scheme}{i}"] for i in ids])
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
    MODEL_COLORS = MODEL_COLORS

    link = staticmethod(link)
    annotate_top = staticmethod(annotate_top)
    paired_slope = staticmethod(paired_slope)
    replicate_grid = staticmethod(replicate_grid)
    survival = staticmethod(survival)
    model_color = staticmethod(model_color)
    model_colormap = staticmethod(model_colormap)

    def __repr__(self) -> str:
        return (
            "<wb.plots · link annotate_top paired_slope replicate_grid "
            "survival model_color model_colormap>"
        )
