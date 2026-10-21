"""Zone lengths for a strip that can show at most four colors at once."""

EDGES = ("left", "top", "right", "bottom")
SEGMENTS = 16


def zone_counts(lengths_cm, segments=SEGMENTS):
    """Turn centimetre lengths into segment counts that add up to `segments`.

    The Battletron zone packet has four color slots. Each length becomes one slot.
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
    """Build the 0004 HSV payload. `colors` are RGB tuples in zone order."""
    slots = [_hsv_word(rgb) for rgb in list(colors)[:4]]
    # Always pad unused slots with black — never clone the last colour.
    black = _hsv_word((0, 0, 0))
    while len(slots) < 4:
        slots.append(black)
    segments = sum(counts)
    mapping = "".join(f"{index + 1:02x}" * count for index, count in enumerate(counts))
    return "0004" + "".join(slots) + f"{segments:04x}" + mapping


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
    val = round((high / 255) * 1000)
    return round(hue) % 360, round(sat * 1000), val
