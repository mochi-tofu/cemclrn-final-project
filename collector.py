"""
Tier 1 Wi-Fi collector.

Logs raw Wi-Fi measurements to a local SQLite file. Works offline; uploading
is handled separately (Phase 2), using the `uploaded` column.

Three loops run side by side:
  - Reading loop  (default every 5 s):  signal info + N TCP "pings"
  - Transfer loop (default every 60 s): timed download (the label, plus time to
                                        first byte), then a timed upload
  - Scan loop     (default every 60 s): list of nearby access points

Only the Python standard library is required on Windows and Linux.
On macOS, install `pyobjc-framework-CoreWLAN` for fast signal readings and for
the nearby access point scan.

Usage:
  python collector.py --location library-4f --device kirby-laptop
  python collector.py --location home --device kirby-laptop --reading-interval 2
"""

import argparse
import os
import platform
import re
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Settings (keep these the same on every laptop for the whole collection)
# ---------------------------------------------------------------------------
PING_HOST = "1.1.1.1"   # Cloudflare DNS, reachable on port 443
PING_PORT = 443
PING_TIMEOUT_S = 1.0
DOWNLOAD_URL = "https://speed.cloudflare.com/__down?bytes=1000000"  # 1 MB
UPLOAD_URL = "https://speed.cloudflare.com/__up"
UPLOAD_BYTES = 500_000                                              # 0.5 MB
TRANSFER_TIMEOUT_S = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id            TEXT PRIMARY KEY,
    ts            TEXT NOT NULL,      -- ISO 8601 with timezone; timestamp
    session_id    TEXT NOT NULL,      -- session each time u run
    device_id     TEXT NOT NULL,      -- laptop name u type
    os            TEXT NOT NULL,      -- OS system (windows, Mac, Linux reports signal diff)
    location      TEXT NOT NULL,      -- location name u typed
    ssid          TEXT,               -- network name (i.e. DLSU-Wifi)
    bssid         TEXT,               -- access point's hardware address (one network has many access points around campus)
    signal_pct    REAL,               -- Windows / Linux report percent; signal strength in percent; NULL in MAC
    rssi_dbm      REAL,               -- macOS reports dBm
    noise_dbm     REAL,               -- bg radio noise (only in MAC); lower or more nega better, shows how clearly signal stands out
    channel       TEXT,               -- 1-13 are 2.4 GHz; 36 and above are 5 GHz
    band          TEXT,               -- 2.4 GHz reaches farther but slower and more crowded; 5 GHz is faster but hsorter range
    radio_type    TEXT,               -- wifi standard/gen (802.11n = Wi-Fi 4, ac = Wi-Fi 5, ax = Wi-Fi 6)
    rx_rate_mbps  REAL,               -- receive link speed; max rate laptop and access point agreed on for receiving; not actual internet speed (windows, Linux)
    tx_rate_mbps  REAL,               -- similar but for sending (windows, mac)
    uploaded      INTEGER DEFAULT 0   -- if uploaded to central database (Supabase)
);
CREATE TABLE IF NOT EXISTS pings (
    id            TEXT PRIMARY KEY,
    reading_id    TEXT NOT NULL REFERENCES readings(id),
    ts            TEXT NOT NULL,      -- timsetamp for ping
    seq           INTEGER NOT NULL,   -- each reading has 5 pings, this says which ping it is
    latency_ms    REAL,               -- NULL = lost / timed out
    error         TEXT,               -- why ping failed
    uploaded      INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS downloads (
    id            TEXT PRIMARY KEY,
    ts            TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    device_id     TEXT NOT NULL,
    os            TEXT NOT NULL,
    location      TEXT NOT NULL,
    bytes         INTEGER,            -- how much bytes downloaded, should be 1000000, cuz set to donwload 1MB
    duration_s    REAL,               -- NULL = failed; download time in seconds
    error         TEXT,               -- why download failed, should be 500000
    ttfb_s        REAL,               -- time to first byte of the download
    upload_bytes  INTEGER,            -- bytes uploaded
    upload_s      REAL,               -- NULL = failed; upload time in seconds
    upload_error  TEXT,               -- why upload failed
    uploaded      INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scans (
    id                 TEXT PRIMARY KEY,
    ts                 TEXT NOT NULL,
    session_id         TEXT NOT NULL,
    device_id          TEXT NOT NULL,
    os                 TEXT NOT NULL,
    location           TEXT NOT NULL,
    connected_bssid    TEXT,          -- access point you were on during the scan
    connected_channel  TEXT,          -- channel during scan
    ap_count           INTEGER,       -- all access points seen from any network
    same_channel_count INTEGER,       -- other acces points on your channel (excluding yours)
    error              TEXT,
    uploaded           INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scan_aps (
    id            TEXT PRIMARY KEY,
    scan_id       TEXT NOT NULL REFERENCES scans(id),  --which scan this came from
    ssid          TEXT,               -- network name
    bssid         TEXT,               -- access point address
    signal_pct    REAL,               -- its signal (windows, linux)
    rssi_dbm      REAL,               -- its signal (mac)
    channel       TEXT,               -- its channel
    band          TEXT,               -- its band
    is_connected  INTEGER,            -- the access point ur connected to =1, if others =0
    uploaded      INTEGER DEFAULT 0
);
"""

# Columns added after the first version. Old .db files get them automatically.
NEW_COLUMNS = {
    "downloads": {"ttfb_s": "REAL", "upload_bytes": "INTEGER",
                  "upload_s": "REAL", "upload_error": "TEXT"},
}


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def to_float(value):
    if value is None:
        return None
    m = re.search(r"-?\d+(\.\d+)?", str(value))
    return float(m.group()) if m else None


def channel_number(value):
    """'44 (5GHz, 80MHz)' -> '44'. Used to compare channels across OSes."""
    m = re.search(r"\d+", str(value)) if value is not None else None
    return m.group() if m else None


def band_from_freq(freq_mhz):
    if not freq_mhz:
        return None
    return "2.4 GHz" if freq_mhz < 3000 else "5 GHz" if freq_mhz < 5925 else "6 GHz"


# ---------------------------------------------------------------------------
# Signal readers (the network you are connected to), one per OS
# ---------------------------------------------------------------------------
def parse_netsh(text):
    """Parse `netsh wlan show interfaces` (English Windows)."""
    fields = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        fields.setdefault(key, value)   # first interface only
    if fields.get("state", "").lower() not in ("connected", ""):
        return {"ssid": None}
    return {
        "ssid": fields.get("ssid"),
        "bssid": fields.get("bssid") or fields.get("ap bssid"),
        "signal_pct": to_float(fields.get("signal")),
        "channel": fields.get("channel"),
        "band": fields.get("band"),
        "radio_type": fields.get("radio type"),
        "rx_rate_mbps": to_float(fields.get("receive rate (mbps)")),
        "tx_rate_mbps": to_float(fields.get("transmit rate (mbps)")),
    }


def split_nmcli(line):
    """nmcli -t separates fields with ':' and escapes ':' inside values as '\\:'."""
    return [p.replace("\\:", ":") for p in re.split(r"(?<!\\):", line)]


def parse_nmcli(text):
    """Parse `nmcli -t -f ACTIVE,SSID,BSSID,SIGNAL,CHAN,FREQ,RATE dev wifi`."""
    for line in text.splitlines():
        parts = split_nmcli(line)
        if len(parts) >= 7 and parts[0] == "yes":
            return {
                "ssid": parts[1],
                "bssid": parts[2],
                "signal_pct": to_float(parts[3]),
                "channel": parts[4],
                "band": band_from_freq(to_float(parts[5])),
                "rx_rate_mbps": to_float(parts[6]),
            }
    return {"ssid": None}


def parse_system_profiler(text):
    """Parse `system_profiler SPAirPortDataType` (macOS fallback, slow)."""
    block = text.split("Current Network Information:", 1)
    if len(block) < 2:
        return {"ssid": None}
    lines = block[1].splitlines()
    ssid = lines[1].strip().rstrip(":") if len(lines) > 1 else None
    out = {"ssid": ssid}
    for line in lines[2:]:
        if line.strip().endswith(":") and not line.startswith(" " * 12):
            break  # reached "Other Local Wi-Fi Networks"
        key, _, value = line.strip().partition(":")
        key = key.strip().lower()
        if key == "signal / noise":
            nums = re.findall(r"-?\d+", value)
            if len(nums) >= 2:
                out["rssi_dbm"], out["noise_dbm"] = float(nums[0]), float(nums[1])
        elif key == "channel":
            out["channel"] = value.strip()
            m = re.search(r"(2|5|6)\s*GHz", value)
            out["band"] = f"{m.group(1)}{'.4' if m.group(1) == '2' else ''} GHz" if m else None
        elif key == "phy mode":
            out["radio_type"] = value.strip()
        elif key == "transmit rate":
            out["tx_rate_mbps"] = to_float(value)
    return out


MAC_BANDS = {1: "2.4 GHz", 2: "5 GHz", 3: "6 GHz"}


def read_signal_mac():
    try:
        import CoreWLAN  # pyobjc-framework-CoreWLAN
        iface = CoreWLAN.CWWiFiClient.sharedWiFiClient().interface()
        ch = iface.wlanChannel()
        return {
            "ssid": iface.ssid(),      # may be None without Location permission
            "bssid": iface.bssid(),    # may be None without Location permission
            "rssi_dbm": float(iface.rssiValue()),
            "noise_dbm": float(iface.noiseMeasurement()),
            "channel": str(ch.channelNumber()) if ch else None,
            "band": MAC_BANDS.get(ch.channelBand()) if ch else None,
            "tx_rate_mbps": float(iface.transmitRate()),
        }
    except ImportError:
        out = subprocess.run(["system_profiler", "SPAirPortDataType"],
                             capture_output=True, text=True, timeout=15).stdout
        return parse_system_profiler(out)


def read_signal():
    system = platform.system()
    try:
        if system == "Windows":
            out = subprocess.run(["netsh", "wlan", "show", "interfaces"],
                                 capture_output=True, text=True, timeout=5).stdout
            return parse_netsh(out)
        if system == "Darwin":
            return read_signal_mac()
        out = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID,BSSID,SIGNAL,CHAN,FREQ,RATE", "dev", "wifi"],
            capture_output=True, text=True, timeout=5).stdout
        return parse_nmcli(out)
    except Exception as e:  # never let a signal read crash the collector
        return {"ssid": None, "error": str(e)}


# ---------------------------------------------------------------------------
# Nearby access point scanners, one per OS. Each returns a list of dicts.
# ---------------------------------------------------------------------------
def parse_netsh_networks(text):
    """Parse `netsh wlan show networks mode=bssid` (English Windows).

    Output looks like:
        SSID 1 : DLSU-Wifi
            BSSID 1                 : 11:22:33:44:55:66
                 Signal             : 87%
                 Band               : 5 GHz
                 Channel            : 44
    """
    aps, ssid, current = [], None, None
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if re.fullmatch(r"ssid \d+", key):
            ssid = value or None          # hidden networks have an empty name
        elif re.fullmatch(r"bssid \d+", key):
            current = {"ssid": ssid, "bssid": value}
            aps.append(current)
        elif current is not None:
            if key == "signal":
                current["signal_pct"] = to_float(value)
            elif key == "channel":
                current["channel"] = value
            elif key == "band":
                current["band"] = value
    return aps


def parse_nmcli_list(text):
    """Parse `nmcli -t -f SSID,BSSID,SIGNAL,CHAN,FREQ dev wifi list`."""
    aps = []
    for line in text.splitlines():
        parts = split_nmcli(line)
        if len(parts) >= 5 and parts[1]:
            aps.append({"ssid": parts[0] or None, "bssid": parts[1],
                        "signal_pct": to_float(parts[2]), "channel": parts[3],
                        "band": band_from_freq(to_float(parts[4]))})
    return aps


def scan_aps():
    """Returns (list of access points, error or None).

    Windows and Linux return the list from the OS's most recent background scan
    (refreshed every so often by the OS), so this is cheap and does not
    interrupt your connection.
    """
    system = platform.system()
    try:
        if system == "Windows":
            out = subprocess.run(["netsh", "wlan", "show", "networks", "mode=bssid"],
                                 capture_output=True, text=True, timeout=10).stdout
            return parse_netsh_networks(out), None
        if system == "Darwin":
            import CoreWLAN  # needs pyobjc-framework-CoreWLAN
            iface = CoreWLAN.CWWiFiClient.sharedWiFiClient().interface()
            nets, err = iface.scanForNetworksWithName_error_(None, None)
            if err:
                return [], str(err)
            aps = []
            for n in nets or []:
                ch = n.wlanChannel()
                aps.append({"ssid": n.ssid(), "bssid": n.bssid(),
                            "rssi_dbm": float(n.rssiValue()),
                            "channel": str(ch.channelNumber()) if ch else None,
                            "band": MAC_BANDS.get(ch.channelBand()) if ch else None})
            return aps, None
        out = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,BSSID,SIGNAL,CHAN,FREQ", "dev", "wifi", "list",
             "--rescan", "no"],
            capture_output=True, text=True, timeout=10).stdout
        return parse_nmcli_list(out), None
    except ImportError:
        return [], "CoreWLAN not installed"
    except Exception as e:
        return [], type(e).__name__


def summarize_scan(aps, connected_bssid, connected_channel):
    """Mark which AP you are on, and count others on the same channel."""
    my_bssid = (connected_bssid or "").lower()
    my_ch = channel_number(connected_channel)
    same = 0
    for ap in aps:
        ap["is_connected"] = int(bool(my_bssid) and (ap.get("bssid") or "").lower() == my_bssid)
        if my_ch and not ap["is_connected"] and channel_number(ap.get("channel")) == my_ch:
            same += 1
    return len(aps), (same if my_ch else None)


# ---------------------------------------------------------------------------
# Network probes
# ---------------------------------------------------------------------------
def tcp_ping(host=PING_HOST, port=PING_PORT, timeout=PING_TIMEOUT_S):
    """Time a TCP handshake. Returns (latency_ms or None, error or None).

    Used instead of ICMP ping: no admin rights needed, same code on every OS,
    and no locale-dependent output to parse.
    """
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return (time.perf_counter() - start) * 1000, None
    except Exception as e:
        return None, type(e).__name__


def timed_download(url=DOWNLOAD_URL, timeout=TRANSFER_TIMEOUT_S):
    """Returns (bytes, duration_s, ttfb_s, error). Times are None if it failed."""
    start = time.perf_counter()
    ttfb = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "dlsu-wifi-collector"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            n = 0
            while chunk := resp.read(65536):
                if ttfb is None:
                    ttfb = time.perf_counter() - start   # first data arrived
                n += len(chunk)
        return n, time.perf_counter() - start, ttfb, None
    except Exception as e:
        return None, None, ttfb, type(e).__name__


def timed_upload(url=UPLOAD_URL, size=UPLOAD_BYTES, timeout=TRANSFER_TIMEOUT_S):
    """Send `size` random bytes and time it. Returns (bytes, duration_s, error)."""
    data = os.urandom(size)   # random so nothing along the way can compress it
    start = time.perf_counter()
    try:
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "User-Agent": "dlsu-wifi-collector",
            "Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return size, time.perf_counter() - start, None
    except Exception as e:
        return size, None, type(e).__name__


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------
class Collector:
    def __init__(self, db_path, location, device_id, reading_interval,
                 transfer_interval, scan_interval, pings_per_reading):
        self.db_path = db_path
        self.location = location
        self.device_id = device_id
        self.os = platform.system()
        self.session_id = str(uuid.uuid4())
        self.reading_interval = reading_interval
        self.transfer_interval = transfer_interval
        self.scan_interval = scan_interval
        self.pings_per_reading = pings_per_reading
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self._add_missing_columns()
        self.db.execute("PRAGMA journal_mode=WAL")  # safer if the laptop dies
        self.last_transfer = "n/a"
        self.last_scan = "n/a"

    def _add_missing_columns(self):
        for table, cols in NEW_COLUMNS.items():
            existing = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            for name, sqltype in cols.items():
                if name not in existing:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sqltype}")
        self.db.commit()

    def _common(self):
        return {"session_id": self.session_id, "device_id": self.device_id,
                "os": self.os, "location": self.location}

    def _insert(self, table, row):
        cols = ",".join(row)
        self.db.execute(f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' * len(row))})",
                        list(row.values()))

    def take_reading(self):
        ts = now_iso()
        sig = read_signal()
        reading_id = str(uuid.uuid4())
        pings = []
        for seq in range(self.pings_per_reading):
            latency, err = tcp_ping()
            pings.append((str(uuid.uuid4()), reading_id, now_iso(), seq, latency, err))
            time.sleep(0.2)

        row = {"id": reading_id, "ts": ts, **self._common(),
               **{k: sig.get(k) for k in ("ssid", "bssid", "signal_pct", "rssi_dbm",
                                          "noise_dbm", "channel", "band", "radio_type",
                                          "rx_rate_mbps", "tx_rate_mbps")}}
        with self.lock:
            self._insert("readings", row)
            self.db.executemany("INSERT INTO pings (id, reading_id, ts, seq, latency_ms, error) "
                                "VALUES (?,?,?,?,?,?)", pings)
            self.db.commit()

        ok = [p[4] for p in pings if p[4] is not None]
        sig_str = (f"{sig['signal_pct']:.0f}%" if sig.get("signal_pct") is not None
                   else f"{sig['rssi_dbm']:.0f} dBm" if sig.get("rssi_dbm") is not None
                   else "no wifi")
        lat_str = f"{sum(ok) / len(ok):.0f} ms" if ok else "all lost"
        loss = 100 * (len(pings) - len(ok)) / len(pings)
        print(f"[{ts[11:19]}] {self.location} | signal {sig_str} | latency {lat_str} | "
              f"loss {loss:.0f}% | {self.last_transfer} | {self.last_scan}")

    def take_transfer(self):
        ts = now_iso()
        n, dur, ttfb, err = timed_download()
        up_n, up_dur, up_err = timed_upload()   # after the download, so they don't compete
        down_str = f"down {dur:.2f}s (ttfb {ttfb:.2f}s)" if dur is not None else f"down FAILED ({err})"
        up_str = f"up {up_dur:.2f}s" if up_dur is not None else f"up FAILED ({up_err})"
        self.last_transfer = f"{down_str} {up_str}"
        with self.lock:
            self._insert("downloads", {
                "id": str(uuid.uuid4()), "ts": ts, **self._common(),
                "bytes": n, "duration_s": dur, "error": err, "ttfb_s": ttfb,
                "upload_bytes": up_n, "upload_s": up_dur, "upload_error": up_err})
            self.db.commit()

    def take_scan(self):
        ts = now_iso()
        sig = read_signal()
        aps, err = scan_aps()
        count, same = summarize_scan(aps, sig.get("bssid"), sig.get("channel"))
        scan_id = str(uuid.uuid4())
        if err:
            self.last_scan = f"APs FAILED ({err})"
        else:
            self.last_scan = f"APs {count}" + (f" (same ch {same})" if same is not None else "")
        with self.lock:
            self._insert("scans", {
                "id": scan_id, "ts": ts, **self._common(),
                "connected_bssid": sig.get("bssid"), "connected_channel": sig.get("channel"),
                "ap_count": count if not err else None, "same_channel_count": same,
                "error": err})
            for ap in aps:
                self._insert("scan_aps", {
                    "id": str(uuid.uuid4()), "scan_id": scan_id,
                    **{k: ap.get(k) for k in ("ssid", "bssid", "signal_pct", "rssi_dbm",
                                              "channel", "band", "is_connected")}})
            self.db.commit()

    def _loop(self, fn, interval):
        next_run = time.monotonic()
        while not self.stop.is_set():
            try:
                fn()
            except Exception as e:
                print(f"  ! {fn.__name__} error: {e}")
            next_run += interval
            self.stop.wait(max(0, next_run - time.monotonic()))

    def run(self):
        print(f"Collecting at '{self.location}' as '{self.device_id}' ({self.os}) "
              f"-> {self.db_path}\nSession {self.session_id}. Ctrl+C to stop.\n")
        jobs = [(self.take_reading, self.reading_interval),
                (self.take_transfer, self.transfer_interval),
                (self.take_scan, self.scan_interval)]
        threads = [threading.Thread(target=self._loop, args=job, daemon=True) for job in jobs]
        for t in threads:
            t.start()
        try:
            while any(t.is_alive() for t in threads):
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nStopping...")
            self.stop.set()
            for t in threads:
                t.join(timeout=2 * TRANSFER_TIMEOUT_S + 2)
        self.db.close()
        print("Saved.")


def main():
    p = argparse.ArgumentParser(description="Tier 1 Wi-Fi quality collector")
    p.add_argument("--location", required=True, help="e.g. library-4f, canteen, home")
    p.add_argument("--device", required=True, help="unique name per laptop, e.g. kirby-laptop")
    p.add_argument("--db", default="wifi_data.db")
    p.add_argument("--reading-interval", type=float, default=5.0, help="seconds")
    p.add_argument("--transfer-interval", type=float, default=60.0,
                   help="seconds between download+upload tests")
    p.add_argument("--scan-interval", type=float, default=60.0,
                   help="seconds between nearby access point scans")
    p.add_argument("--pings", type=int, default=5, help="TCP pings per reading")
    a = p.parse_args()
    Collector(a.db, a.location.strip().lower(), a.device, a.reading_interval,
              a.transfer_interval, a.scan_interval, a.pings).run()


if __name__ == "__main__":
    main()