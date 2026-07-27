"""
iPod / Rockbox filesystem manager.

Rockbox uses a plain FAT32 filesystem — no iTunes database needed.
Files are organized as:

  <mount>/Music/<Artist>/<Album>/<NN - Title.ext>
  <mount>/Podcasts/<Podcast Title>/<Episode Title.ext>

After syncing, Rockbox will auto-update its database on next boot
(or when "Update Now" is triggered from the main menu).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import unicodedata
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

# Characters that are illegal on FAT32/vfat, plus apostrophe (both ASCII U+0027
# and any residual after Unicode normalisation) which triggers [Errno 22] on
# some vfat mounts.
_ILLEGAL = re.compile(r"[<>:\"'/\\|?*\x00-\x1f]")

# Markers delimiting the auto-generated block inside .rockbox/shortcuts.txt
# so we can regenerate it each sync without clobbering shortcuts the user
# added by hand elsewhere in the file.
_SHORTCUTS_BEGIN = "# --- ipod-sync: podcast shortcuts (auto-generated, do not edit below) ---"
_SHORTCUTS_END = "# --- ipod-sync: end podcast shortcuts ---"


def safe_name(s: str, max_len: int = 64) -> str:
    """Sanitise a string for use as a FAT32 directory-name component.

    Normalises Unicode to ASCII (NFKD decomposition) so that smart quotes,
    accented letters and other non-ASCII chars don't cause [Errno 22] on
    vfat mounts that lack UTF-8 support.  Illegal FAT32 characters are then
    replaced with underscores.

    Strip is applied both before AND after truncation so the result never
    ends with a space or dot (FAT32 forbids both).
    """
    # NFKD decomposes combined chars (é → e + combining accent); encoding to
    # ASCII with 'ignore' strips the non-ASCII residue (accents, smart quotes,
    # etc.), leaving plain ASCII equivalents where possible.
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = _ILLEGAL.sub("_", s).strip(". ")
    return s[:max_len].strip(". ") or "Unknown"


def safe_filename(s: str) -> str:
    """Sanitise a complete filename (stem + extension) for FAT32.

    Unlike safe_name, this does not truncate the stem — it preserves the
    extension and only shortens the stem if the total would exceed 255 chars
    (the FAT32 LFN limit per path component).
    """
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = _ILLEGAL.sub("_", s).strip(". ")
    if len(s) <= 255:
        return s or "Unknown"
    # Preserve the extension when truncating.
    p = Path(s)
    ext = p.suffix          # e.g. ".mp3"
    stem = p.stem[:255 - len(ext)].strip(". ")
    return (stem + ext) or "Unknown"


class IPodManager:
    def __init__(self, mount_point: str):
        self.mount = Path(mount_point)

    # ------------------------------------------------------------------
    # Mount helpers
    # ------------------------------------------------------------------

    def is_mounted(self) -> bool:
        return self.mount.is_dir() and any(self.mount.iterdir())

    @staticmethod
    def find_ipod_mount(label: str = "IPOD") -> str | None:
        """
        Try to find where the iPod is mounted by its filesystem label.
        Works on Linux (checks /proc/mounts and /media paths).
        """
        # Check /proc/mounts for the label
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        device, mount = parts[0], parts[1]
                        # Check blkid for label.
                        # The service user may not have direct device access;
                        # try plain blkid first (works if user is in `disk`
                        # group), fall back to sudo blkid (sudoers rule in
                        # install.sh grants this without a password).
                        try:
                            for blkid_cmd in (
                                ["blkid", "-s", "LABEL", "-o", "value", device],
                                ["sudo", "blkid", "-s", "LABEL", "-o", "value", device],
                            ):
                                try:
                                    out = subprocess.check_output(
                                        blkid_cmd,
                                        stderr=subprocess.DEVNULL,
                                        timeout=5,
                                    ).decode().strip()
                                    break
                                except subprocess.CalledProcessError:
                                    out = ""
                                    continue
                            if out.upper() == label.upper():
                                return mount
                        except Exception:
                            pass
        except FileNotFoundError:
            pass

        # Fallback: look under /media for a directory matching the label
        for base in ["/media", "/mnt"]:
            candidate = Path(base) / label
            if candidate.is_dir():
                return str(candidate)
            # /media/<user>/<label>
            if Path(base).exists():
                for user_dir in Path(base).iterdir():
                    candidate = user_dir / label
                    if candidate.is_dir():
                        return str(candidate)

        return None

    # ------------------------------------------------------------------
    # Music
    # ------------------------------------------------------------------

    def music_path(self, artist: str, album: str, filename: str) -> Path:
        return (
            self.mount
            / "Music"
            / safe_name(artist)
            / safe_name(album)
            / safe_filename(filename)
        )

    def music_rel(self, artist: str, album: str, filename: str) -> str:
        return str(
            Path("Music") / safe_name(artist) / safe_name(album) / safe_filename(filename)
        )

    def track_filename(self, track: "JellyfinTrack") -> str:  # type: ignore[name-defined]
        num = f"{track.track_number:02d} - " if track.track_number else ""
        ext = track.container if not track.container.startswith(".") else track.container[1:]
        return f"{num}{safe_name(track.title)}.{ext}"

    async def copy_music_track(self, src: str, artist: str, album: str, filename: str) -> str:
        """Copy a local file to the iPod music tree. Returns relative path."""
        dest = self.music_path(artist, album, filename)
        await asyncio.to_thread(self._copy, src, dest)
        return self.music_rel(artist, album, filename)

    def remove_music_track(self, rel_path: str) -> None:
        full = self.mount / rel_path
        if full.exists():
            full.unlink()
            self._remove_empty_parents(full.parent, self.mount / "Music")

    # ------------------------------------------------------------------
    # Podcasts
    # ------------------------------------------------------------------

    def podcast_path(self, podcast_title: str, filename: str) -> Path:
        return self.mount / "Podcasts" / safe_name(podcast_title) / safe_filename(filename)

    def podcast_rel(self, podcast_title: str, filename: str) -> str:
        return str(Path("Podcasts") / safe_name(podcast_title) / safe_filename(filename))

    def ensure_podcast_ignore(self) -> None:
        """
        Exclude Podcasts/ from Rockbox's tag database.

        Rockbox's Database (Artist/Album browsing) is built from ID3 tags
        scanned across the whole disk, not from folder structure. Podcast
        episodes keep the ID3 "artist" tag from the source feed (the show
        name), so without this they get lumped into the Artist list next to
        real music artists. `database.ignore` tells Rockbox's tagcache
        scanner to skip this subtree; podcasts stay reachable via Files.
        """
        podcasts_dir = self.mount / "Podcasts"
        try:
            podcasts_dir.mkdir(parents=True, exist_ok=True)
            (podcasts_dir / "database.ignore").touch()
        except Exception as exc:
            log.warning("Could not create database.ignore in Podcasts/: %s", exc)

    async def copy_podcast_episode(
        self, src: str, podcast_title: str, filename: str
    ) -> str:
        """Copy a downloaded podcast episode to the iPod. Returns relative path."""
        dest = self.podcast_path(podcast_title, filename)
        await asyncio.to_thread(self._copy, src, dest)
        return self.podcast_rel(podcast_title, filename)

    def write_podcast_shortcuts(self) -> None:
        """
        Maintain a "Podcasts" section in .rockbox/shortcuts.txt: one
        shortcut per podcast show currently on the device, each linking
        straight to its Podcasts/<Show> folder. This gives an Apple
        Podcasts-style flow (pick a show, then pick an episode) as a
        single tap from Rockbox's Shortcuts root-menu item, instead of
        drilling through Files -> Podcasts -> <Show> every time.

        Since database.ignore keeps podcasts out of the tag database (see
        ensure_podcast_ignore), Shortcuts is the only way to give them a
        dedicated browse entry. Rockbox doesn't enable "Shortcuts" in its
        root menu by default — it must be added once on the device under
        Settings > General Settings > Root Menu.

        Any shortcuts outside our marked section (e.g. ones the user
        added by hand) are preserved as-is.
        """
        podcasts_dir = self.mount / "Podcasts"
        shortcuts_file = self.mount / ".rockbox" / "shortcuts.txt"

        shows = sorted(
            p.name for p in podcasts_dir.iterdir() if p.is_dir()
        ) if podcasts_dir.is_dir() else []

        block_lines = [_SHORTCUTS_BEGIN]
        for show in shows:
            block_lines += [
                "[shortcut]",
                "type: browse",
                f"data: /Podcasts/{show}",
                f"name: {show[:64]}",
            ]
        block_lines.append(_SHORTCUTS_END)
        block = "\n".join(block_lines)

        try:
            existing = (
                shortcuts_file.read_text(encoding="utf-8")
                if shortcuts_file.exists() else ""
            )
        except Exception:
            existing = ""

        if _SHORTCUTS_BEGIN in existing and _SHORTCUTS_END in existing:
            pre, _, rest = existing.partition(_SHORTCUTS_BEGIN)
            _, _, post = rest.partition(_SHORTCUTS_END)
            new_content = pre + block + post
        else:
            sep = "\n" if existing and not existing.endswith("\n") else ""
            new_content = existing + sep + block + "\n"

        try:
            shortcuts_file.parent.mkdir(parents=True, exist_ok=True)
            shortcuts_file.write_text(new_content, encoding="utf-8")
        except Exception as exc:
            log.warning("Could not write shortcuts.txt: %s", exc)

    def remove_podcast_episode(self, rel_path: str) -> None:
        full = self.mount / rel_path
        if full.exists():
            full.unlink()
            self._remove_empty_parents(full.parent, self.mount / "Podcasts")

    # ------------------------------------------------------------------
    # Free space
    # ------------------------------------------------------------------

    def free_bytes(self) -> int:
        stat = shutil.disk_usage(self.mount)
        return stat.free

    def total_bytes(self) -> int:
        stat = shutil.disk_usage(self.mount)
        return stat.total

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _copy(src: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Use a UUID-based temp name so the temp file never contains characters
        # that are invalid on the FAT32/vfat mount (e.g. apostrophes in track
        # titles cause [Errno 22] Invalid argument when the temp name is derived
        # from the destination filename).
        tmp = dest.parent / f".{uuid.uuid4().hex}.part"
        shutil.copy2(src, tmp)
        tmp.rename(dest)
        log.debug("Copied %s → %s", src, dest)

    @staticmethod
    def _remove_empty_parents(directory: Path, stop_at: Path) -> None:
        """Walk up, removing empty directories until stop_at."""
        try:
            while directory != stop_at and directory.exists():
                if not any(directory.iterdir()):
                    directory.rmdir()
                    directory = directory.parent
                else:
                    break
        except Exception as exc:
            log.debug("Could not clean up empty dir: %s", exc)

    def trigger_database_update(self) -> None:
        """
        Signal Rockbox to rebuild its music database on next boot.

        Rockbox watches for /.rockbox/database_rebuild at startup; when the
        file exists it performs a full database scan and then removes it.
        """
        flag = self.mount / ".rockbox" / "database_rebuild"
        try:
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.touch()
            log.info("Touched %s — Rockbox will rebuild its database on next boot", flag)
        except Exception as exc:
            log.warning("Could not create database_rebuild flag: %s", exc)

    def list_music_files(self) -> set[str]:
        """Return a set of relative paths for all files under Music/."""
        root = self.mount / "Music"
        if not root.exists():
            return set()
        return {
            str(p.relative_to(self.mount))
            for p in root.rglob("*")
            if p.is_file()
        }

    def list_podcast_files(self) -> set[str]:
        root = self.mount / "Podcasts"
        if not root.exists():
            return set()
        return {
            str(p.relative_to(self.mount))
            for p in root.rglob("*")
            if p.is_file()
        }
