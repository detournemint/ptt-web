#!/usr/bin/env python3
"""ptt-web: browser mic + hold-to-talk PTT + radio audio + tuning for a Yaesu
FT-991A behind hamlib's rigctld.

Binds to 127.0.0.1 only; reach it via `ssh -L 8075:127.0.0.1:8075 <host>`.

Safety:
  * Host / Origin checks (blocks cross-site WebSocket and DNS-rebinding abuse
    from any web page open in the browser while the tunnel is up).
  * PTT failsafes: heartbeat timeout, client disconnect, max TX time,
    unkey on shutdown.
  * Frequency/mode/power changes are refused while transmitting.
  * Optional power cap (PTT_MAX_POWER, 0.0-1.0 of the radio's full scale).
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web, WSMsgType

RIGCTLD = (os.environ.get("PTT_RIGCTLD_HOST", "127.0.0.1"),
           int(os.environ.get("PTT_RIGCTLD_PORT", "4532")))
SINK = os.environ.get(
    "PTT_SINK", "alsa_output.usb-Burr-Brown_from_TI_USB_Audio_CODEC-00.analog-stereo")
RX_SOURCE = os.environ.get(
    "PTT_SOURCE", "alsa_input.usb-Burr-Brown_from_TI_USB_Audio_CODEC-00.analog-stereo")
PORT = int(os.environ.get("PTT_PORT", "8075"))
MAX_POWER = float(os.environ.get("PTT_MAX_POWER", "1.0"))
MAX_TX = 120.0      # seconds, hard time-out timer
HB_TIMEOUT = 1.0    # unkey if no message from the keyed client for this long
TAIL = 0.25         # let audio drain before unkeying
RX_CHUNK = 1920     # 20 ms of 48 kHz mono s16le
HERE = Path(__file__).parent

FREQ_MIN, FREQ_MAX = 30_000, 470_000_000          # FT-991A receive range
MODES = ["LSB", "USB", "CW", "CWR", "AM", "FM", "AMN", "FMN",
         "PKTUSB", "PKTLSB", "RTTY", "RTTYR"]
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

log = logging.getLogger("ptt")


class State:
    listeners = set()   # websockets receiving radio audio
    rx_task = None
    owner = None        # websocket currently keyed
    pacat = None        # playback subprocess
    tx_start = 0.0
    last_hb = 0.0
    busy = asyncio.Lock()


S = State()
RIG_LOCK = asyncio.Lock()   # one CAT command at a time (hamlib hates bursts)


# ---------------------------------------------------------------- rig access
async def _rig_once(cmd, nlines):
    r, w = await asyncio.wait_for(asyncio.open_connection(*RIGCTLD), 3)
    try:
        w.write((cmd + "\n").encode())
        await w.drain()
        out = []
        for _ in range(nlines):
            line = (await asyncio.wait_for(r.readline(), 3)).decode().strip()
            out.append(line)
            if line.startswith("RPRT"):     # error or ack: reply is complete
                break
        return out
    finally:
        w.close()


async def rig(cmd, nlines=1, retries=1):
    """Serialized rigctld command; retries once on hamlib timeout/protocol error."""
    async with RIG_LOCK:
        for attempt in range(retries + 1):
            out = await _rig_once(cmd, nlines)
            if out and out[0] in ("RPRT -5", "RPRT -8") and attempt < retries:
                await asyncio.sleep(0.15)
                continue
            return out


async def rig_val(cmd, retries=1):
    """Numeric reply of a `l LEVEL` query, or None on any error."""
    try:
        out = await rig(cmd, 1, retries)
        return None if out[0].startswith("RPRT") else float(out[0])
    except Exception:
        return None


# ------------------------------------------------------------------ PTT / TX
async def start_pacat():
    return await asyncio.create_subprocess_exec(
        "pacat", "--playback", f"--device={SINK}", "--format=s16le",
        "--rate=48000", "--channels=1", "--latency-msec=50",
        "--client-name=ptt-web",
        stdin=asyncio.subprocess.PIPE,
    )


async def kill_pacat():
    p, S.pacat = S.pacat, None
    if p and p.returncode is None:
        try:
            p.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(p.wait(), 1)
        except asyncio.TimeoutError:
            p.kill()


async def ptt_on(ws):
    async with S.busy:
        if S.owner is not None:
            return False, "another client is transmitting"
        S.pacat = await start_pacat()
        try:
            reply = await rig("T 1")
        except Exception as e:  # rigctld down etc.
            await kill_pacat()
            return False, f"rigctld error: {e!r}"
        if reply[0] != "RPRT 0":
            await kill_pacat()
            return False, f"rig refused PTT: {reply[0]}"
        S.owner = ws
        S.tx_start = S.last_hb = time.monotonic()
        log.warning("PTT ON")
        return True, ""


async def ptt_off(reason="release"):
    async with S.busy:
        if S.owner is None:
            return
        ws, S.owner = S.owner, None     # stop accepting audio immediately
        await asyncio.sleep(TAIL)
        ok = False
        for _ in range(5):
            try:
                if (await rig("T 0", 1, 0))[0] == "RPRT 0":
                    ok = True
                    break
            except Exception as e:
                log.error("unkey attempt failed: %r", e)
            await asyncio.sleep(0.2)
        log.warning("PTT OFF (%s)%s", reason, "" if ok else "  *** UNKEY FAILED ***")
        await kill_pacat()
        if not ws.closed:
            await ws.send_json({"t": "state", "tx": False, "reason": reason,
                                "unkey_ok": ok})


async def watchdog():
    while True:
        await asyncio.sleep(0.2)
        if S.owner is None:
            continue
        now = time.monotonic()
        if now - S.last_hb > HB_TIMEOUT:
            await ptt_off("heartbeat lost")
        elif now - S.tx_start > MAX_TX:
            await ptt_off("max TX time")


# ---------------------------------------------------------------- RX audio
async def rx_loop():
    """Stream the radio's receive audio to every listener while any exist."""
    proc = await asyncio.create_subprocess_exec(
        "parec", f"--device={RX_SOURCE}", "--format=s16le", "--rate=48000",
        "--channels=1", "--latency-msec=40", "--client-name=ptt-web-rx",
        stdout=asyncio.subprocess.PIPE)
    try:
        while True:
            data = await proc.stdout.readexactly(RX_CHUNK)
            for ws in list(S.listeners):
                try:
                    await asyncio.wait_for(ws.send_bytes(data), 0.5)
                except Exception:
                    pass        # slow/closed client: drop this frame
    except (asyncio.IncompleteReadError, asyncio.CancelledError):
        pass
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


