"""Offline tests against HA 2026.10.2; no household devices are contacted."""

import asyncio
import copy
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
from aiohttp import web
import pytest
import pytest_asyncio

from custom_components.localtuya import granular, zone_lights
from custom_components.localtuya.granular import decode_frame, map_strip, resample
from custom_components.localtuya.screen_layout import color_payload, zone_counts
from custom_components.localtuya.screen_stream import ScreenStreamView, StreamController
from custom_components.localtuya.zone_lights import BattletronZoneLight, ZonePaintHub
from homeassistant.exceptions import HomeAssistantError
from homeassistant.core import HomeAssistant
from homeassistant.auth import auth_manager_from_config
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_READ_ONLY
from homeassistant.components.http.server import HomeAssistantHTTP


def wire_frame(colors=None):
    return {"command": "ledcolors-ledstream-update", "data": {"leds": colors or [[i, 0, 255 - i] for i in range(80)]}}


def test_frame_contract_and_order():
    frame = decode_frame(wire_frame())
    assert frame["left"][0] == (0, 0, 255)
    assert frame["top"][0] == (20, 0, 235)
    assert frame["bottom"][-1] == (79, 0, 176)
    assert decode_frame({"command": "ledcolors-ledstream-start", "success": True}) is None


@pytest.mark.parametrize("bad", [None, {}, [], [[0, 0, 0]] * 79, [[True, 0, 0]] * 80, [[256, 0, 0]] * 80, [[1.5, 0, 0]] * 80, [[0, 0]] * 80])
def test_invalid_frames(bad):
    with pytest.raises(ValueError):
        decode_frame({"command": "ledcolors-ledstream-update", "data": {"leds": bad}})


def test_two_strips_can_sample_the_same_edge_with_different_counts_and_directions():
    edges = {edge: [(i * 10, 0, 0) for i in range(20)] for edge in granular.EDGES}
    monitor = map_strip([27, 57, 27], {0: ("left", True), 1: ("top", False), 2: ("right", False)}, edges)
    desk = map_strip([100, 80, 120], {0: ("left", False), 1: ("bottom", False), 2: ("right", True)}, edges)
    assert zone_counts([27, 57, 27], segments=20) == [5, 10, 5]
    assert len(monitor) == len(desk) == 20
    assert monitor[:5] == list(reversed(resample(edges["left"], 5)))
    assert desk[:7] == resample(edges["left"], 7)
    assert len(set(monitor)) > 4
    assert map_strip([1, 1], {0: ("top", False)}, edges)[10:] == [(0, 0, 0)] * 10


def test_area_resampling_uses_both_endpoints():
    assert resample([(0, 0, 0), (100, 200, 250)], 1) == [(50, 100, 125)]
    assert resample([(i, 0, 0) for i in range(20)], 20) == [(i, 0, 0) for i in range(20)]


def test_variable_palette_matches_node_format_and_is_bounded():
    data = bytes.fromhex(color_payload([(255, 0, 0)] * 20, [1] * 20))
    assert data[:2] == b"\x00\x14"
    assert data[122:124] == b"\x00\x14"
    assert data[124:] == bytes(range(1, 21))
    assert len(data) == 144
    for colors, counts in [([(0, 0, 0)] * 21, [1] * 21), ([(0, 0, 0)], [21]), ([(256, 0, 0)], [1]), ([(0, 0, 0)], [])]:
        with pytest.raises(ValueError):
            color_payload(colors, counts)


class FakeSender:
    def __init__(self, frames):
        self._devices = {}
        self.frames = frames

    def add(self, did, key, host):
        self._devices[did] = {"session": b"test"}

    def close(self):
        self._devices.clear()

    def send_colors(self, did, colors, counts):
        self.frames.append((did, list(colors), list(counts)))


@pytest.fixture
def hub(monkeypatch):
    frames, power = [], []
    monkeypatch.setattr(zone_lights, "ZoneSender", lambda: FakeSender(frames))

    async def painter(sender, ids, color_fn, interval):
        while True:
            for did in ids:
                colors, counts = color_fn(did)
                if colors:
                    sender.send_colors(did, colors, counts)
            await asyncio.sleep(0.01)

    monkeypatch.setattr(zone_lights, "run_zone_loop", painter)
    devices = {
        "monitor": {"host": "127.0.0.2", "local_key": "unused", "screen_sync": {"zones": [{"cm": n} for n in [27, 57, 27]]}},
        "desk": {"host": "127.0.0.3", "local_key": "unused", "screen_sync": {"zones": [{"cm": 300}]}},
    }
    entry = SimpleNamespace(entry_id="test", data={"devices": devices})
    entry.async_create_background_task = lambda hass, coro, name: asyncio.create_task(coro, name=name)

    async def executor(func, *args):
        return func(*args)

    hass = SimpleNamespace(data={"localtuya": {"zone_hubs": {}}}, async_add_executor_job=executor)
    result = ZonePaintHub(hass, entry)
    hass.data["localtuya"]["zone_hubs"]["test"] = result

    async def on(did):
        result._powered.add(did)
        power.append((did, True))

    async def off(did):
        result._powered.discard(did)
        power.append((did, False))

    result._power_one = AsyncMock(side_effect=on)
    result._power_off = AsyncMock(side_effect=off)
    result.recorded_frames, result.recorded_power = frames, power
    return result


