# ptt-web

Browser-based remote operation for a Yaesu FT-991A behind hamlib's `rigctld`:
hold-to-talk PTT with your computer's mic, live receive audio, frequency /
mode / power control, S-meter and TX meters.

```
browser mic ──WS──> server.py ──pacat──> radio USB codec (TX audio)
browser <──WS── server.py <──parec── radio USB codec (RX audio)
browser buttons ──HTTP──> server.py ──TCP──> rigctld ──CAT──> radio
```

## Requirements (on the machine wired to the radio)

- Python 3.10+ with `aiohttp`
- PipeWire/PulseAudio (`pacat`, `parec`) with the radio's USB audio codec
- `rigctld` (hamlib) reachable on `127.0.0.1:4532`

## Run

```
python3 server.py
```

Binds to **127.0.0.1 only**. From the machine you sit at:

```
ssh -N -L 8075:127.0.0.1:8075 <radio-host>
# then open http://localhost:8075
```

Browsers only allow microphone access on `localhost` or HTTPS, which is why
the page must be opened as `localhost`.

Install as a user service: copy `ptt-web.service` to `~/.config/systemd/user/`
and `systemctl --user enable --now ptt-web`.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `PTT_PORT` | `8075` | listen port (localhost) |
| `PTT_SINK` | USB codec output | PulseAudio sink that feeds the radio's TX audio |
| `PTT_SOURCE` | USB codec input | PulseAudio source carrying the radio's RX audio |
| `PTT_RIGCTLD_HOST` / `PTT_RIGCTLD_PORT` | `127.0.0.1` / `4532` | rigctld address |
| `PTT_MAX_POWER` | `1.0` | cap on the power slider (0.0-1.0 of full scale; 1.0 = 100 W on an FT-991A) |

## Safety design

- **Host and Origin checks** on every request, including the WebSocket
  upgrade, so a web page open in your browser cannot reach the transmitter
  through `localhost` (cross-site WebSocket hijacking / DNS rebinding).
- **PTT failsafes:** unkeys on release, client disconnect, tab blur/hide,
  1 s of silence from the client (heartbeat), a 120 s TX limit, and server
  shutdown.
- Frequency, mode and power changes are **refused while transmitting**.
- CAT commands are serialized with one retry, because hamlib returns
  spurious timeout / protocol errors when commands arrive back to back.
- Only one client can transmit at a time.

## Radio-side notes

- The FT-991A ignores USB audio in plain SSB unless the modulation source is
  set to the rear/USB input in its menu (or you use a DATA mode). If you key
  up and nothing is transmitted, check this first.
- Power defaults to whatever the radio is set to. Lower it with the slider
  before testing, and use a dummy load for the first run.
- Stop other programs that key the radio or hold the CAT port (WSJT-X,
  CW keyers) while using this.

## Not tested against hardware

Frequency/mode/power *setting* and TX metering (SWR/ALC/power) are written
against hamlib's documented interface; the no-op frequency set and all read
paths were verified, but transmit audio, mode changes, power changes and the
TX meters have not been exercised on the air.