def set_listener(ws, on):
    if on:
        S.listeners.add(ws)
        if S.rx_task is None or S.rx_task.done():
            S.rx_task = asyncio.create_task(rx_loop())
    else:
        S.listeners.discard(ws)
        if not S.listeners and S.rx_task:
            S.rx_task.cancel()
            S.rx_task = None


# ------------------------------------------------------------------ web side
@web.middleware
async def guard(request, handler):
    """Reject DNS-rebinding (bad Host) and cross-site (bad Origin) requests."""
    host = urlparse("//" + request.host).hostname
    if host not in LOCAL_HOSTS:
        raise web.HTTPForbidden(text="bad Host")
    origin = request.headers.get("Origin")
    if origin is not None and urlparse(origin).netloc != request.host:
        raise web.HTTPForbidden(text="bad Origin")
    return await handler(request)


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=None, max_msg_size=1 << 20)
    await ws.prepare(request)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                d = json.loads(msg.data)
                if S.owner is ws:
                    S.last_hb = time.monotonic()
                if d.get("t") == "listen":
                    set_listener(ws, bool(d.get("on")))
                elif d.get("t") == "ptt":
                    if d.get("on"):
                        ok, why = await ptt_on(ws)
                        await ws.send_json({"t": "state", "tx": ok, "reason": why})
                    else:
                        await ptt_off("release")
            elif msg.type == WSMsgType.BINARY:
                if S.owner is ws and S.pacat and S.pacat.stdin:
                    S.last_hb = time.monotonic()
                    try:
                        S.pacat.stdin.write(msg.data)
                    except Exception:
                        pass
    finally:
        set_listener(ws, False)
        if S.owner is ws:
            await ptt_off("client disconnected")
    return ws


