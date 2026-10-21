"""UDP zone frames for Battletron strips. Same framing as the Mac proof of concept."""

import asyncio
import hashlib
import hmac
import logging
import os
import socket
import struct
import zlib

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_LOGGER = logging.getLogger(__name__)

from .screen_layout import color_payload

PORT = 40001
HEADER = bytes.fromhex("00006699")
TAIL = bytes.fromhex("00009966")
FIXED_KEY = bytes.fromhex("6f36045d84b042e01e29b7c819e37cf7")
CONTROLLER_UUID = "420691337420b00b"


def _crc32(buf):
    return struct.pack(">I", zlib.crc32(buf) & 0xFFFFFFFF)


def _pad_id(text):
    return bytes.fromhex(text.encode("utf-8").hex().ljust(50, "0") + "00")


def _gcm(plain, nonce, aad, key):
    return AESGCM(key).encrypt(nonce, plain, aad)


def aad(sequence, kind, controller_crc, payload_len, devices):
    """22-byte additional data: version, seq, type, controller crc, length, count."""
    seq = f"{sequence:08x}"
    type_hex = f"{kind:08x}"
    total = f"{42 + payload_len:08x}"
    frames = f"{devices:08x}"
    return bytes.fromhex("0000" + seq + type_hex) + controller_crc + bytes.fromhex(total + frames)


class ZoneSender:
    """One UDP socket. Several strips can share it."""

    def __init__(self):
        self._sock = None
        self._devices = {}

    def add(self, device_id, local_key, host):
        raw = local_key.encode("utf-8")
        key = bytes.fromhex(raw.hex().ljust(32, "0")[:32])
        controller_crc = _crc32(_pad_id(CONTROLLER_UUID))
        device_crc = _crc32(_pad_id(device_id) + controller_crc)
        bind_ip = _route(host)
        self._devices[device_id] = {
            "key": key,
            "host": host,
            "bind_ip": bind_ip,
            "targets": _targets(host, bind_ip),
            "controller_crc": controller_crc,
            "device_crc": device_crc,
            "session": None,
            "sequence": 1,
            "token": os.urandom(16),
            "rnd": os.urandom(16),
        }
        token = self._devices[device_id]["token"]
        self._devices[device_id]["negotiation"] = _gcm(token, token[:12], None, key)[:16]

    def close(self):
        if self._sock:
            self._sock.close()
            self._sock = None

    def _socket(self):
        if self._sock:
            return self._sock
        bind_ip = next((dev["bind_ip"] for dev in self._devices.values() if dev.get("bind_ip")), "0.0.0.0")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind((bind_ip, PORT))
        sock.setblocking(False)
        self._sock = sock
        return sock

    def negotiate(self, device_id):
        dev = self._devices[device_id]
        plain = b"\x00\x00\x00\x00" + dev["token"] + dev["rnd"]
        self._send(dev, 0x05, plain, FIXED_KEY, negotiating=True)

    def send_colors(self, device_id, colors, counts):
        dev = self._devices[device_id]
        if not dev["session"]:
            return
        plain = bytes.fromhex(color_payload(colors, counts))
        self._send(dev, 0x10, plain, dev["session"], negotiating=False)

    def _send(self, dev, kind, plain, key, negotiating):
        nonce = os.urandom(12)
        extra = aad(dev["sequence"], kind, dev["controller_crc"], len(plain) + 24, 1)
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
        sock = self._socket()
        for target in dev["targets"]:
            try:
                sock.sendto(packet, (target, PORT))
            except OSError:
                continue

    def pump(self):
        """Read one reply, if any. Returns the device id that completed negotiation."""
        sock = self._socket()
        try:
            msg, _addr = sock.recvfrom(2048)
        except BlockingIOError:
            return None
        if len(msg) < 64 or not msg.startswith(HEADER) or not msg.endswith(TAIL):
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


async def run_zone_loop(sender, device_ids, color_fn, interval=0.25):
    """Negotiate until each strip answers, then keep sending colors."""
    loop = asyncio.get_running_loop()
    ready = set()
    painting = set()
    last_negotiate = 0.0
    try:
        while True:
            now = loop.time()
            if now - last_negotiate >= 1:
                for device_id in device_ids:
                    if device_id not in ready:
                        await loop.run_in_executor(None, sender.negotiate, device_id)
                last_negotiate = now
            finished = await loop.run_in_executor(None, sender.pump)
            if finished:
                ready.add(finished)
                _LOGGER.info("Zone session ready for %s", finished)
            for device_id in ready:
                colors, counts = color_fn(device_id)
                if colors:
                    if device_id not in painting:
                        painting.add(device_id)
                        _LOGGER.info("Painting %s from the screen", device_id)
                    await loop.run_in_executor(None, sender.send_colors, device_id, colors, counts)
            await asyncio.sleep(interval)
    finally:
        sender.close()


def _route(host):
    """Source address this machine uses to reach the strip."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((host, PORT))
        return probe.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        probe.close()


def _targets(host, bind_ip):
    """Unicast the strip, and also the subnet broadcast the Mac client uses."""
    targets = [host]
    parts = (bind_ip if bind_ip != "0.0.0.0" else host).split(".")
    if len(parts) == 4:
        targets.append(".".join(parts[:3] + ["255"]))
    return list(dict.fromkeys(targets))
