# iPod Sync

Raspberry Pi app that automatically syncs your Jellyfin music library and
podcast subscriptions (Pinepods or RSS) to an iPod Classic running Rockbox.
Also runs on a Bazzite handheld (e.g. ROG Ally) so you can sync away from
home over Tailscale — see [Installing on Bazzite](#installing-on-bazzite-rog-ally-etc)
below.

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

## Installing on Bazzite (ROG Ally, etc.)

The app itself doesn't care what it runs on — the only hard requirement
is that the **iPod is plugged into the same machine running the daemon**,
since syncing works by mounting the iPod's onboard filesystem and writing
files to it directly (not over the network). Jellyfin/Pinepods, on the
other hand, are just HTTP APIs, so if your handheld reaches your home
server over Tailscale, syncing away from home works exactly like it does
on the Pi at home — just point the Jellyfin/Pinepods URLs in Settings at
your home server's Tailscale IP or MagicDNS name.

Bazzite is an immutable, rpm-ostree-based image (no `apt-get`), so use
`install-bazzite.sh` instead of `install.sh`:

```bash
git clone <repo-url> ~/ipod-sync-src
cd ~/ipod-sync-src
sudo bash install-bazzite.sh
```

It installs the same systemd services, udev rules, and sudoers file as
the Pi installer — `/etc` is writable on ostree systems, so none of that
needs special handling. The only difference is dependency handling: it
checks whether `rsync`, `udisks2`, `util-linux`, and `python3` are already
part of the base image (they usually are) and only layers anything
missing via `rpm-ostree install --apply-live`. If a layered package needs
a reboot to fully take effect, the installer will tell you.

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
