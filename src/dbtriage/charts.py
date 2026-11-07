"""Inline SVG charts for the web view. No JavaScript, no CDN: each chart is a string of SVG that
inherits the page's CSS variables (--accent, --fg, --mut, --high ...) so it follows the brand and
the light/dark theme. Every mark carries a <title> so hovering shows the exact numbers."""
from __future__ import annotations

import html

SEV = ("high", "medium", "low", "info")


def fmt(v) -> str:
    """Compact number: 1234567 -> 1.2M, 0.9634 -> 0.96, None -> '-'."""
    if v is None:
        return "-"
    if isinstance(v, float) and abs(v) < 10:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    for unit, div in (("G", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(v) >= div:
            return f"{v / div:.1f}{unit}"
    return f"{v:.0f}" if isinstance(v, float) else str(v)


def _t(s) -> str:
    return html.escape(str(s), quote=True)


def severity_strip(points: list[dict], height: int = 56) -> str:
    """One bar per snapshot, oldest left. Height = the load metric, colour = severity (status colour,
    labelled in the tooltip and in the table underneath, never colour alone)."""
    if not points:
        return ""
    width = 640
    n = len(points)
    gap, bw = 2, max(4, min(28, (width - 2 * (n - 1)) // n))
    vals = [p.get("value") or 0 for p in points]
    vmax = max(vals) or 1
    bars = []
    for i, p in enumerate(points):
        h = max(3, round((vals[i] / vmax) * (height - 4)))
        x, y = i * (bw + gap), height - h
        s = p.get("severity") if p.get("severity") in SEV else "info"
        tip = f"{p.get('captured_at', '')[:19].replace('T', ' ')} · {s} · {p.get('title') or ''} · {p.get('metric', '')} {fmt(p.get('value'))}"
        bars.append(f"<a href='/s/{_t(p['id'])}'><rect x='{x}' y='{y}' width='{bw}' height='{h}' rx='2' fill='var(--{s})'>"
                    f"<title>{_t(tip)}</title></rect></a>")
    total = n * bw + (n - 1) * gap
    return (f"<svg class='chart strip' viewBox='0 0 {total} {height}' width='{min(total, width)}' height='{height}' role='img' "
            f"aria-label='{n} snapshots, newest right'>{''.join(bars)}</svg>")


def _pct_w(frac: float) -> str:
    """CSS width for a bar: at least a 1px sliver so a zero still shows where the bar would be."""
    return f"max(1px,{max(0.0, min(1.0, frac)) * 100:.1f}%)"


def waits_chart(current: dict, reference: dict, top: int = 8) -> str:
    """Paired bars per wait class: current (filled) over reference (outlined). Plain HTML, so labels take the
    page's text colours in either theme; each row's title carries the exact numbers."""
    current, reference = current or {}, reference or {}  # postgres stores null when it has no wait classes
    classes = sorted(set(current) | set(reference), key=lambda c: -max(current.get(c, {}).get("share", 0), reference.get(c, {}).get("share", 0)))[:top]
    if not classes:
        return ""
    rows = []
    for c in classes:
        cur, ref = current.get(c, {}), reference.get(c, {})
        cs, rs = cur.get("share", 0) or 0, ref.get("share", 0) or 0
        tip = f"{c}: now {cs:.0%} ({fmt(cur.get('value'))} ms) · reference {rs:.0%} ({fmt(ref.get('value'))} ms)"
        rows.append(f"<span class='k' title='{_t(tip)}'>{_t(c)}</span>"
                    f"<div class='pair' title='{_t(tip)}'><i class='cur' style='width:{_pct_w(cs)}'></i><i class='ref' style='width:{_pct_w(rs)}'></i></div>"
                    f"<span class='v'><b>{cs:.0%}</b> vs {rs:.0%}</span>")
    return ("<figure class='chart'><figcaption class='row'><span>Wait time by class</span>"
            "<span class='legend'><span><i class='sw fill'></i>current</span><span><i class='sw line'></i>reference</span></span></figcaption>"
            f"<div class='bars waits' role='table' aria-label='wait classes, current vs reference'>{''.join(rows)}</div></figure>")


def delta_chart(load_profile: dict, top: int = 8, clamp: float = 100.0) -> str:
    """% change vs reference for the metrics that moved most, one bar each, length = size of the change
    (capped at 100%), colour = direction. Neither direction is 'bad' by itself; the review text says which."""
    items = [(k, v) for k, v in (load_profile or {}).items() if isinstance(v, dict) and v.get("pct") is not None and k != "elapsed_s"]
    items.sort(key=lambda kv: -abs(kv[1]["pct"]))
    items = items[:top]
    if not items:
        return ""
    rows = []
    for k, v in items:
        pct = v["pct"]
        cur = v.get("current_per_s", v.get("current"))
        ref = v.get("reference_per_s", v.get("reference"))
        unit = "/s" if "current_per_s" in v else ""
        tip = f"{k}: {fmt(ref)}{unit} -> {fmt(cur)}{unit} ({pct:+.0f}%)"
        rows.append(f"<span class='k' title='{_t(tip)}'>{_t(k)}</span>"
                    f"<div class='one' title='{_t(tip)}'><i class='{'up' if pct >= 0 else 'down'}' style='width:{_pct_w(min(abs(pct), clamp) / clamp)}'></i></div>"
                    f"<span class='v'><b>{pct:+.0f}%</b> {fmt(ref)} → {fmt(cur)}{unit}</span>")
    return ("<figure class='chart'><figcaption class='row'><span>Load profile vs reference</span>"
            "<span class='legend'><span><i class='sw up'></i>up</span><span><i class='sw down'></i>down</span></span></figcaption>"
            f"<div class='bars delta' role='table' aria-label='load profile change'>{''.join(rows)}</div></figure>")


def share_bar(share: float, width: int = 80) -> str:
    """A single bar for a table cell: share of SQL time."""
    share = max(0.0, min(1.0, share or 0.0))
    return (f"<span class='share' title='{share:.0%} of SQL time'><span class='track'><i style='width:{_pct_w(share)}'></i></span>"
            f"<span class='pct'>{share:.0%}</span></span>")


def stat_tiles(counts: dict[str, int]) -> str:
    """KPI row: how many targets sit at each severity right now."""
    tiles = []
    for s, label in (("high", "Incident"), ("medium", "Degraded"), ("low", "Minor"), ("info", "Healthy")):
        tiles.append(f"<div class='tile'><span class='lbl'>{label}</span><span class='num {s}'>{counts.get(s, 0)}</span></div>")
    return "<div class='tiles'>" + "".join(tiles) + "</div>"


def trend_chart(points: list[dict], metric: str, width: int = 760, height: int = 180, marker_key: str = "severity") -> str:
    """Line over time with one marker per snapshot. points: [{id, t (datetime), value, severity, title, ...}],
    oldest first. Markers are coloured by severity (status colour, named in the tooltip and in the table
    beneath); a point with plan_changed=True gets a diamond so a plan flip is visible without colour."""
    pts = [p for p in points if p.get("value") is not None and p.get("t") is not None]
    if not pts:
        return ""
    left, right, top, bottom = 56, 16, 12, 30
    pw, ph = width - left - right, height - top - bottom
    gid = f"g{abs(hash(metric)) % 100000}"
    t0, t1 = pts[0]["t"], pts[-1]["t"]
    span = (t1 - t0).total_seconds() or 1.0
    vmax = max(p["value"] for p in pts) or 1.0

    def x(p):
        return left + (p["t"] - t0).total_seconds() / span * pw if len(pts) > 1 else left + pw / 2

    def y(v):
        return top + ph - (v / vmax) * ph

    grid = []
    for frac, lab in ((1.0, fmt(vmax)), (0.5, fmt(vmax / 2)), (0.0, "0")):
        gy = top + ph - frac * ph
        dash = " stroke-dasharray='4 4'" if frac == 0.5 else ""
        grid.append(f"<line x1='{left}' y1='{gy:.1f}' x2='{left + pw}' y2='{gy:.1f}' stroke='var(--line{'2' if frac == 0 else ''})'{dash}/>"
                    f"<text x='{left - 8}' y='{gy + 4:.1f}' text-anchor='end' class='lbl'>{lab}</text>")
    first, last = pts[0]["t"], pts[-1]["t"]
    same_day = first.date() == last.date()
    xl = [f"<text x='{left}' y='{height - 8}' class='lbl mut'>{first.strftime('%H:%M' if same_day else '%m-%d %H:%M')}</text>"]
    if len(pts) > 1:
        xl.append(f"<text x='{left + pw}' y='{height - 8}' text-anchor='end' class='lbl mut'>{last.strftime('%H:%M' if same_day else '%m-%d %H:%M')}</text>")
    path = " ".join(f"{'M' if i == 0 else 'L'}{x(p):.1f},{y(p['value']):.1f}" for i, p in enumerate(pts))
    area = f"M{x(pts[0]):.1f},{top + ph:.1f} " + " ".join(f"L{x(p):.1f},{y(p['value']):.1f}" for p in pts) + f" L{x(pts[-1]):.1f},{top + ph:.1f} Z"
    marks = []
    for p in pts:
        s = p.get("severity") if p.get("severity") in SEV else "info"
        cx, cy = x(p), y(p["value"])
        tip = f"{p['t'].strftime('%Y-%m-%d %H:%M')} · {metric} {fmt(p['value'])} · {s}" + (f" · {p['title']}" if p.get("title") else "") + (" · plan changed" if p.get("plan_changed") else "")
        shape = (f"<polygon points='{cx:.1f},{cy - 7:.1f} {cx + 7:.1f},{cy:.1f} {cx:.1f},{cy + 7:.1f} {cx - 7:.1f},{cy:.1f}'" if p.get("plan_changed")
                 else f"<circle cx='{cx:.1f}' cy='{cy:.1f}' r='5.5'")
        marks.append(f"<a href='/s/{_t(p['id'])}'>{shape} fill='var(--{s})' stroke='var(--card)' stroke-width='2'><title>{_t(tip)}</title>{'</polygon>' if p.get('plan_changed') else '</circle>'}</a>")
    return (f"<svg class='chart trend' viewBox='0 0 {width} {height}' width='100%' role='img' aria-label='{_t(metric)} over {len(pts)} snapshots'>"
            f"<defs><linearGradient id='{gid}' x1='0' y1='0' x2='0' y2='1'><stop offset='0' stop-color='var(--info)' stop-opacity='0.18'/><stop offset='1' stop-color='var(--info)' stop-opacity='0'/></linearGradient></defs>"
            f"{''.join(grid)}{''.join(xl)}<path d='{area}' fill='url(#{gid})'/>"
            f"<path d='{path}' fill='none' stroke='var(--info)' stroke-width='2' stroke-linejoin='round'/>{''.join(marks)}</svg>")
