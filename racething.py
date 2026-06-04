#!/usr/bin/env python3
"""
Simple TUI for the HH race classification.

Unlike ``simple_track_order.py`` (which computes relative on-track positions),
this shows HH's *official race position* straight from the feed — the order you
see on the HH Scoreboard — plus the race time remaining and a column marking
which cars have been lapped.

  * Race position comes from the ``position`` channel
    (``position_type == "Overall"``).
  * Lapped status comes from the ``gap`` channel
    (``gap_type == "Overall"`` → ``laps_back``): 0 == lead lap, >0 == lapped.

Consumes the HHTiming TrackBridge loopback TCP feed (127.0.0.1:11003), which
emits newline-delimited JSON envelopes ``{type, data, ts}``. Rendering uses
``rich`` (Live + Table); everything else is standard library.
"""

import socket
import json
import time
import threading

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

# Configuration
FEED_HOST = "127.0.0.1"
FEED_PORT = 11003
UPDATE_INTERVAL = 0.1   # Seconds between display refreshes (max 10 FPS)
RECONNECT_DELAY = 2     # Seconds to wait before reconnecting
FRESH_S = 20            # A car's data is "fresh" if seen within this many seconds

# HH uses a large placeholder (~1e8+) for an unknown race countdown; hide it.
TIME_SENTINEL = 1e8


def _clean_int(v):
    """HH uses int-max / double-max as 'not set' sentinels — hide those."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        if v == 2147483647 or (isinstance(v, float) and (v != v or abs(v) >= 1e300)):
            return None
        return int(v)
    return None


def _clean_num(v):
    """Like _clean_int but keeps decimals (race countdown)."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        if f != f or abs(f) >= 1e300:  # NaN or double-max sentinel
            return None
        return f
    return None


