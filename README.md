# Wi-Fi Quality Collector (Tier 1)

`collector.py` measures the Wi-Fi on your laptop and saves everything to a local
SQLite file (`.db`). It works offline. Nothing is sent anywhere except the small
test traffic it measures with.

## What you need

- **Python 3.8 or newer.** Check in Command Prompt with `python --version`.
  If it is missing, install it from python.org and tick **"Add python.exe to PATH"**.
  If `python` does not work but `py` does, use `py` in every command below.
- **Windows or Linux:** nothing else to install.
- **Mac:** run `pip install pyobjc-framework-CoreWLAN` once. Give Terminal
  Location permission if SSID/BSSID show as empty.
- **To look at the data:** DB Browser for SQLite (sqlitebrowser.org) or DBeaver.

## Run it

1. Put `collector.py` in a folder, e.g. `C:\wifi-quality`.
2. Open Command Prompt and go to that folder:
   ```
   cd C:\wifi-quality
   ```
3. Start it:
   ```
   python collector.py --location library-4f --device laptop-name --db wifi_data.db
   ```
4. Leave the window open. Every 5 seconds it prints one line, for example:
   ```
   [14:03:22] library-4f | signal 87% | latency 12 ms | loss 0% | down 0.85s (ttfb 0.10s) up 1.20s | APs 14 (same ch 3, overlap 5)
   ```
   The first lines show `n/a` until the first download and scan finish. That is normal.
5. Stop it with **Ctrl+C**. Wait for `Saved.`

## Options

| Option | Required | Default | Meaning |
| --- | --- | --- | --- |
| `--location` | yes | | Short name for the spot. Use the agreed names, lowercase, no spaces |
| `--device` | yes | | Your laptop's name. Unique per member, same every time |
| `--db` | no | `wifi_data.db` | File to save to. Running again adds to the same file |
| `--reading-interval` | no | `5` | Seconds between readings (signal + pings) |
| `--transfer-interval` | no | `60` | Seconds between download + upload tests |
| `--scan-interval` | no | `60` | Seconds between nearby access point scans |
| `--pings` | no | `5` | Pings per reading |
| `--no-rescan` | no | off | Do not force a fresh Wi-Fi scan before each access point scan |

**Keep every setting the same on all laptops for the whole collection.**
Only `--location`, `--device` and `--db` should differ.

## Moving to a new location

Press Ctrl+C, walk to the new spot, then start again with the new `--location`
and the **same** `--db`. Write the move on the shared log sheet.

## Before campus day (checklist)

- [ ] Run it at home for 10 to 60 minutes with `--location home --db test.db`
- [ ] Open the file in DB Browser. Check `readings`, `pings`, `downloads`, `scans`
      and `scan_aps` all have rows, and that signal, download and scan values are not empty
- [ ] Check that `ap_count` in `scans` changes over time (it should, now that a fresh scan is forced)
- [ ] Stop the laptop from sleeping while plugged in:
      `powercfg /change standby-timeout-ac 0`
- [ ] Turn off Wi-Fi power saving in your adapter settings if you can

## On campus

- Plug in, keep the window open, do not close the lid.
- Use a fresh file for campus data, e.g. `--db campus_kirby.db`, so home tests do not mix in.
- At the end of each day: **stop the collector first**, then copy the `.db` to the shared Drive.
  (While it runs you will see `.db-wal` and `.db-shm` files. They merge into the `.db` when it stops.)

## Viewing the data

Open the `.db` in DB Browser for SQLite → **Browse Data** → pick a table.
Tick **Read only** when opening if the collector is still running, otherwise
DB Browser can lock the file.

## If something looks wrong

| You see | Meaning |
| --- | --- |
| `signal no wifi` but latency works | Windows is not in English, or you are on Ethernet. Send the output of `netsh wlan show interfaces` to A |
| `latency all lost`, `loss 100%` | No internet, or port 443 is blocked |
| `down FAILED` every time | The test site is blocked. Open the download URL in a browser to check |
| `APs FAILED` | The scan command failed. On Mac, install `pyobjc-framework-CoreWLAN` |
| `error: the following arguments are required` | You left out `--location` or `--device` |

## Data use

About 1.5 MB per minute (1 MB down + 0.5 MB up), roughly 90 MB per hour per laptop.
