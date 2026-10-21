# Battletron zone lights (screen edges)

This fork is [hass-localtuya](https://github.com/xZetsubou/hass-localtuya) with Tuya protocol 3.5, plus **Battletron zone lights**: each configured zone is a normal Home Assistant RGB light. Colour and brightness are painted over **UDP 40001** (same framing as the Mac `strip.js` client). Solid colour TCP is held closed while zones are active so music/colour DP does not fight the UDP frames.

The Mac grabber is [screen-edges](https://github.com/rostyslav-khomyk/screen-edges). It pushes edge colours to those zone entities over the HA **REST** API (`light.turn_on`). Hyperion is not used.

```
Screen Edges  --HA REST-->  light.*_zone_*  -->  localtuya UDP :40001  -->  Battletron strip
```

## Configure zones on a strip

1. Add the strip to LocalTuya (local key + protocol `3.5`; see below).
2. In the device options, add zones with length (cm) and edge (`left` / `top` / `right` / `bottom`).
3. Each zone appears as a colour light (e.g. `light.…_zone_1_left`). The parent solid light is disabled while zones exist.

Example — 110 cm monitor strip, controller on the left:

| Zone | Edge | Length |
| --- | --- | --- |
| 1 | left | 27 cm |
| 2 | top | 57 cm |
| 3 | right | 27 cm |

A 300 cm desk/cove strip can be a single zone: edge `bottom`, length 300.

Turning a zone on/off or changing its colour paints that segment immediately. Turning the last zone off blacks out the strip and powers it off over TCP.

## How zone UDP stays lit

- On paint start, LocalTuya powers the strip (`DP20`), sets work mode to `colour`, clears the solid colour DP to black, then holds the colour TCP link closed.
- One shared UDP socket binds on the HA host (port `40001`). Frames go **unicast** to each strip’s LAN address (broadcast is avoided: duplicate negotiations can replace the strip’s session key).
- Immediate HA updates and the background painter share one handshake; negotiate is debounced (~1 s) and skipped once a session exists.
- When the painter restarts (new socket / second strip joins), `_powered` is cleared so TCP power is re-asserted. Otherwise UDP can negotiate while the LEDs stay dark.

## Get the local key

Do this once per device from a browser, logged into the Tuya IoT console. Keep the data center matching the Smart Life account (e.g. Central Europe).

1. Open [platform.tuya.com](https://platform.tuya.com) and sign in.
2. Open the cloud project (Smart Home industry). Confirm the data center.
3. Link the Smart Life app account if needed. Ensure **IoT Core** is in service.
4. Open **API Explorer** → **Query Device Details** (single-device, not bulk).
5. Call `GET /v2.0/cloud/thing/{device_id}` and copy `local_key` exactly (punctuation is significant).

In LocalTuya, add the device by hand:

- host: strip LAN address  
- device id / local key from that response  
- protocol: `3.5`  
- `no_cloud` / local-only is fine once every device has a key  

Do not remove the device from Smart Life until local control works — removing it can issue a new local key. After a factory reset, fetch the key again the same way.

## Screen Edges (Mac)

Install and map zone lights as described in the [screen-edges README](https://github.com/rostyslav-khomyk/screen-edges). Sync pushes REST `light.turn_on` only; LocalTuya owns UDP to the strip.
