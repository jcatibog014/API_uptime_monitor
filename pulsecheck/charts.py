"""Charts, drawn on the server as SVG geometry (no chart library, no JavaScript needed).

The templates turn these numbers into <path>, <rect> and <text>: sizes are SVG
attributes, which the strict CSP allows (inline styles it does not).

Colours (style.css, checked with the dataviz validator against both themes'
card colours):
- response times: ONE series hue (blue). Green is reserved for "up", so the
  chart doesn't borrow it. The typical time (p50) is a 2px line; the slowest
  5% (p95) is a 10% wash of the same hue above it;
- the uptime strip uses the status colours, never alone: a day's bar is also
  SHORTER the more it was down (red/green look alike to many people), and
  every bar has a hover label and a table.
"""
import math

from .formatting import local

WIDTH, HEIGHT = 640, 230
LEFT, RIGHT, TOP, BOTTOM = 64, 10, 12, 30
STRIP_WIDTH, STRIP_HEIGHT, STRIP_GAP = 900, 34, 2
STRIP_SHARE = {"up": 1.0, "some": 0.62, "down": 0.34}  # bar height by level (the second signal)
SPARK_WIDTH, SPARK_HEIGHT = 120, 30
X_LABELS = 6


def nice_top(value):
    """A round number at or above value: 1, 2, 2.5, 5 times a power of ten."""
    if not value or value <= 0:
        return 100.0
    power = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        if value <= step * power:
            return step * power
    return 10 * power


def ms_label(value):
    return f"{value / 1000:g} s" if value >= 1000 else f"{value:g} ms"


def time_label(moment, period):
    stamp = local(moment)
    if period == "24h":
        return f"{stamp.hour % 12 or 12} {'AM' if stamp.hour < 12 else 'PM'}"
    if period == "7d":
        return f"{stamp:%a} {stamp.day}"
    return f"{stamp:%b} {stamp.day}"


def response_chart(points, period):
    """Geometry for the response-time chart, or None when there is nothing to draw."""
    if not any(point.p50 is not None for point in points):
        return None
    top = nice_top(max(point.p95 or point.p50 or 0 for point in points) * 1.08)
    inner_w, inner_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM
    count = len(points)
    step = inner_w / max(count - 1, 1)

    def x(n):
        return LEFT + n * step

    def y(value):
        return TOP + inner_h * (1 - min(value, top) / top)

    lines, bands, run = [], [], []
    for n, point in enumerate(points + [None]):
        if point is not None and point.p50 is not None:
            run.append((n, point))
            continue
        if run:
            lines.append("M" + " L".join(f"{x(i):.1f} {y(p.p50):.1f}" for i, p in run))
            upper = " L".join(f"{x(i):.1f} {y(p.p95 or p.p50):.1f}" for i, p in run)
            lower = " L".join(f"{x(i):.1f} {y(p.p50):.1f}" for i, p in reversed(run))
            bands.append(f"M{upper} L{lower} Z")
            if len(run) == 1:  # one lone point: a dot instead of a line nobody sees
                lines[-1] += " l0.1 0"
            run = []
    ticks = [{"y": round(y(top * share), 1), "label": ms_label(top * share)} for share in (0, .25, .5, .75, 1)]
    every = max(1, round(count / X_LABELS))
    labels = [{"x": round(x(n), 1), "label": time_label(point.start, period)}
              for n, point in enumerate(points) if n % every == 0 and n < count - every / 2]
    hits = [{"x": round(x(n) - step / 2, 1), "width": round(step, 1), "point": point} for n, point in enumerate(points)]
    return {"width": WIDTH, "height": HEIGHT, "left": LEFT, "right": WIDTH - RIGHT, "top": TOP,
            "bottom": HEIGHT - BOTTOM, "lines": lines, "bands": bands, "ticks": ticks, "labels": labels, "hits": hits}


def strip(days):
    """Geometry for the uptime strip: one bar per day, bottom-aligned."""
    count = len(days)
    width = (STRIP_WIDTH - STRIP_GAP * (count - 1)) / count
    bars = []
    for n, day in enumerate(days):
        share = STRIP_SHARE.get(day.level)
        height = STRIP_HEIGHT * share if share else 3
        bars.append({"x": round(n * (width + STRIP_GAP), 2), "y": round(STRIP_HEIGHT - height, 2),
                     "width": round(width, 2), "height": round(height, 2), "day": day})
    return {"width": STRIP_WIDTH, "height": STRIP_HEIGHT, "bars": bars}


def sparkline(values):
    """Points for a small line of recent response times (None for a failed check: the line
    drops to the bottom there, like a missed beat). None when there's nothing to draw."""
    if len(values) < 2:
        return None
    good = [value for value in values if value is not None]
    top = max(good) * 1.15 if good else 1
    step = SPARK_WIDTH / (len(values) - 1)
    points = []
    for n, value in enumerate(values):
        height = 0 if value is None else (value / top) * (SPARK_HEIGHT - 4)
        points.append(f"{n * step:.1f},{SPARK_HEIGHT - 2 - height:.1f}")
    return {"width": SPARK_WIDTH, "height": SPARK_HEIGHT, "points": " ".join(points),
            "end_x": round((len(values) - 1) * step, 1), "end_y": float(points[-1].split(",")[1]),
            "failed": values[-1] is None}
