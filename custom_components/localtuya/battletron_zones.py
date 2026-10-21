"""UDP zone frames for Battletron strips. Same framing as the Mac strip.js client."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import socket
import struct
import threading
import time
import zlib

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .screen_layout import color_payload

_LOGGER = logging.getLogger(__name__)

PORT = 40001
HEADER = bytes.fromhex("00006699")
TAIL = bytes.fromhex("00009966")
FIXED_KEY = bytes.fromhex("6f36045d84b042e01e29b7c819e37cf7")
CONTROLLER_UUID = "420691337420b00b"
NEGOTIATE = 0x05
NEGOTIATE_REPLY = 0x06
COLORS = 0x10


def _crc32(buf: bytes) -> bytes:
    return struct.pack(">I", zlib.crc32(buf) & 0xFFFFFFFF)


def _pad_id(text: str) -> bytes:
    return bytes.fromhex(text.encode("utf-8").hex().ljust(50, "0") + "00")


def _gcm(plain: bytes, nonce: bytes, aad: bytes | None, key: bytes) -> bytes:
    return AESGCM(key).encrypt(nonce, plain, aad)


def _aad(sequence: int, kind: int, controller_crc: bytes, payload_len: int, devices: int) -> bytes:
    seq = f"{sequence:08x}"
    type_hex = f"{kind:08x}"
    total = f"{42 + payload_len:08x}"
    frames = f"{devices:08x}"
    return bytes.fromhex("0000" + seq + type_hex) + controller_crc + bytes.fromhex(total + frames)


def _route(host: str) -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((host, PORT))
        return probe.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        probe.close()


def _targets(host: str, bind_ip: str) -> list[str]:
    """Match strip.js: subnet broadcast first, then the strip unicast."""
    parts = (bind_ip if bind_ip != "0.0.0.0" else host).split(".")
    targets: list[str] = []
    if len(parts) == 4:
        targets.append(".".join(parts[:3] + ["255"]))
    targets.append(host)
    return list(dict.fromkeys(targets))


class ZoneSender:
    """One shared UDP socket. Several strips can use it (only one process may bind 40001)."""

    def __init__(self):
        self._sock: socket.socket | None = None
        self._devices: dict[str, dict] = {}
        # paint_now (executor) and the zone thread must not race-bind :40001.
        self._io = threading.Lock()

    def add(self, device_id: str, local_key: str, host: str) -> None:
        raw = local_key.encode("utf-8")
        key = bytes.fromhex(raw.hex().ljust(32, "0")[:32])
        controller_crc = _crc32(_pad_id(CONTROLLER_UUID))
        device_crc = _crc32(_pad_id(device_id) + controller_crc)
        bind_ip = _route(host)
        token = os.urandom(16)
        rnd = os.urandom(16)
        self._devices[device_id] = {
            "key": key,
            "host": host,
            "bind_ip": bind_ip,
            # Unicast first (reliable), then subnet broadcast — strip.js reverses
            # this order on the Mac; HA's host needs unicast to win.
            "targets": list(reversed(_targets(host, bind_ip))),
            "controller_crc": controller_crc,
            "device_crc": device_crc,
            "session": None,
            "sequence": 1,
            "token": token,
            "rnd": rnd,
            "negotiation": _gcm(token, token[:12], None, key)[:16],
        }

    def close(self) -> None:
        with self._io:
            sock = self._sock
            self._sock = None
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
            self._devices.clear()

    def _socket_unlocked(self) -> socket.socket:
        if self._sock:
            return self._sock
        bind_ip = next(
            (dev["bind_ip"] for dev in self._devices.values() if dev.get("bind_ip")),
            "0.0.0.0",
        )
        last_err: OSError | None = None
        for attempt in range(12):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # Match strip.js (reuseAddr: false) — SO_REUSEADDR let two threads
            # both bind and then split negotiate replies from colour sends.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                sock.bind((bind_ip, PORT))
                sock.settimeout(0.05)
                self._sock = sock
                _LOGGER.info("Zone socket on %s for %s", bind_ip, ",".join(self._devices))
                return sock
            except OSError as err:
                last_err = err
                try:
                    sock.close()
                except OSError:
                    pass
                # Previous painter may still be releasing UDP 40001.
                time.sleep(0.1 * (attempt + 1))
        assert last_err is not None
        raise last_err

    def negotiate(self, device_id: str) -> None:
        with self._io:
            dev = self._devices.get(device_id)
            if not dev:
                return
            plain = b"\x00\x00\x00\x00" + dev["token"] + dev["rnd"]
            self._send_unlocked(dev, NEGOTIATE, plain, FIXED_KEY)

    def send_colors(self, device_id: str, colors, counts) -> None:
        with self._io:
            dev = self._devices.get(device_id)
            if not dev or not dev["session"]:
                return
            plain = bytes.fromhex(color_payload(colors, counts))
            self._send_unlocked(dev, COLORS, plain, dev["session"])

    def _send_unlocked(self, dev: dict, kind: int, plain: bytes, key: bytes) -> None:
        nonce = os.urandom(12)
        extra = _aad(dev["sequence"], kind, dev["controller_crc"], len(plain) + 24, 1)
        dev["sequence"] = (dev["sequence"] + 1) & 0xFFFFFFFF
        blob = _gcm(plain, nonce, extra, key)
        enc, tag = blob[:-16], blob[-16:]
        packet = b"".join(
            [
                HEADER,
                extra,
                nonce,
                dev["device_crc"],
                struct.pack(">I", len(enc)),
                enc,
                tag,
                TAIL,
            ]
        )
        sock = self._socket_unlocked()
        for target in dev["targets"]:
            try:
                sock.sendto(packet, (target, PORT))
            except OSError as err:
                _LOGGER.debug("Zone send to %s failed: %s", target, err)

    def pump(self) -> str | None:
        """Read one negotiate reply. Returns the device id that completed."""
        with self._io:
            sock = self._socket_unlocked()
            try:
                msg, _addr = sock.recvfrom(2048)
            except (TimeoutError, BlockingIOError, OSError):
                return None
            if len(msg) < 64 or not msg.startswith(HEADER) or not msg.endswith(TAIL):
                return None
            kind = struct.unpack(">I", msg[10:14])[0]
            if kind != NEGOTIATE_REPLY:
                return None
            data_len = struct.unpack(">I", msg[42:46])[0]
            frame_aad = msg[4:26]
            nonce = msg[26:38]
            enc = msg[46 : 46 + data_len]
            tag = msg[46 + data_len : 46 + data_len + 16]
            for device_id, dev in self._devices.items():
                if dev["session"]:
                    continue
                try:
                    plain = AESGCM(dev["negotiation"]).decrypt(nonce, enc + tag, frame_aad)
                except Exception:
                    continue
                if len(plain) < 80:
                    continue
                rnd_b = plain[:16]
                rnd_hmac = plain[16:48]
                session_hmac = plain[48:80]
                if hmac.new(dev["negotiation"], dev["rnd"], hashlib.sha256).digest() != rnd_hmac:
                    continue
                mixed = bytes(a ^ b for a, b in zip(dev["rnd"], rnd_b))
                session = _gcm(mixed, dev["rnd"][:12], None, dev["negotiation"])[:16]
                if hmac.new(dev["negotiation"], session, hashlib.sha256).digest() != session_hmac:
                    continue
                dev["session"] = session
                return device_id
            return None


def _zone_thread(sender: ZoneSender, device_ids: list[str], color_fn, interval: float, stop: threading.Event):
    """Blocking loop. Matches the node setInterval clients."""
    ready: set[str] = set()
    painting: set[str] = set()
    last_negotiate = 0.0
    try:
        while not stop.is_set():
            if not sender._devices:
                break
            now = time.monotonic()
            if now - last_negotiate >= 1.0:
                for device_id in device_ids:
                    if device_id not in ready and device_id in sender._devices:
                        sender.negotiate(device_id)
                last_negotiate = now
            finished = sender.pump()
            if finished:
                ready.add(finished)
                _LOGGER.info("Zone session ready for %s", finished)
            for device_id in list(ready):
                if device_id not in sender._devices:
                    continue
                colors, counts = color_fn(device_id)
                if not colors:
                    continue
                if device_id not in painting:
                    painting.add(device_id)
                    _LOGGER.info("Zone UDP session painting %s", device_id[:8])
                sender.send_colors(device_id, colors, counts)
            stop.wait(interval)
    except Exception:
        _LOGGER.exception("Zone painter thread crashed")
    finally:
        # Leave socket close to the async caller so restart can wait on it.
        pass


async def run_zone_loop(sender: ZoneSender, device_ids, color_fn, interval=0.125):
    """Run the zone painter in a worker thread so timing matches strip.js."""
    stop = threading.Event()
    loop = asyncio.get_running_loop()
    task = loop.run_in_executor(
        None, _zone_thread, sender, list(device_ids), color_fn, interval, stop
    )
    try:
        await task
    except asyncio.CancelledError:
        raise
    except Exception:
        _LOGGER.exception("Zone painter task failed")
    finally:
        stop.set()
        if not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=3)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        sender.close()
