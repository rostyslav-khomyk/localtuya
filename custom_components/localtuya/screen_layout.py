"""Physical zone allocation and the verified variable-palette UDP payload."""

import math

EDGES = ("left", "top", "right", "bottom")
SEGMENTS = 16


def zone_counts(lengths_cm, segments=SEGMENTS):
    """Turn centimetre lengths into segment counts that add up to `segments`.

    Manual lights support up to four physical zones; streaming uses 20 sections.
    """
    lengths = [int(n) for n in lengths_cm if int(n) > 0]
    if not lengths or len(lengths) > 4:
        raise ValueError("Use 1 to 4 zones")
    total = sum(lengths)
    exact = [cm * segments / total for cm in lengths]
    counts = [int(value) for value in exact]
    leftover = segments - sum(counts)
    order = sorted(range(len(exact)), key=lambda i: exact[i] - counts[i], reverse=True)
    for index in order:
        if leftover <= 0:
            break
        counts[index] += 1
        leftover -= 1
    for index, count in enumerate(counts):
        if count == 0:
            donor = max(range(len(counts)), key=lambda i: counts[i])
            if counts[donor] > 1:
                counts[donor] -= 1
                counts[index] = 1
    return counts


def color_payload(colors, counts):
    """Build the palette-count + HSV slots + section mapping payload. `colors` are RGB tuples in zone order.

    Unused colour slots clone the last colour — same as battletron-zones/strip.js.
    Off zones must pass (0,0,0) explicitly in `colors`, not rely on padding.
    """
    colors = list(colors)
    counts = list(counts)
    if not 1 <= len(colors) <= 20 or len(counts) != len(colors):
        raise ValueError("Use one count per color, at most 20 colors")
    if any(type(n) is not int or n < 0 for n in counts) or not 1 <= sum(counts) <= 20:
        raise ValueError("Use 1 to 20 mapped sections")
    if any(len(rgb) != 3 or any(type(c) is not int or not 0 <= c <= 255 for c in rgb) for rgb in colors):
        raise ValueError("RGB channels must be integers from 0 to 255")
    slots = [_hsv_word(rgb) for rgb in colors]
    while len(slots) < 4:
        slots.append(slots[-1])
    mapping = "".join(f"{index + 1:02x}" * count for index, count in enumerate(counts))
    return f"{len(slots):04x}" + "".join(slots) + f"{sum(counts):04x}" + mapping



def _hsv_word(rgb):
    hue, sat, val = _rgb_to_hsv(rgb)
    return f"{hue:04x}{sat:04x}{val:04x}"


def _rgb_to_hsv(rgb):
    r, g, b = [int(channel) for channel in rgb]
    high = max(r, g, b)
    low = min(r, g, b)
    hue = 0
    if high != low:
        if high == r:
            hue = 60 * ((g - b) / (high - low)) + (360 if g < b else 0)
        elif high == g:
            hue = 60 * ((b - r) / (high - low)) + 120
        else:
            hue = 60 * ((r - g) / (high - low)) + 240
    sat = 0 if high == 0 else 1 - low / high
    val = math.floor((high / 255) * 1000 + 0.5)
    # Match JavaScript Math.round, including values exactly halfway.
    return math.floor(hue + 0.5) % 360, math.floor(sat * 1000 + 0.5), val