@pytest.mark.asyncio
async def test_stream_preserves_manual_states_and_restores_exact_wire_colors(hub):
    hub.set_zone("monitor", 1, is_on=True, rgb=(11, 22, 33))
    manual = copy.deepcopy(hub._state)
    await hub.begin_stream("owner", {"monitor": [(255, 0, 0)] * 20, "desk": [(0, 0, 255)] * 20})
    assert set(hub.painting_device_ids()) == {"monitor", "desk"}
    assert hub._colors_for("monitor") == ([(255, 0, 0)] * 20, [1] * 20)
    assert hub._state["monitor"] == manual["monitor"]
    hub.update_stream("owner", {"desk": [(0, 255, 0)] * 20})
    with pytest.raises(HomeAssistantError):
        hub.update_stream("stranger", {"desk": [(0, 0, 0)] * 20})
    await hub.end_stream("stranger")
    assert "desk" in hub._stream_owner
    await hub.end_stream("owner")
    assert hub._colors_for("monitor") == ([(0, 0, 0), (11, 22, 33), (0, 0, 0)], [5, 10, 5])
    assert hub._state["monitor"] == manual["monitor"]
    assert ("desk", False) in hub.recorded_power
    await hub.stop()


@pytest.mark.asyncio
async def test_manual_command_rejected_before_entity_state_mutates(hub):
    light = BattletronZoneLight(hub.entry, "monitor", hub._device("monitor"), 0)
    light.hass = hub.hass
    light.async_write_ha_state = Mock()
    await hub.begin_stream("owner", {"monitor": [(0, 0, 0)] * 20})
    before = (light._rgb, light._brightness, light._is_on)
    with pytest.raises(HomeAssistantError):
        await light.async_turn_on(rgb_color=(255, 0, 0), brightness=123)
    with pytest.raises(HomeAssistantError):
        await light.async_turn_off()
    assert before == (light._rgb, light._brightness, light._is_on)
    with pytest.raises(HomeAssistantError):
        await hub.begin_stream("other", {"monitor": [(1, 2, 3)] * 20})
    await hub.end_stream("owner")
    await light.async_turn_on(rgb_color=(10, 20, 30), brightness=127)
    assert light._is_on and light._rgb == (10, 20, 30) and light._brightness == 127
    await hub.stop()


@pytest.mark.asyncio
async def test_layout_changes_and_tcp_fallback_are_blocked_during_stream(hub):
    from custom_components.localtuya.screen_sync import ZoneLengthNumber, AddZoneButton, RemoveZoneButton
    before = copy.deepcopy(hub.entry.data)
    await hub.begin_stream("owner", {"desk": [(0, 0, 0)] * 20})
    assert zone_lights.zones_painting(hub.hass, hub.entry, "desk")
    controls = [ZoneLengthNumber(hub.entry, "desk", hub._device("desk"), 0),
                AddZoneButton(hub.entry, "desk", hub._device("desk")),
                RemoveZoneButton(hub.entry, "desk", hub._device("desk"), 0)]
    for control in controls:
        control.hass = hub.hass
        with pytest.raises(HomeAssistantError):
            if isinstance(control, ZoneLengthNumber):
                await control.async_set_native_value(123)
            else:
                await control.async_press()
    assert hub.entry.data == before
    await hub.end_stream("owner")
    await hub.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_twice", [False, True])
async def test_painter_cancellation_joins_worker_before_closing_socket(monkeypatch, cancel_twice):
    from custom_components.localtuya import battletron_zones
    started, stopping, finished = threading.Event(), threading.Event(), threading.Event()

    def worker(sender, ids, colors, interval, stop):
        started.set()
        stop.wait(2)
        stopping.set()
        # Simulate the outstanding socket operation while HA cancels again.
        threading.Event().wait(0.1)
        finished.set()

    monkeypatch.setattr(battletron_zones, "_zone_thread", worker)
    sender = SimpleNamespace()
    closed_after_finish = []
    sender.close = lambda: closed_after_finish.append(finished.is_set())
    task = asyncio.create_task(battletron_zones.run_zone_loop(sender, [], lambda _: None))
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.to_thread(stopping.wait, 2)
    if cancel_twice:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed_after_finish == [True]


