"""Chart geometry in percentages, so templates can lay charts out with CSS at any width."""

import math


def nice_max(value: int) -> int:
    """Round up to 1, 2, 2.5 or 5 x 10^n so axis ticks land on clean numbers."""
    if value <= 0:
        return 100
    magnitude = 10 ** math.floor(math.log10(value))
    return next(int(step * magnitude) for step in (1, 2, 2.5, 5, 10) if step * magnitude >= value)


def scale(values: list[int]) -> tuple[list[float], list[tuple[float, int]]]:
    """Each value as % of the axis height, plus (percent, cents) gridline ticks."""
    top = nice_max(max(values, default=0))
    return [100 * v / top for v in values], [(100 * t / top, t) for t in (0, top // 2, top)]


def step_path(heights: list[float]) -> tuple[list[float], str]:
    """x positions (%) and an SVG path in a 0-100 box that holds each price flat until it changes."""
    xs = [50.0] if len(heights) == 1 else [100 * i / (len(heights) - 1) for i in range(len(heights))]
    path = ""
    for i, (x, h) in enumerate(zip(xs, heights)):
        y = 100 - h
        path += f"M{x:.2f},{y:.2f}" if i == 0 else f"H{x:.2f}V{y:.2f}"
    if len(heights) == 1:
        path = f"M0,{100 - heights[0]:.2f}H100"
    return xs, path
