# iPod Sync

Raspberry Pi app that automatically syncs your Jellyfin music library and
podcast subscriptions (Pinepods or RSS) to an iPod Classic running Rockbox.

## Features

- **Auto-sync on connect** — udev detects the iPod and triggers a sync
- **Full Jellyfin music library** sync to iPod filesystem
- **Podcasts** from Pinepods subscriptions or any RSS feed
  - Played episodes are deleted from the device
  - Max 10 (configurable) episodes per podcast
- **Web UI** — configure credentials, manage podcast subscriptions, view logs

## Requirements

- Raspberry Pi running Raspberry Pi OS (Bookworm or later)
- Python 3.11+
- iPod Classic with [Rockbox](https://www.rockbox.org/) installed
- Jellyfin media server (for music)
- Pinepods instance and/or RSS podcast feeds

## Installation

```bash
git clone <repo-url> /home/pi/ipod-sync-src
cd /home/pi/ipod-sync-src
sudo bash install.sh
```

Then open `http://<pi-ip>:8000` in your browser.

## Quick Start

1. Go to **Settings** and enter your Jellyfin URL, API key, and User ID
2. Enter your Pinepods URL and API key (optional)
3. Set the iPod mount point or rely on auto-detection by filesystem label (`IPOD`)
4. Click **Save All Settings**
5. Go to **Podcasts** and add RSS feeds or import from Pinepods
6. Plug in your iPod — sync starts automatically, or click **Sync Now**

## File Layout on the iPod

```
/Music/<Artist>/<Album>/<NN - Title.mp3>
/Podcasts/<Podcast Name>/<Episode Title.mp3>
```

Rockbox scans these automatically and builds its database on boot.
`Podcasts/` contains a `database.ignore` marker (created automatically by
the sync) so podcast episodes are excluded from Rockbox's tag Database —
otherwise their ID3 artist tags (the show name) would show up mixed in
with music artists.

### Browsing podcasts by show (Apple Podcasts-style)

Since podcasts are excluded from the Database, the sync also maintains a
single **"Podcasts"** entry in `.rockbox/shortcuts.txt` that links to the
`Podcasts/` folder. Opening it lists every show, and opening a show lists
its episodes — the same "pick a show, then an episode" flow as Apple's
stock Podcasts app.

Rockbox doesn't show its **Shortcuts** menu by default, so enable it once
on the device: **Settings → General Settings → Root Menu**, add
`Shortcuts` to the menu order. (Any shortcuts you add by hand elsewhere in
that file are left alone.)

## GPIO Buttons &amp; Status LEDs (optional)

You can wire physical buttons and LEDs to the Pi's GPIO header for
mounting, unmounting, and syncing without opening the web UI:

- **Mount button** — (re)mounts the iPod
- **Unmount button** — safely ejects the iPod
- **Sync button** — starts a sync
- **Mounted LED** — on while the iPod is mounted
- **Removable LED** — on while the iPod is unmounted (safe to unplug)
- **Syncing LED** — flashes while a sync is running

Every pin is independently optional — wire only what you have. Configure
BCM pin numbers under **Settings → GPIO Buttons & LEDs**. Buttons should
be wired to GND (internal pull-up is used, no external resistor needed);
LEDs need a series resistor to GND.

`install.sh` installs the `python3-lgpio` GPIO backend via apt and adds
the service user to the `gpio` group. If you're running the app outside
of `install.sh` (e.g. for development), install `gpiozero` plus a pin
factory backend (`lgpio` on Pi 4/5, `RPi.GPIO` on older boards).

## Services

| Service | Description |
|---|---|
| `ipod-sync` | FastAPI web app (port 8000) |
| `ipod-sync-daemon` | USB detection daemon |

```bash
# View logs
journalctl -fu ipod-sync
journalctl -fu ipod-sync-daemon

# Restart
sudo systemctl restart ipod-sync ipod-sync-daemon
```

## Project Structure

```
app/
  main.py              FastAPI app entry point
  database.py          SQLAlchemy async setup
  models.py            DB models (settings, podcasts, music, logs)
  config.py            Settings helpers
  services/
    jellyfin.py        Jellyfin API client
    pinepods.py        Pinepods API client
    rss.py             RSS feed parser
    ipod.py            iPod filesystem manager
    sync.py            Sync orchestrator
  routes/
    api.py             REST API endpoints
    web.py             HTML page routes
  templates/           Jinja2 + Bootstrap 5 UI
daemon.py              udev USB detection daemon
install.sh             Installer script
ipod-sync.service      systemd service (web app)
ipod-sync-daemon.service systemd service (USB daemon)
90-ipod.rules          udev rules
media-ipod.mount       systemd mount unit
```