class PositionState:
    """Thread-safe holder for the latest classification state.

    The feed reader runs on a background thread and mutates this state while
    the main thread reads it to render; every access is guarded by ``lock``.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.cars = {}            # car -> {'position': int|None, 'laps_back': int}
        self.car_ts = {}          # car -> last time any update arrived (freshness)
        self.session = {}         # session info: time_remaining_s, track_status, ...
        self.last_update = 0.0
        self.events_received = 0
        self.connected = False

    def _row(self, car):
        r = self.cars.get(car)
        if r is None:
            r = {"position": None, "laps_back": 0}
            self.cars[car] = r
        return r

    # ── mutation (feed thread); each takes the lock ──────────────────────
    def update_position(self, car_str, position, position_type):
        """Record official overall race position. Class positions arrive on the
        same channel and would corrupt the order, so ignore them."""
        if position_type not in (None, "Overall"):
            return
        car = str(car_str)
        pos = _clean_int(position)
        now = time.time()
        with self.lock:
            self._row(car)["position"] = pos
            self.car_ts[car] = now
            self.last_update = now

    def update_gap(self, car_str, laps_back, gap_type):
        """Record laps-down to the leader (lapped status) from the overall gap."""
        if gap_type != "Overall":
            return
        car = str(car_str)
        lb = _clean_int(laps_back)
        now = time.time()
        with self.lock:
            self._row(car)["laps_back"] = lb if lb is not None else 0
            self.car_ts[car] = now
            self.last_update = now

    def update_session(self, data):
        now = time.time()
        with self.lock:
            self.session = {
                "name": data.get("session_name"),
                "type": data.get("session_type"),
                "track_status": data.get("track_status"),
                "time_remaining_s": _clean_num(data.get("time_remaining_s")),
                "laps_remaining": _clean_int(data.get("laps_remaining")),
            }
            self.last_update = now

    def note_event(self):
        with self.lock:
            self.events_received += 1

    def set_connected(self, value):
        with self.lock:
            self.connected = value

    # ── snapshot (main/render thread) ────────────────────────────────────
    def snapshot(self):
        """Build header info + classification rows under the lock, returning a
        plain structure the renderer uses without holding the lock."""
        now = time.time()
        with self.lock:
            ranked, unpositioned = [], []
            for car, c in self.cars.items():
                p = c.get("position")
                fresh = (now - self.car_ts.get(car, 0)) <= FRESH_S
                entry = {"car": car, "laps_back": c.get("laps_back", 0), "stale": not fresh}
                if isinstance(p, (int, float)):
                    ranked.append((p, entry))
                else:
                    unpositioned.append((self._car_sort_key(car), entry))

            ranked.sort(key=lambda x: x[0])
            unpositioned.sort(key=lambda x: x[0])

            rows = [e for _, e in ranked] + [e for _, e in unpositioned]
            for i, e in enumerate(rows, start=1):
                e["position"] = i

            return {
                "connected": self.connected,
                "events_received": self.events_received,
                "leader": rows[0]["car"] if rows else None,
                "session": dict(self.session),
                "last_update": self.last_update,
                "cars_tracked": len(rows),
                "order": rows,
            }

    @staticmethod
    def _car_sort_key(car):
        """Sort un-positioned cars numerically when possible, else by string."""
        try:
            return (0, int(car))
        except (TypeError, ValueError):
            return (1, str(car))


def _format_time_left(time_left):
    """Format the race countdown, hiding sentinels."""
    if isinstance(time_left, (int, float)) and 0 <= time_left < TIME_SENTINEL:
        total = int(time_left)
        return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"
    return "--:--:--"


def _flag(track_status):
    """Track status as a coloured chip (green/yellow/red/chequered)."""
    if not track_status:
        return Text("")
    s = str(track_status).lower()
    if "red" in s:
        style = "bold white on red"
    elif any(k in s for k in ("yellow", "fcy", "safety", "sc", "caution", "slow")):
        style = "bold black on yellow"
    elif any(k in s for k in ("cheq", "finish", "end")):
        style = "bold black on white"
    elif "green" in s or "clear" in s or "track" in s:
        style = "bold black on green"
    else:
        style = "bold"
    return Text(f" {track_status} ", style=style)


def _lapped_cell(row):
    """Lapped indicator: green dash on the lead lap, magenta '+NL' if lapped."""
    lb = row.get("laps_back", 0)
    if not lb:
        return Text("-", style="green", justify="center")
    style = "magenta" + (" dim" if row["stale"] else "")
    return Text(f"+{lb}L", style=style, justify="center")


def render(state):
    """Build the renderable (header + table + footer) for the current state."""
    snap = state.snapshot()
    sess = snap["session"]

    conn = Text("CONNECTED", style="bold green") if snap["connected"] \
        else Text("DISCONNECTED", style="bold red")
    header = Text.assemble(
        ("Feed ", "bold"), conn,
        ("    Leader ", "bold"), (f"#{snap['leader']}" if snap["leader"] else "-"),
        ("    Events ", "bold"), str(snap["events_received"]), "    ",
        _flag(sess.get("track_status")),
    )
    clock = Text.assemble(
        ("RACE TIME LEFT  ", "bold"),
        (_format_time_left(sess.get("time_remaining_s")), "bold cyan"),
    )

    table = Table(title="[bold]RACE POSITIONS (HH)[/bold]",
                  header_style="bold", expand=False, title_justify="left")
    table.add_column("POS", justify="right", width=4)
    table.add_column("CAR", justify="left", width=6)
    table.add_column("LAPPED", justify="center", width=8)

    for row in snap["order"]:
        car_style = "dim" if row["stale"] else None
        table.add_row(str(row["position"]),
                      Text(f"#{row['car']}", style=car_style),
                      _lapped_cell(row))

    age = time.time() - snap["last_update"] if snap["last_update"] else 0.0
    footer = Text(f"updated {age:.1f}s ago    cars {snap['cars_tracked']}    "
                  f"(grey = stale; +NL = laps down)    Ctrl-C to quit", style="dim")
    return Group(clock, header, table, footer)


def feed_reader(state):
    """Continuously read from the HH feed and update state. Runs on a daemon
    thread; status is reflected in the display, not printed (a stray print
    would corrupt the alternate-screen TUI)."""
    while True:
        sock = None
        try:
            sock = socket.create_connection((FEED_HOST, FEED_PORT), timeout=5)
            sock.settimeout(1.0)
            state.set_connected(True)

            buffer = b""
            while True:
                try:
                    data = sock.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                buffer += data

                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue

                    etype = event.get("type")
                    payload = event.get("data", {})
                    if not isinstance(payload, dict):
                        continue
                    state.note_event()

                    if etype == "position":
                        car = payload.get("car")
                        if car is not None:
                            state.update_position(car, payload.get("position"),
                                                  payload.get("position_type"))
                    elif etype == "gap":
                        car = payload.get("car")
                        if car is not None:
                            state.update_gap(car, payload.get("laps_back"),
                                             payload.get("gap_type"))
                    elif etype == "session":
                        state.update_session(payload)

        except (ConnectionRefusedError, socket.timeout, OSError):
            pass
        finally:
            state.set_connected(False)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        time.sleep(RECONNECT_DELAY)


def main():
    """Main application loop."""
    state = PositionState()

    feed_thread = threading.Thread(target=feed_reader, args=(state,), daemon=True)
    feed_thread.start()

    console = Console()
    try:
        # screen=True uses the alternate screen buffer (clean enter/exit,
        # cursor handled for us); Live diffs frames so there's no flicker.
        with Live(render(state), console=console, screen=True,
                  refresh_per_second=int(1 / UPDATE_INTERVAL),
                  auto_refresh=False) as live:
            while True:
                live.update(render(state), refresh=True)
                time.sleep(UPDATE_INTERVAL)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