async def status(request):
    tx = S.owner is not None
    out = {"tx": tx, "max_power": MAX_POWER, "modes": MODES}
    try:
        line = (await rig("f"))[0]
        if line.startswith("RPRT"):
            out["error"] = f"freq read failed ({line})"
            return web.json_response(out)
        out["freq"] = int(line)
        mode = await rig("m", 2)
        if not mode[0].startswith("RPRT"):
            out["mode"], out["passband"] = mode[0], int(mode[1])
        out["power"] = await rig_val("l RFPOWER", 0 if tx else 1)
        if tx:      # best-effort meters; skip if CAT is busy so unkey never queues behind us
            if not RIG_LOCK.locked():
                out["swr"] = await rig_val("l SWR", 0)
                out["alc"] = await rig_val("l ALC", 0)
                out["pwr_w"] = await rig_val("l RFPOWER_METER_WATTS", 0)
        else:
            out["strength"] = await rig_val("l STRENGTH")
    except Exception as e:
        out["error"] = repr(e)
    return web.json_response(out)


def _refuse_if_tx():
    if S.owner is not None:
        return web.json_response({"ok": False, "error": "transmitting"}, status=409)


async def _set(request, build):
    """Shared POST plumbing: parse JSON, refuse during TX, run `build`, report."""
    busy = _refuse_if_tx()
    if busy:
        return busy
    try:
        body = await request.json()
        cmd = build(body)
    except (ValueError, TypeError, KeyError) as e:
        return web.json_response({"ok": False, "error": str(e) or "bad request"}, status=400)
    try:
        reply = (await rig(cmd))[0]
    except Exception as e:
        return web.json_response({"ok": False, "error": repr(e)}, status=502)
    ok = reply == "RPRT 0"
    log.warning("SET %s -> %s", cmd, reply)
    return web.json_response({"ok": ok, "reply": reply}, status=200 if ok else 502)


async def set_freq(request):
    def build(b):
        hz = int(b["hz"])
        if not FREQ_MIN <= hz <= FREQ_MAX:
            raise ValueError(f"frequency out of range ({FREQ_MIN}-{FREQ_MAX} Hz)")
        return f"F {hz}"
    return await _set(request, build)


async def set_mode(request):
    def build(b):
        mode = str(b["mode"]).upper()
        if mode not in MODES:
            raise ValueError("unknown mode")
        return f"M {mode} 0"        # passband 0 = radio default
    return await _set(request, build)


async def set_power(request):
    def build(b):
        v = float(b["value"])
        if not 0.0 <= v <= MAX_POWER:
            raise ValueError(f"power must be 0.0-{MAX_POWER}")
        return f"L RFPOWER {v:.3f}"
    return await _set(request, build)


async def index(request):
    return web.FileResponse(HERE / "index.html")


async def on_startup(app):
    app["wd"] = asyncio.create_task(watchdog())


async def on_cleanup(app):
    app["wd"].cancel()
    await ptt_off("server shutdown")


def make_app():
    app = web.Application(middlewares=[guard])
    app.add_routes([web.get("/", index), web.get("/ws", ws_handler),
                    web.get("/status", status),
                    web.post("/rig/freq", set_freq),
                    web.post("/rig/mode", set_mode),
                    web.post("/rig/power", set_power)])
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    web.run_app(make_app(), host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    main()
