"""Matplotlib charts for simulation runs. Static PNGs (no hover layer). Colors follow the dataviz
reference palette in fixed order and follow the entity: progressive is always blue, predictive is
always orange, and each Safety Controller reason code always has the same color."""
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
MODE_COLOR = {"PROGRESSIVE": "#2a78d6", "PREDICTIVE": "#eb6834"}
REASON_COLOR = {   # slots 3-8, fixed order
    "HARD_RATIO_CAP": "#1baf7a", "COLD_START": "#eda100", "ANSWER_RATE_DRIFT": "#e87ba4",
    "ABANDON_BUDGET": "#008300", "AGENT_DROP": "#6250d6", "PROVIDER_HEALTH": "#e34948",
}


def _style(ax, title, ylabel):
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", fontsize=10, color=INK, fontweight="bold")
    ax.set_ylabel(ylabel, fontsize=8, color=INK_2)
    ax.tick_params(colors=INK_2, labelsize=8, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    for s in ax.spines.values():
        s.set_visible(False)


def _direct_labels(ax, ends):
    """Label line ends; nudge apart labels that would collide (legend carries identity anyway)."""
    lo, hi = ax.get_ylim()
    gap = (hi - lo) * 0.07
    items = sorted(ends.items(), key=lambda kv: kv[1][1])
    placed = []
    for mode, (x, y) in items:
        if placed and y - placed[-1] < gap:
            y = placed[-1] + gap
        placed.append(y)
        ax.annotate(mode.lower(), (x, y), xytext=(4, 0), textcoords="offset points", fontsize=8, color=INK_2, va="center")


def run_figure(path, title, runs: dict, shock_min: float | None = None, budget: float = 0.03):
    """runs: {"PROGRESSIVE": rows, "PREDICTIVE": rows}, rows = per-minute dicts."""
    fig, axes = plt.subplots(6, 1, figsize=(10, 15), sharex=True, facecolor=SURFACE)
    import textwrap
    fig.suptitle(textwrap.fill(title, 95), x=0.06, ha="left", fontsize=12, color=INK, fontweight="bold")
    panels = [
        ("Agent utilization (talk time / staffed time)", "%", lambda r: 100 * r["utilization"]),
        ("Calls initiated per minute", "calls", lambda r: r["dials"]),
        ("Calls connected per minute", "calls", lambda r: r["connected"]),
        (f"Cumulative abandon rate (dashed line: {budget:.0%} budget)", "% of answered", None),
        ("Pacing ratio: calls awaiting answer per agent awaiting a call", "ratio", lambda r: r["pacing_ratio"]),
    ]
    for ax, (ptitle, ylabel, fn) in zip(axes, panels):
        _style(ax, ptitle, ylabel)
        ends = {}
        for mode, rows in runs.items():
            xs = [r["minute"] for r in rows]
            if fn is None:
                ab = an = 0
                ys = []
                for r in rows:
                    ab, an = ab + r["abandoned"], an + r["answered"]
                    ys.append(100 * ab / an if an else 0)
                ax.axhline(100 * budget, color=INK_2, linewidth=1, linestyle="--")
            else:
                ys = [fn(r) for r in rows]
            ax.plot(xs, ys, color=MODE_COLOR[mode], linewidth=2, label=mode.lower())
            ends[mode] = (xs[-1], ys[-1])
        _direct_labels(ax, ends)
    axes[0].legend(frameon=False, fontsize=8, loc="lower right")

    ax = axes[5]
    _style(ax, "Safety Controller reason codes fired per minute, predictive run (one tick can fire several)", "codes")
    rows = runs.get("PREDICTIVE", [])
    xs = [r["minute"] for r in rows]
    bottom = [0] * len(rows)
    for reason, color in REASON_COLOR.items():
        ys = [r["reasons"].get(reason, 0) for r in rows]
        if any(ys):
            ax.bar(xs, ys, bottom=bottom, color=color, width=0.9, label=reason, edgecolor=SURFACE, linewidth=1)
            bottom = [b + y for b, y in zip(bottom, ys)]
    if any(bottom):
        ax.legend(frameon=False, fontsize=7, ncol=3, loc="upper left")
    ax.set_xlabel("minute", fontsize=8, color=INK_2)
    if shock_min is not None:
        for a in axes:
            a.axvline(shock_min, color=INK_2, linewidth=1, linestyle=":")
    fig.tight_layout(rect=(0, 0, 1, 0.98))
    fig.savefig(path, dpi=110, facecolor=SURFACE)
    plt.close(fig)
