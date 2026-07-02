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


def install_template() -> None:
    """Register ``pio.templates["workbench"]`` and set it as default.

    Idempotent. The ``notebook_connected`` renderer is set separately in
    ``orchestrator._prewarm`` — don't touch it here.
    """
    if "workbench" in pio.templates and pio.templates.default == "workbench":
        return
    axis = dict(
        gridcolor="#e8e8ec",
        linecolor="#e8e8ec",
        zerolinecolor="#d4d4d8",
        tickfont=dict(color=INK["faint"], size=10),
    )
    pio.templates["workbench"] = go.layout.Template(
        layout=dict(
            font=dict(
                family="Inter, -apple-system, Segoe UI, Roboto, sans-serif",
                size=12,
                color=INK["mid"],
            ),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=40, r=20, t=30, b=40),
            height=320,
            # App palette (styles.css `--accent`/`--ok`/`--danger`) first so
            # single-series charts pick up the accent, not grey.
            colorway=[ACCENT, OK, "#ff9f43", DANGER, "#a855f7", "#06b6d4"],
            colorscale=dict(sequential=[[0, INK["faint"]], [1, INK["ink"]]]),
            xaxis=axis,
            yaxis=axis,
            hoverlabel=dict(
                bgcolor="#fff",
                bordercolor="#e8e8ec",
                font=dict(color=INK["mid"]),
            ),
            modebar=dict(remove=["toImage", "autoScale2d", "resetScale2d"]),
            showlegend=False,
        ),
        data=dict(
            bar=[go.Bar(marker=dict(line=dict(width=0)))],
            scatter=[go.Scatter(line=dict(dash="solid"))],
            histogram=[go.Histogram()],
        ),
    )
    pio.templates.default = "workbench"


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

    link = staticmethod(link)
    annotate_top = staticmethod(annotate_top)
    paired_slope = staticmethod(paired_slope)
    replicate_grid = staticmethod(replicate_grid)
    survival = staticmethod(survival)

    def __repr__(self) -> str:
        return "<wb.plots · link annotate_top paired_slope replicate_grid survival>"
