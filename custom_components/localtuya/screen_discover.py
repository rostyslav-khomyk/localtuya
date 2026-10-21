"""Find Hyperion screen servers and check a token against one."""

import asyncio
import re
from urllib.parse import urlparse

import aiohttp

SSDP_ST = "urn:hyperion-project.org:device:basic:1"
_SEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 1\r\n"
    f"ST: {SSDP_ST}\r\n"
    "\r\n"
).encode()


class _Collector(asyncio.DatagramProtocol):
    def __init__(self):
        self.packets = []

    def datagram_received(self, data, addr):
        self.packets.append((data, addr))


def _header(text, name):
    match = re.search(rf"(?im)^{re.escape(name)}:\s*(.+)$", text)
    return match.group(1).strip() if match else ""


def _xml_text(xml, tag):
    match = re.search(rf"<{tag}>([^<]+)</{tag}>", xml)
    return match.group(1).strip() if match else ""


async def async_discover_screen_servers(timeout=1.5):
    """Return Hyperion servers answering the SSDP search."""
    loop = asyncio.get_running_loop()
    try:
        transport, protocol = await loop.create_datagram_endpoint(
            _Collector, local_addr=("0.0.0.0", 0), allow_broadcast=True
        )
    except OSError:
        return []
    transport.sendto(_SEARCH, ("239.255.255.250", 1900))
    await asyncio.sleep(timeout)
    packets = list(protocol.packets)
    transport.close()

    found = []
    seen = set()
    timeout_cfg = aiohttp.ClientTimeout(total=2)
    async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
        for data, addr in packets:
            text = data.decode(errors="replace")
            if SSDP_ST not in text and "Screen Edges" not in text:
                continue
            host = addr[0]
            port = 8090
            name = host
            location = _header(text, "LOCATION")
            if location:
                parsed = urlparse(location)
                host = parsed.hostname or host
                try:
                    async with session.get(location) as response:
                        xml = await response.text()
                    name = _xml_text(xml, "friendlyName") or name
                    ws = _xml_text(xml, "websocket")
                    if ws.isdigit():
                        port = int(ws)
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass
            key = (host, port)
            if key in seen:
                continue
            seen.add(key)
            found.append({"name": name, "host": host, "port": port})
    return found


async def async_test_screen_server(host, port, token):
    """Log in to a Hyperion websocket. Returns an error string, or None."""
    url = f"ws://{host}:{int(port)}"
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(url, compress=0) as ws:
                await ws.send_json(
                    {
                        "command": "authorize",
                        "subcommand": "login",
                        "token": token,
                        "tan": 1,
                    }
                )
                login = await ws.receive_json(timeout=4)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return "screen_down"
    if not isinstance(login, dict) or not login.get("success"):
        return "screen_auth"
    return None
