"""Validated Hyperion frames and physical zone mapping; no HA or socket I/O."""

import math

from .screen_layout import zone_counts

SAMPLES = 20
EDGES = ("left", "top", "right", "bottom")


def decode_frame(message):
    if not isinstance(message, dict) or message.get("command") != "ledcolors-ledstream-update":
        return None
    data = message.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("leds"), list):
        raise ValueError("Missing Hyperion LED array")
    leds = data["leds"]
    if len(leds) != SAMPLES * len(EDGES):
        raise ValueError("Expected 80 RGB samples (left/top/right/bottom, 20 each)")
    colors = []
    for led in leds:
        rgb = led.get("RGB Value") if isinstance(led, dict) else led
        if not isinstance(rgb, list) or len(rgb) != 3 or any(type(c) is not int or not 0 <= c <= 255 for c in rgb):
            raise ValueError("Invalid RGB sample")
        colors.append(tuple(rgb))
    return {edge: colors[i * SAMPLES : (i + 1) * SAMPLES] for i, edge in enumerate(EDGES)}


def resample(colors, count, reverse=False):
    """Area average, preserving the entire edge when reducing spatial resolution."""
    source = list(reversed(colors)) if reverse else colors
    result = []
    scale = len(source) / count
    for index in range(count):
        start, end = index * scale, (index + 1) * scale
        sums = [0.0, 0.0, 0.0]
        for pos in range(math.floor(start), min(len(source), math.ceil(end))):
            weight = min(end, pos + 1) - max(start, pos)
            for channel in range(3):
                sums[channel] += source[pos][channel] * weight
        result.append(tuple(round(value / scale) for value in sums))
    return result


def map_strip(lengths, mappings, edges):
    """Zone index -> (edge, reverse); unassigned physical sections stay black."""
    counts = zone_counts(lengths, segments=SAMPLES)
    result = []
    for index, count in enumerate(counts):
        mapping = mappings.get(index)
        result.extend(resample(edges[mapping[0]], count, mapping[1]) if mapping else [(0, 0, 0)] * count)
    return result
