# Screen edges and local keys

This fork is [hass-localtuya](https://github.com/xZetsubou/hass-localtuya), which already speaks Tuya protocol 3.5. The additions here attach those lights to a Hyperion screen-edge stream.

The Mac grabber is [screen-edges](https://github.com/rostyslav-khomyk/screen-edges).

## What stays the same

The light entity still does what the controller already does:

- on, off, and one solid color
- scenes
- music

Screen sync is a separate switch. Turn it off before using scenes or music. The zone packets and the music mode will otherwise fight over the strip.

## Add a screen server

1. Install and start `screen-edges` on the Mac. Copy the token from `~/Library/Application Support/ScreenEdges/config.json`.
2. In Home Assistant, open the LocalTuya options and choose **Screen edge colors**.
3. Name the server, set the Mac's address, port `8090`, and the token.
4. Pick the strip. Zone 1 is the controller end of the strip.

The 110 cm monitor strip, controller on the left:

| Zone | Edge | Length |
| --- | --- | --- |
| 1 | left | 27 cm |
| 2 | top | 57 cm |
| 3 | right | 27 cm |
| 4 |  | 0 |

A second strip can use the same server name. Example: a 300 cm cove that only echoes the bottom of the ultrawide is one zone, edge `bottom`, length 300. Sliders on the device change the lengths later. A length of 0 drops that zone. There are four color slots, so four zones is the maximum.

Saving this step sets `local_only` on that strip. Home Assistant will not ask the Tuya cloud for a new local key afterwards.

## Get the local key

Do this once per device, from a browser, while logged into the Tuya IoT console. The data center has to stay Central Europe, the same region as the Smart Life account.

1. Open [platform.tuya.com](https://platform.tuya.com) and sign in.
2. Open the cloud project. The industry is Smart Home. Confirm the data center is Central Europe.
3. Link the Smart Life app account to the project if it is not linked yet.
4. The project needs the IoT Core API. The reminder on the project overview is not the same thing. IoT Core has to be in service.
5. Open API Explorer. Choose **Query Device Details**. That is the single-device call, not the bulk call.
6. Request `GET /v2.0/cloud/thing/{device_id}`.
7. Copy `local_key` exactly. Characters such as `?`, `@`, `>`, `$`, `[`, and `{` are part of the key.

In LocalTuya, add the device by hand:

- host: the strip's LAN address
- device id: the `id` from that same response
- local key: the value you copied
- protocol: `3.5`

Leave the cloud account empty (`no_cloud`) if every device already has a key. After **Screen edge colors** saves, that strip stays on local traffic only.

Do not remove the device from Smart Life until local control is already working. Removing it can issue a new local key.