@pytest.mark.asyncio
async def test_failed_power_claim_rolls_back_ownership(hub):
    hub._power_one = AsyncMock()
    with pytest.raises(HomeAssistantError):
        await hub.begin_stream("owner", {"desk": [(1, 2, 3)] * 20})
    assert not hub._stream_owner and not hub._stream_frames
    await hub.stop()


@pytest.mark.asyncio
async def test_real_websocket_timeout_restores_saved_lights(hub, aiohttp_server, monkeypatch):
    monkeypatch.setattr("custom_components.localtuya.screen_stream.FRAME_TIMEOUT", 0.15)

    async def source(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        login = await ws.receive_json()
        assert login["token"] == "test-token-long-enough"
        await ws.send_json({"success": True})
        await ws.receive_json()
        await ws.send_json(wire_frame())
        async for _ in ws:
            pass
        return ws

    app = web.Application()
    app.router.add_get("/", source)
    server = await aiohttp_server(app)
    hub.set_zone("monitor", 1, is_on=True, rgb=(17, 33, 51))
    async with aiohttp.ClientSession() as session:
        controller = StreamController(hub, session)
        await controller.start("owner", str(server.make_url("/")).replace("http:", "ws:"), "test-token-long-enough", {"monitor": {1: ("top", False)}})
        assert hub._stream_owner == {"monitor": "owner"}
        await asyncio.sleep(0.3)
        assert not hub._stream_owner
        assert hub.zone_state("monitor", 1) == {"on": True, "rgb": (17, 33, 51)}
        await controller.close()
    await hub.stop()


@pytest.mark.asyncio
async def test_bad_source_auth_cannot_take_ownership(hub, aiohttp_server):
    async def source(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.receive_json()
        await ws.send_json({"success": False})
        return ws

    app = web.Application()
    app.router.add_get("/", source)
    server = await aiohttp_server(app)
    async with aiohttp.ClientSession() as session:
        controller = StreamController(hub, session)
        with pytest.raises(ValueError):
            await controller.start("owner", str(server.make_url("/")).replace("http:", "ws:"), "incorrect-token", {"desk": {0: ("bottom", False)}})
        assert not hub._stream_owner
        assert not hub.recorded_power
    await hub.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["malformed", "nonframe", "cancel_start"])
async def test_bad_or_missing_frames_release_without_touching_saved_lights(hub, aiohttp_server, monkeypatch, mode):
    monkeypatch.setattr("custom_components.localtuya.screen_stream.FRAME_TIMEOUT", 0.1)
    subscribed = asyncio.Event()

    async def source(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.receive_json()
        await ws.send_json({"success": True})
        await ws.receive_json()
        subscribed.set()
        if mode == "malformed":
            await ws.send_json(wire_frame())
            await asyncio.sleep(0.02)
            await ws.send_json(wire_frame([[0, 0, 0]] * 79))
        elif mode == "nonframe":
            for _ in range(10):
                if ws.closed:
                    break
                try:
                    await ws.send_json({"command": "irrelevant"})
                except aiohttp.ClientConnectionError:
                    break
                await asyncio.sleep(0.02)
        async for _ in ws:
            pass
        return ws

    app = web.Application()
    app.router.add_get("/", source)
    server = await aiohttp_server(app)
    hub.set_zone("monitor", 1, is_on=True, rgb=(30, 40, 50))
    async with aiohttp.ClientSession() as session:
        controller = StreamController(hub, session)
        task = asyncio.create_task(controller.start("owner", str(server.make_url("/")).replace("http:", "ws:"), "test-token-long-enough", {"monitor": {1: ("top", False)}}))
        await subscribed.wait()
        if mode == "cancel_start":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif mode == "nonframe":
            with pytest.raises(ValueError):
                await task
        else:
            try:
                await task
            except ValueError:
                pass  # An immediately queued malformed frame may fail startup itself.
            await asyncio.sleep(0.4)
        assert not hub._stream_owner
        assert hub.zone_state("monitor", 1) == {"on": True, "rgb": (30, 40, 50)}
        await controller.close()
    await hub.stop()


@pytest_asyncio.fixture
async def ha_http(hub, tmp_path, monkeypatch):
    """Real HA auth middleware and HTTP server, bound only to localhost."""
    hass = HomeAssistant(str(tmp_path))
    from homeassistant.helpers import device_registry, entity_registry
    device_registry.async_setup(hass)
    await device_registry.async_load(hass)
    await entity_registry.async_load(hass)
    hass.auth = await auth_manager_from_config(hass, [], [])
    admin = await hass.auth.async_create_user("Offline test administrator", group_ids=[GROUP_ID_ADMIN])
    reader = await hass.auth.async_create_user("Offline read-only test", group_ids=[GROUP_ID_READ_ONLY])
    tokens = []
    for user in (admin, reader):
        refresh = await hass.auth.async_create_refresh_token(user, client_id="http://localhost/")
        tokens.append(hass.auth.async_create_access_token(refresh))
    server = HomeAssistantHTTP(hass, ssl_certificate=None, ssl_peer_certificate=None, ssl_key=None,
                               server_host=["127.0.0.1"], server_port=0, trusted_proxies=[], ssl_profile="modern")
    hass.http = server
    hass.data["localtuya"] = {"zone_hubs": {"test": hub}}
    hub.hass = hass
    for did, count in [("monitor", 3), ("desk", 1)]:
        for index in range(count):
            hub.lights[(did, index)] = SimpleNamespace(entity_id=f"light.{did}_{index}", name=f"Zone {index}", async_write_ha_state=Mock())
    await server.async_initialize(cors_origins=[], use_x_forwarded_for=False, login_threshold=-1,
                                  is_ban_enabled=False, use_x_frame_options=True)
    source_session = aiohttp.ClientSession()
    monkeypatch.setattr("custom_components.localtuya.screen_stream.async_get_clientsession", lambda _: source_session)
    from homeassistant.components.api import APIStatusView
    server.register_view(APIStatusView())
    server.register_view(ScreenStreamView(hass))
    await server.async_bind()
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/api/localtuya/screen_sync", tokens
    finally:
        await hub.stop()
        await server.stop()
        await source_session.close()
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_real_ha_auth_validation_and_requester_cannot_choose_host(ha_http, hub):
    url, (admin, reader) = ha_http
    async with aiohttp.ClientSession() as session:
        assert (await session.get(url)).status == 401
        assert (await session.get(url, headers={"Authorization": f"Bearer {reader}"})).status == 403
        response = await session.get(url, headers={"Authorization": f"Bearer {admin}"})
        body = await response.json()
        assert response.status == 200 and body["protocol_version"] == 1
        assert [zone["sections"] for zone in body["zones"]] == [5, 10, 5, 20]
        headers = {"Authorization": f"Bearer {admin}"}
        good = {"action": "start", "owner": "owner", "port": 12345, "token": "test-token-long-enough",
                "mappings": [{"entity_id": "light.desk_0", "edge": "bottom"}]}
        bad_requests = [[], {}, {**good, "port": 80}, {**good, "mappings": []},
                        {**good, "mappings": good["mappings"] * 2},
                        {**good, "mappings": [{"entity_id": "light.missing", "edge": "left"}]},
                        {**good, "mappings": [{"entity_id": "light.desk_0", "edge": "left", "reverse": "yes"}]}]
        for request in bad_requests:
            assert (await session.post(url, json=request, headers=headers)).status == 400
        assert not hub.recorded_power
        fake = SimpleNamespace(owner="owner", start=AsyncMock(), close=AsyncMock())
        hub.stream = fake
        assert (await session.post(url, json={**good, "host": "192.0.2.50"}, headers=headers)).status == 200
        assert fake.start.call_args.args[1] == "ws://127.0.0.1:12345"
        await session.post(url, json={"action": "stop", "owner": "stranger"}, headers=headers)
        fake.close.assert_not_called()
        await session.post(url, json={"action": "stop", "owner": "owner"}, headers=headers)
        fake.close.assert_awaited_once()
        hub.stream = None


async def launch_swift_fixture():
    binary = Path(os.environ.get("SWIFT_STREAM_FIXTURE", "../screen-edges/.build/debug/screen-edges-stream-fixture")).resolve()
    assert binary.is_file(), "Build the Swift StreamFixture target before running cross-language tests"
    process = await asyncio.create_subprocess_exec(str(binary), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    line = await asyncio.wait_for(process.stdout.readline(), timeout=5)
    assert line.startswith(b"PORT="), line
    return process, int(line.decode().strip().split("=")[1])


@pytest.mark.asyncio
async def test_swift_producer_to_real_ha_http_mapping_release_and_disconnect(ha_http, hub):
    url, (admin, _) = ha_http
    process, port = await launch_swift_fixture()
    hub.set_zone("monitor", 1, is_on=True, rgb=(9, 18, 27))
    manual = copy.deepcopy(hub._state)
    mappings = [{"entity_id": "light.monitor_0", "edge": "left", "reverse": True},
                {"entity_id": "light.monitor_1", "edge": "top"},
                {"entity_id": "light.monitor_2", "edge": "right"},
                {"entity_id": "light.desk_0", "edge": "bottom"}]
    request = {"action": "start", "owner": "swift-owner", "port": port,
               "token": "local-integration-test-token", "mappings": mappings}
    try:
        async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {admin}"}) as session:
            response = await session.post(url, json=request)
            assert response.status == 200, await response.text()
            expected = {edge: [(i, 255 - i, i * 3 % 256) for i in range(offset, offset + 20)]
                        for edge, offset in zip(granular.EDGES, range(0, 80, 20), strict=True)}
            assert hub._stream_frames["desk"] == expected["bottom"]
            assert hub._stream_frames["monitor"][:5] == resample(expected["left"], 5, reverse=True)
            assert hub._stream_frames["monitor"][5:15] == resample(expected["top"], 10)
            assert hub._state["monitor"] == manual["monitor"]
            assert (await session.post(url, json={**request, "owner": "competitor"})).status == 400
            assert (await session.post(url, json={"action": "stop", "owner": "competitor"})).status == 200
            assert hub._stream_owner["desk"] == "swift-owner"
            # Repeated idempotent start requests must not restart the UDP painter.
            sender = hub.sender
            for _ in range(10):
                assert (await session.post(url, json=request)).status == 200
                assert hub.sender is sender
            await asyncio.sleep(float(os.environ.get("SCREEN_SYNC_SOAK_SECONDS", "0.3")))
            assert hub._stream_owner["desk"] == "swift-owner"
            # Pause restores the exact saved manual wire colors and off zones.
            assert (await session.post(url, json={"action": "stop", "owner": "swift-owner"})).status == 200
            assert not hub._stream_owner and hub._state["monitor"] == manual["monitor"]
            # Genuine repeated pause/resume, as distinct from idempotent starts.
            for _ in range(10):
                assert (await session.post(url, json=request)).status == 200
                assert (await session.post(url, json={"action": "stop", "owner": "swift-owner"})).status == 200
                assert not hub._stream_owner
                assert hub._state["monitor"] == manual["monitor"]
            # Resume, then an abrupt source death must also release ownership.
            assert (await session.post(url, json=request)).status == 200
            process.terminate()
            await process.wait()
            for _ in range(100):
                if not hub._stream_owner:
                    break
                await asyncio.sleep(0.05)
            assert not hub._stream_owner and hub._state["monitor"] == manual["monitor"]
    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()


@pytest.mark.asyncio
async def test_unload_does_not_restart_manual_output(hub):
    hub.set_zone("desk", 0, is_on=True, rgb=(10, 20, 30))
    await hub.begin_stream("owner", {"desk": [(1, 2, 3)] * 20})
    hub._stopping = True
    hub.apply = AsyncMock()
    await hub.end_stream("owner")
    assert not hub._stream_owner
    hub.apply.assert_not_awaited()
    await hub.stop()


def test_packets_match_working_node_encoder_golden():
    fixture = json.loads((Path(__file__).parent / "fixtures/node_palette.json").read_text())
    for case in fixture["cases"]:
        assert color_payload(case["colors"], case["counts"]) == case["hex"], case["name"]


@pytest.mark.asyncio
async def test_swift_ha_control_client_acquires_and_releases_real_endpoint(ha_http, hub):
    url, (admin, _) = ha_http
    binary = Path(os.environ.get("SWIFT_STREAM_FIXTURE", "../screen-edges/.build/debug/screen-edges-stream-fixture")).resolve()
    config = {"ha_url": url.split("/api/")[0], "ha_token": admin,
              "stream_token": "local-control-test-token", "owner_id": "control-owner",
              "mappings": [{"entity_id": "light.desk_0", "edge": "bottom", "reverse": True}]}
    hub.set_zone("desk", 0, is_on=True, rgb=(11, 22, 33))
    process = await asyncio.create_subprocess_exec(str(binary), "--control", stdin=asyncio.subprocess.PIPE,
                                                  stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        output, errors = await asyncio.wait_for(process.communicate(json.dumps(config).encode()), timeout=10)
        assert process.returncode == 0, errors.decode()
        assert b"STARTED\nRELEASED\n" in output
        assert any(did == "desk" and len(colors) == 20 and colors[0] == (79, 176, 237)
                   for did, colors, _ in hub.recorded_frames)
        assert not hub._stream_owner
        assert hub.zone_state("desk", 0) == {"on": True, "rgb": (11, 22, 33)}
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
