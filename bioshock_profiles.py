#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BioShock Infinite Profile Manager
=================================

Kleines Windows-Tool (Python + Tkinter, nur Standardbibliothek), mit dem sich
mehrere unabhängige Spielstand-Profile für BioShock Infinite (Steam, App-ID 8870)
verwalten lassen, ohne dass ein "Neues Spiel" den alten Spielstand überschreibt.

Prinzip
-------
* Der "Live-Ordner" ist der Save-Ordner, den das Spiel tatsächlich benutzt.
  Sein Inhalt gehört immer zum *aktiven* Profil (steht in config.json).
* Jedes Profil ist eine vollständige Kopie dieses Ordners unter
  Dokumente\\BioShockProfiles\\<Profilname>\\.
* Wechseln:      Live-Stand -> aktives Profil, danach Zielprofil -> Live-Ordner.
* Neues Profil:  Live-Stand -> aktives Profil, danach Live-Ordner leeren.
* Vor jeder verändernden Aktion entsteht ein Zeitstempel-Backup unter
  BioShockProfiles\\_backups\\. Es wird nie etwas gelöscht oder überschrieben,
  von dem nicht vorher eine geprüfte Kopie existiert.
* Spielwerte (experimentell): Infusions-Stufen (Leben, Schild, Salze), Silberadler und
  Dietriche werden direkt im Spielstand gelesen und geändert (Aufbau siehe Abschnitt
  „Spielstand-Dateien“).
* Oberfläche auf Deutsch und Englisch (tr() und Wörterbuch EN am Dateiende).

Autor: Baistus · MIT-Lizenz · https://github.com/Baistus/bioshock-infinite-profile-manager

Gliederung der Datei: Konstanten · Pfade · Datei-Hilfen · Save-Ordner-Erkennung ·
Spielprüfung · Konfiguration · ProfileManager · Spielstand-Dateien · Design/Widgets ·
Infusions-Editor · Hauptfenster · Programmstart

Start:  python bioshock_profiles.py
Tests:  python -m unittest test_bioshock_profiles -v
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hashlib
import json
import logging
import logging.handlers
import math
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import threading
import time
import webbrowser
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox

# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------

APP_NAME = "BioShock Infinite Profile Manager"
APP_VERSION = "2.1.0"
APP_AUTHOR = "Baistus"
APP_YEAR = "2026"
APP_URL = "https://github.com/Baistus/bioshock-infinite-profile-manager"
APP_LICENSE = "MIT"

STEAM_APP_ID = "8870"
STEAM_RUN_URL = "steam://rungameid/" + STEAM_APP_ID
GAME_EXE_NAMES = ("BioShockInfinite.exe",)

PROFILES_DIRNAME = "BioShockProfiles"
CONFIG_FILENAME = "config.json"
LOG_FILENAME = "manager.log"
BACKUP_DIRNAME = "_backups"
WORK_DIRNAME = "_work"  # Zwischenablage für das atomare Ersetzen von Profilen

DEFAULT_PROFILE_NAME = "Standard"
MAX_NAME_LENGTH = 40  # kurz halten, damit Backup-Pfade unter 260 Zeichen bleiben
SAVE_FILE_EXTENSIONS = (".sav", ".bkm", ".bin")
MAX_LIVE_FILES = 2000  # Sicherheitsnetz: ein echter Save-Ordner hat nie so viele Dateien

INVALID_NAME_CHARS = '<>:"/\\|?*'
RESERVED_NAMES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {"COM%d" % i for i in range(1, 10)}
    | {"LPT%d" % i for i in range(1, 10)}
)

log = logging.getLogger("bioshock_profiles")


# ---------------------------------------------------------------------------
# Sprache (Deutsch / Englisch)
# ---------------------------------------------------------------------------
#
# Alle Oberflächentexte stehen im Code auf Deutsch und laufen durch tr(). Die
# englischen Entsprechungen stehen im Wörterbuch EN am Ende der Datei; ein Test
# prüft, dass jeder Text eine Übersetzung mit denselben Platzhaltern hat.

LANGUAGES = {"de": "Deutsch", "en": "English"}
_LANGUAGE = {"code": "de"}


def tr(text: str) -> str:
    """Übersetzt einen (deutschen) Oberflächentext in die eingestellte Sprache."""
    if _LANGUAGE["code"] == "en":
        return EN.get(text, text)
    return text


def set_language(code: str) -> None:
    _LANGUAGE["code"] = code if code in LANGUAGES else "en"


def current_language() -> str:
    return _LANGUAGE["code"]


def system_language() -> str:
    """Deutsch, wenn Windows auf Deutsch läuft, sonst Englisch."""
    try:
        import ctypes
        return "de" if (ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF) == 0x07 else "en"
    except Exception:
        import locale
        loc = (locale.getlocale()[0] or "").lower()
        return "de" if loc.startswith(("de", "german")) else "en"


# ---------------------------------------------------------------------------
# Fehlerklassen
# ---------------------------------------------------------------------------

class ProfileError(Exception):
    """Fehler mit verständlicher (deutscher) Meldung für die Oberfläche."""


class GameRunningError(ProfileError):
    """BioShock Infinite läuft – der Live-Ordner darf nicht verändert werden."""


# ---------------------------------------------------------------------------
# Windows-Pfade ermitteln
# ---------------------------------------------------------------------------

def get_documents_dir() -> Path:
    """Liefert den echten Dokumente-Ordner (berücksichtigt Umleitungen, z. B. OneDrive)."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

            # FOLDERID_Documents = {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
            folder_id = GUID(
                0xFDD39AD0, 0x238F, 0x46AF,
                (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7),
            )
            buf = ctypes.c_wchar_p()
            shell32 = ctypes.windll.shell32
            shell32.SHGetKnownFolderPath.restype = ctypes.c_long
            if shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(buf)) == 0:
                try:
                    if buf.value:
                        return Path(buf.value)
                finally:
                    ctypes.windll.ole32.CoTaskMemFree(buf)
        except Exception:  # pragma: no cover - nur Fallback
            log.debug("SHGetKnownFolderPath fehlgeschlagen", exc_info=True)
    return Path(os.environ.get("USERPROFILE") or str(Path.home())) / "Documents"


def get_default_profiles_dir() -> Path:
    """Dokumente\\BioShockProfiles"""
    return get_documents_dir() / PROFILES_DIRNAME


def find_steam_dirs() -> List[Path]:
    """Sucht die Steam-Installation(en) über die Registry und Standardpfade."""
    found: List[Path] = []
    if os.name == "nt":
        try:
            import winreg

            keys = [
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
            ]
            for hive, subkey, value_name in keys:
                try:
                    with winreg.OpenKey(hive, subkey) as key:
                        value, _ = winreg.QueryValueEx(key, value_name)
                        if value:
                            # realpath liefert die echte Schreibweise ("C:\Program Files (x86)\Steam")
                            found.append(Path(os.path.realpath(os.path.normpath(value))))
                except OSError:
                    pass
        except ImportError:  # pragma: no cover - nur unter Windows relevant
            pass
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env)
        if base:
            found.append(Path(base) / "Steam")

    result: List[Path] = []
    seen = set()
    for path in found:
        key = _norm(path)
        if key not in seen and path.is_dir():
            seen.add(key)
            result.append(path)
    return result


# ---------------------------------------------------------------------------
# Datei-Hilfsfunktionen
# ---------------------------------------------------------------------------

def _norm(path) -> str:
    """Normalisierter Pfad-String für Vergleiche (Groß/Klein egal unter Windows)."""
    return os.path.normcase(os.path.abspath(str(path)))


def _is_same_or_inside(child, parent) -> bool:
    """True, wenn child == parent ist oder innerhalb von parent liegt."""
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def _on_remove_error(func, path, _exc) -> None:
    """Schreibgeschützte Dateien beschreibbar machen und Löschen erneut versuchen."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def rmtree_safe(path: Path) -> None:
    """shutil.rmtree, das auch schreibgeschützte Dateien entfernt."""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_on_remove_error)
    else:
        shutil.rmtree(path, onerror=_on_remove_error)


def clear_dir(path: Path) -> None:
    """Entfernt den *Inhalt* eines Ordners, der Ordner selbst bleibt bestehen."""
    if not path.is_dir():
        return
    for entry in path.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            rmtree_safe(entry)
        else:
            try:
                entry.unlink()
            except PermissionError:
                os.chmod(entry, stat.S_IWRITE)
                entry.unlink()


def copy_dir_contents(src: Path, dst: Path) -> None:
    """Kopiert den Inhalt von src nach dst (dst wird bei Bedarf angelegt)."""
    dst.mkdir(parents=True, exist_ok=True)
    if not src.is_dir():
        return
    for entry in src.iterdir():
        target = dst / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, target)


def dir_signature(path: Path, with_mtime: bool = False) -> Dict[str, Tuple[int, ...]]:
    """Relativer Dateipfad -> (Größe[, Änderungszeit in s]) für alle Dateien eines Ordners."""
    sig: Dict[str, Tuple[int, ...]] = {}
    if not path.is_dir():
        return sig
    for root, _dirs, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            st = os.stat(full)
            rel = os.path.relpath(full, path).replace("\\", "/")
            sig[rel] = (st.st_size, int(st.st_mtime)) if with_mtime else (st.st_size,)
    return sig


def files_identical(a: str, b: str, chunk: int = 1 << 20) -> bool:
    """Byteweiser Vergleich zweier Dateien (filecmp würde nach Größe/Zeit zwischenspeichern)."""
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            x, y = fa.read(chunk), fb.read(chunk)
            if x != y:
                return False
            if not x:
                return True


def dirs_identical(a: Path, b: Path) -> bool:
    """True, wenn beide Ordner dieselben Dateien mit identischem Inhalt enthalten.

    Größe und Änderungszeit reichen nicht: Der Infusions-Editor ändert Spielstände,
    ohne Größe oder Zeitstempel zu verändern.
    """
    sa, sb = dir_signature(a), dir_signature(b)
    if sa != sb:
        return False
    return all(files_identical(os.path.join(a, rel), os.path.join(b, rel)) for rel in sa)


def verify_copy(src: Path, dst: Path) -> None:
    """Prüft, ob dst dieselben Dateien (Namen + Größen) enthält wie src."""
    a, b = dir_signature(src), dir_signature(dst)
    if a != b:
        missing = sorted(set(a) - set(b))
        differing = sorted(k for k in set(a) & set(b) if a[k] != b[k])
        extra = sorted(set(b) - set(a))
        raise ProfileError(
            tr("Die Kopie nach „%s“ ist unvollständig (fehlend: %d, abweichend: %d, zusätzlich: %d).")
            % (dst, len(missing), len(differing), len(extra))
        )


def dir_stats(path: Path) -> Tuple[int, int, Optional[float]]:
    """(Anzahl Dateien, Gesamtgröße in Bytes, jüngste Änderungszeit) eines Ordners."""
    count, size, newest = 0, 0, None
    if not path.is_dir():
        return count, size, newest
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                continue
            count += 1
            size += st.st_size
            if newest is None or st.st_mtime > newest:
                newest = st.st_mtime
    return count, size, newest


def dir_has_files(path: Path) -> bool:
    if not path.is_dir():
        return False
    for _root, _dirs, files in os.walk(path):
        if files:
            return True
    return False


def count_files(path: Path, limit: int) -> int:
    """Zählt Dateien, bricht aber ab, sobald limit überschritten ist."""
    count = 0
    if not path.is_dir():
        return 0
    for _root, _dirs, files in os.walk(path):
        count += len(files)
        if count > limit:
            break
    return count


def format_size(num_bytes: int) -> str:
    if num_bytes >= 1024 * 1024:
        return "%.1f MB" % (num_bytes / (1024 * 1024))
    if num_bytes >= 1024:
        return "%d KB" % (num_bytes // 1024)
    return "%d B" % num_bytes


def format_time(timestamp: Optional[float]) -> str:
    if timestamp is None:
        return tr("–")
    fmt = "%d.%m.%Y %H:%M" if current_language() == "de" else "%Y-%m-%d %H:%M"
    return _dt.datetime.fromtimestamp(timestamp).strftime(fmt)


def _rename_with_retry(src: Path, dst: Path, attempts: int = 5) -> None:
    """os.rename mit kurzen Wiederholungen (Virenscanner/OneDrive sperren manchmal kurz)."""
    for attempt in range(attempts):
        try:
            os.rename(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.3)


# ---------------------------------------------------------------------------
# Save-Ordner erkennen und absichern
# ---------------------------------------------------------------------------

@dataclass
class SaveCandidate:
    path: Path
    description: str
    file_count: int
    newest_mtime: Optional[float]


def find_save_candidates(documents_dir: Optional[Path] = None,
                         steam_dirs: Optional[Sequence[Path]] = None) -> List[SaveCandidate]:
    """Sucht alle möglichen Save-Ordner von BioShock Infinite.

    1. Dokumente\\My Games\\BioShock Infinite\\SaveData (ggf. mit Unterordner je Steam-ID)
    2. Steam\\userdata\\<Steam-ID>\\8870\\remote (Steam-Cloud-Speicherort)
    """
    if documents_dir is None:
        documents_dir = get_documents_dir()
    if steam_dirs is None:
        steam_dirs = find_steam_dirs()

    raw: List[Tuple[Path, str]] = []

    savedata = documents_dir / "My Games" / "BioShock Infinite" / "SaveData"
    if savedata.is_dir():
        id_dirs = [d for d in savedata.iterdir() if d.is_dir() and d.name.isdigit()]
        has_direct_files = any(f.is_file() for f in savedata.iterdir())
        for d in id_dirs:
            raw.append((d, tr("Dokumente\\My Games (Steam-ID %s)") % d.name))
        if has_direct_files or not id_dirs:
            raw.append((savedata, tr("Dokumente\\My Games\\BioShock Infinite\\SaveData")))

    for steam in steam_dirs:
        userdata = steam / "userdata"
        if not userdata.is_dir():
            continue
        for user_dir in userdata.iterdir():
            if not (user_dir.is_dir() and user_dir.name.isdigit()):
                continue
            remote = user_dir / STEAM_APP_ID / "remote"
            if remote.is_dir():
                raw.append((remote, tr("Steam-Cloud-Ordner (Steam-ID %s)") % user_dir.name))

    candidates: List[SaveCandidate] = []
    seen = set()
    for path, desc in raw:
        key = _norm(path)
        if key in seen:
            continue
        seen.add(key)
        count, _size, newest = dir_stats(path)
        candidates.append(SaveCandidate(path, desc, count, newest))
    return candidates


def choose_save_dir(candidates: Sequence[SaveCandidate]) -> Optional[SaveCandidate]:
    """Bevorzugt Ordner mit Dateien, davon den zuletzt geänderten (dort schreibt das Spiel)."""
    with_files = [c for c in candidates if c.file_count > 0]
    if with_files:
        return max(with_files, key=lambda c: c.newest_mtime or 0.0)
    return candidates[0] if candidates else None


def is_steam_cloud_dir(path: Path) -> bool:
    parts = [p.lower() for p in path.parts]
    return "userdata" in parts and STEAM_APP_ID in parts


def describe_live_dir(path: Path) -> str:
    parts = [p.lower() for p in path.parts]
    if is_steam_cloud_dir(path):
        idx = parts.index("userdata")
        steam_id = path.parts[idx + 1] if idx + 1 < len(path.parts) else "?"
        return tr("Steam-Cloud-Ordner (Steam-ID %s)") % steam_id
    if "my games" in parts:
        return tr("Dokumente\\My Games")
    return tr("manuell gewählter Ordner")


def check_live_dir_safety(live: Path, profiles_dir: Path) -> Optional[str]:
    """Liefert eine Fehlermeldung, wenn der Ordner als Live-Ordner gefährlich wäre.

    Hintergrund: "Neues Profil" leert den Live-Ordner. Ein versehentlich gewählter
    Ordner wie C:\\ oder "Dokumente" darf deshalb niemals akzeptiert werden.
    """
    p = Path(os.path.abspath(str(live)))
    if p.parent == p:
        return tr("Ein ganzes Laufwerk (z. B. C:\\) kann nicht als Save-Ordner verwendet werden.")
    if _is_same_or_inside(p, profiles_dir) or _is_same_or_inside(profiles_dir, p):
        return tr("Der Save-Ordner darf weder im Profil-Ordner liegen noch ihn enthalten:\n%s") % profiles_dir

    home = Path.home()
    docs = get_documents_dir()
    if _is_same_or_inside(home, p) or _is_same_or_inside(docs, p):
        return tr("„%s“ ist ein allgemeiner Benutzerordner und kann nicht als Save-Ordner verwendet werden.") % p

    protected = [docs / "My Games", home / "Desktop", home / "Downloads", home / "OneDrive"]
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData", "APPDATA", "LOCALAPPDATA"):
        if os.environ.get(env):
            protected.append(Path(os.environ[env]))
    for steam in find_steam_dirs():
        protected += [steam, steam / "userdata"]
    for prot in protected:
        if _norm(p) == _norm(prot):
            return tr("„%s“ ist ein allgemeiner System- oder Programmordner und kann nicht als Save-Ordner verwendet werden.") % p

    windir = os.environ.get("SystemRoot") or os.environ.get("windir")
    if windir and _is_same_or_inside(p, windir):
        return tr("Ordner innerhalb von Windows können nicht als Save-Ordner verwendet werden.")
    return None


def looks_like_save_dir(path: Path) -> bool:
    """Grobe Plausibilitätsprüfung für manuell gewählte Ordner."""
    if path.name.lower() in ("savedata", "remote"):
        return True
    if not dir_has_files(path):
        return True  # leerer Ordner: unkritisch
    checked = 0
    for _root, _dirs, files in os.walk(path):
        for name in files:
            if name.lower().endswith(SAVE_FILE_EXTENSIONS):
                return True
            checked += 1
            if checked > 500:
                return False
    return False


# ---------------------------------------------------------------------------
# Läuft das Spiel?
# ---------------------------------------------------------------------------

def is_game_running() -> Optional[bool]:
    """True/False – oder None, wenn die Prüfung (tasklist) nicht möglich war."""
    if os.name != "nt":
        return None
    try:
        proc = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        log.warning("tasklist konnte nicht ausgeführt werden", exc_info=True)
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout.decode("utf-8", errors="replace").lower()
    # CSV-Format: "BioShockInfinite.exe","1234",... -> mit Anführungszeichen suchen
    return any('"%s"' % name.lower() in text for name in GAME_EXE_NAMES)


# ---------------------------------------------------------------------------
# Konfiguration (config.json)
# ---------------------------------------------------------------------------

class Config:
    """Kleine JSON-Konfiguration: Save-Ordner, aktives Profil, Hinweis-Einstellung."""

    DEFAULTS = {
        "version": 1,
        "live_dir": None,
        "active_profile": None,
        "hide_cloud_warning": False,
    }

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = dict(self.DEFAULTS)
        self.load_problem: Optional[str] = None  # Hinweis für die Oberfläche

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError(tr("kein JSON-Objekt"))
        except (OSError, ValueError) as exc:
            stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            broken = self.path.with_name("config.defekt_%s.json" % stamp)
            try:
                _rename_with_retry(self.path, broken)
                where = tr("Sie wurde als „%s“ aufbewahrt.") % broken.name
            except OSError:
                where = ""
            self.load_problem = (
                tr("Die Konfigurationsdatei war beschädigt und wurde zurückgesetzt (%s). %s\n\n"
                "Deine Profile und Backups sind davon nicht betroffen.") % (exc, where)
            )
            log.warning("config.json defekt: %s", exc)
            return
        self.data.update(raw)

    def save(self) -> None:
        """Schreibt atomar (erst temporäre Datei, dann ersetzen)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    @property
    def live_dir(self) -> Optional[Path]:
        value = self.data.get("live_dir")
        return Path(value) if isinstance(value, str) and value else None

    @live_dir.setter
    def live_dir(self, value: Optional[Path]) -> None:
        self.data["live_dir"] = str(value) if value else None

    @property
    def active_profile(self) -> Optional[str]:
        value = self.data.get("active_profile")
        return value if isinstance(value, str) and value else None

    @active_profile.setter
    def active_profile(self, value: Optional[str]) -> None:
        self.data["active_profile"] = value

    @property
    def hide_cloud_warning(self) -> bool:
        return bool(self.data.get("hide_cloud_warning"))

    @hide_cloud_warning.setter
    def hide_cloud_warning(self, value: bool) -> None:
        self.data["hide_cloud_warning"] = bool(value)


# ---------------------------------------------------------------------------
# Profil-Logik (ohne Oberfläche, daher gut testbar)
# ---------------------------------------------------------------------------

class ProfileManager:
    """Verwaltet Profile, Live-Ordner und Backups."""

    def __init__(self, profiles_dir: Path, config: Config,
                 game_running: Optional[Callable[[], Optional[bool]]] = None):
        self.profiles_dir = profiles_dir
        self.config = config
        # Funktion, die prüft, ob das Spiel läuft (für Tests austauschbar)
        self._game_running = game_running if game_running is not None else (lambda: is_game_running())

    # -- Eigenschaften -----------------------------------------------------

    @property
    def backups_dir(self) -> Path:
        return self.profiles_dir / BACKUP_DIRNAME

    @property
    def live_dir(self) -> Optional[Path]:
        return self.config.live_dir

    @property
    def active(self) -> Optional[str]:
        return self.config.active_profile

    # -- Profile auflisten / Namen prüfen ----------------------------------

    def list_profiles(self) -> List[str]:
        """Alle Profilordner (Ordner mit führendem _ oder . sind intern reserviert)."""
        if not self.profiles_dir.is_dir():
            return []
        names = [
            p.name for p in self.profiles_dir.iterdir()
            if p.is_dir() and not p.name.startswith(("_", "."))
        ]
        return sorted(names, key=str.casefold)

    def profile_path(self, name: str) -> Path:
        if not name or name in (".", "..") or any(c in INVALID_NAME_CHARS for c in name):
            raise ProfileError(tr("Ungültiger Profilname: „%s“") % name)
        return self.profiles_dir / name

    def resolve_profile(self, name: str) -> str:
        """Liefert den tatsächlichen Ordnernamen (Groß/Klein egal) eines bestehenden Profils."""
        for existing in self.list_profiles():
            if existing.casefold() == (name or "").strip().casefold():
                return existing
        raise ProfileError(tr("Das Profil „%s“ existiert nicht (mehr).") % name)

    def validate_name(self, raw: Optional[str], exclude: Optional[str] = None) -> str:
        """Prüft einen neuen Profilnamen und gibt ihn bereinigt zurück."""
        name = (raw or "").strip()
        if not name:
            raise ProfileError(tr("Bitte einen Namen eingeben."))
        if len(name) > MAX_NAME_LENGTH:
            raise ProfileError(tr("Der Name darf höchstens %d Zeichen lang sein.") % MAX_NAME_LENGTH)
        if any(c in INVALID_NAME_CHARS or ord(c) < 32 for c in name):
            raise ProfileError(tr("Der Name darf diese Zeichen nicht enthalten:  < > : \" / \\ | ? *"))
        if name.endswith("."):
            raise ProfileError(tr("Der Name darf nicht mit einem Punkt enden."))
        if name.startswith(("_", ".")):
            raise ProfileError(tr("Der Name darf nicht mit „_“ oder „.“ beginnen (intern reserviert)."))
        if name.split(".")[0].upper() in RESERVED_NAMES:
            raise ProfileError(tr("„%s“ ist unter Windows ein reservierter Name.") % name)
        for existing in self.list_profiles():
            if existing.casefold() == name.casefold():
                if exclude is None or existing.casefold() != exclude.casefold():
                    raise ProfileError(tr("Ein Profil mit dem Namen „%s“ gibt es bereits.") % existing)
        return name

    def _free_name(self, base: str) -> str:
        taken = {n.casefold() for n in self.list_profiles()}
        if base.casefold() not in taken:
            return base
        i = 2
        while ("%s %d" % (base, i)).casefold() in taken:
            i += 1
        return "%s %d" % (base, i)

    # -- Sicherheitsprüfungen ----------------------------------------------

    def _ensure_game_closed(self) -> None:
        if self._game_running() is True:
            raise GameRunningError(
                tr("BioShock Infinite läuft gerade.\n\n"
                "Bitte beende das Spiel vollständig und versuche es dann erneut. "
                "Sonst würde das Spiel die ausgetauschten Spielstände sofort wieder überschreiben.")
            )

    def _require_live_dir(self) -> Path:
        live = self.live_dir
        if live is None:
            raise ProfileError(tr("Es ist noch kein Save-Ordner festgelegt. Bitte über „Ändern…“ auswählen."))
        problem = check_live_dir_safety(live, self.profiles_dir)
        if problem:
            raise ProfileError(problem)
        if live.exists() and not live.is_dir():
            raise ProfileError(tr("„%s“ ist kein Ordner.") % live)
        if count_files(live, MAX_LIVE_FILES) > MAX_LIVE_FILES:
            raise ProfileError(
                tr("Der Save-Ordner „%s“ enthält über %d Dateien und ist daher sehr wahrscheinlich "
                "nicht der Save-Ordner von BioShock Infinite. Aus Sicherheitsgründen abgebrochen.")
                % (live, MAX_LIVE_FILES)
            )
        return live

    def set_live_dir(self, path: Path) -> None:
        problem = check_live_dir_safety(path, self.profiles_dir)
        if problem:
            raise ProfileError(problem)
        self.config.live_dir = Path(os.path.abspath(str(path)))
        self.config.save()
        log.info("Save-Ordner festgelegt: %s", self.config.live_dir)

    # -- Backups -----------------------------------------------------------

    def create_backup(self, action: str, include_live: bool = True,
                      profiles: Sequence[str] = (), details: str = "") -> Path:
        """Legt _backups\\<Zeitstempel>_<Aktion>\\ an und prüft jede Kopie.

        Unterordner:  live\\            = Inhalt des Save-Ordners
                      profil_<Name>\\   = bisheriger Inhalt eines Profils
        """
        stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        base = self.backups_dir / ("%s_%s" % (stamp, action))
        i = 2
        while base.exists():
            base = self.backups_dir / ("%s_%s_%d" % (stamp, action, i))
            i += 1
        lines = [
            tr("Backup erstellt von %s %s") % (APP_NAME, APP_VERSION),
            tr("Zeitpunkt:      %s") % _dt.datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            tr("Aktion:         %s") % (details or action),
            tr("Aktives Profil: %s") % (self.active or tr("–")),
            tr("Save-Ordner:    %s") % (self.live_dir or tr("–")),
            "",
            "Inhalt:",
        ]
        try:
            base.mkdir(parents=True)
            live = self.live_dir
            if include_live and live is not None and live.is_dir():
                copy_dir_contents(live, base / "live")
                verify_copy(live, base / "live")
                lines.append(tr("  live\\  = Inhalt des Save-Ordners (gehörte zum Profil „%s“)") % (self.active or tr("–")))
            for name in profiles:
                src = self.profile_path(name)
                if src.is_dir():
                    dst = base / ("profil_%s" % name)
                    copy_dir_contents(src, dst)
                    verify_copy(src, dst)
                    lines.append(tr("  profil_%s\\  = bisheriger Inhalt des Profils „%s“") % (name, name))
            lines += [
                "",
                tr("Wiederherstellen: Spiel beenden, dann den Inhalt des gewünschten Unterordners"),
                tr("in den Save-Ordner bzw. in den Profilordner kopieren."),
            ]
            (base / "info.txt").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        except ProfileError as exc:
            raise ProfileError(tr("Backup fehlgeschlagen: %s\n\nVorgang abgebrochen – es wurde nichts verändert.") % exc)
        except OSError as exc:
            raise ProfileError(tr("Backup konnte nicht erstellt werden (%s).\n\nVorgang abgebrochen – es wurde nichts verändert.") % exc)
        log.info("Backup erstellt: %s (%s)", base, details or action)
        return base

    def _profiles_needing_backup(self, name: Optional[str]) -> List[str]:
        """Bisheriger Profilinhalt muss nur gesichert werden, wenn er vom Live-Stand abweicht."""
        if not name:
            return []
        path = self.profile_path(name)
        live = self.live_dir
        if not dir_has_files(path):
            return []
        if live is not None and dirs_identical(path, live):
            return []  # identisch -> steckt bereits im live\-Backup
        return [name]

    # -- Kopier-Bausteine --------------------------------------------------

    def _replace_profile_with_live(self, name: str) -> None:
        """Ersetzt den Profilordner durch eine geprüfte Kopie des Live-Ordners.

        Ablauf: Kopie in _work\\neu_* -> prüfen -> altes Profil nach _work\\alt_*
        verschieben -> neue Kopie an die Stelle des Profils verschieben -> alt löschen.
        Der Aufrufer hat vorher ein Backup des alten Profilinhalts angelegt.
        """
        live = self._require_live_dir()
        dst = self.profile_path(name)
        work = self.profiles_dir / WORK_DIRNAME
        work.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        tmp = work / ("neu_" + stamp)
        old = work / ("alt_" + stamp)
        try:
            copy_dir_contents(live, tmp)
            verify_copy(live, tmp)
        except Exception:
            if tmp.exists():
                rmtree_safe(tmp)  # nur unsere eigene, unvollständige Kopie
            raise
        moved_old = False
        try:
            if dst.exists():
                _rename_with_retry(dst, old)
                moved_old = True
            _rename_with_retry(tmp, dst)
        except OSError as exc:
            if moved_old and not dst.exists():
                _rename_with_retry(old, dst)  # alten Zustand wiederherstellen
            if tmp.exists():
                rmtree_safe(tmp)
            raise ProfileError(
                tr("Das Profil „%s“ konnte nicht aktualisiert werden (%s). "
                "Ist der Ordner gerade in einem anderen Programm geöffnet?") % (name, exc)
            )
        if moved_old:
            try:
                rmtree_safe(old)
            except OSError:
                log.warning("Alter Profilstand konnte nicht entfernt werden: %s", old)

    def _load_profile_into_live(self, name: str) -> None:
        """Leert den Live-Ordner und kopiert das Profil hinein (Aufrufer hat gesichert)."""
        live = self._require_live_dir()
        src = self.profile_path(name)
        live.mkdir(parents=True, exist_ok=True)
        clear_dir(live)
        if src.is_dir():
            copy_dir_contents(src, live)
            verify_copy(src, live)

    def _restore_live_from(self, source: Path, backup: Path) -> None:
        """Notfall-Rückweg: Live-Ordner wieder auf den Stand von source bringen."""
        live = self._require_live_dir()
        try:
            live.mkdir(parents=True, exist_ok=True)
            clear_dir(live)
            copy_dir_contents(source, live)
            verify_copy(source, live)
        except Exception as exc:
            log.exception("Wiederherstellung des Save-Ordners fehlgeschlagen")
            raise ProfileError(
                tr("Der Vorgang ist fehlgeschlagen und der Save-Ordner konnte nicht automatisch "
                "wiederhergestellt werden (%s).\n\nDein Spielstand ist im Backup gesichert:\n%s\\live")
                % (exc, backup)
            )

    def _save_config_or_rollback(self, previous_profile: str, backup: Path) -> None:
        try:
            self.config.save()
        except OSError as exc:
            self.config.active_profile = previous_profile
            self._restore_live_from(self.profile_path(previous_profile), backup)
            raise ProfileError(tr("config.json konnte nicht gespeichert werden (%s). Der vorherige Stand wurde wiederhergestellt.") % exc)

    # -- Öffentliche Aktionen ----------------------------------------------

    def ensure_initial_profile(self) -> Optional[str]:
        """Sorgt dafür, dass es ein aktives Profil gibt, zu dem der Live-Ordner gehört.

        Beim allerersten Start wird der vorhandene Spielstand als Profil „Standard“
        übernommen. Rückgabe: Name eines neu angelegten Profils oder None.
        """
        live = self._require_live_dir()
        active = self.active
        if active:
            if not self.profile_path(active).is_dir():
                log.warning("Ordner des aktiven Profils fehlte, wird aus dem Save-Ordner neu angelegt: %s", active)
                self._replace_profile_with_live(active)
            return None
        name = self._free_name(DEFAULT_PROFILE_NAME)
        if dir_has_files(live):
            self.create_backup("ErstesProfil", include_live=True,
                               details=tr("Übernahme des vorhandenen Spielstands als Profil „%s“") % name)
        self._replace_profile_with_live(name)
        self.config.active_profile = name
        self.config.save()
        log.info("Erstes Profil angelegt: %s", name)
        return name

    def save_current(self) -> Tuple[bool, Optional[Path]]:
        """Kopiert den Live-Stand ins aktive Profil.

        Rückgabe: (geändert?, Backup-Pfad des alten Profilinhalts oder None)
        """
        live = self._require_live_dir()
        if self.active is None:
            self.ensure_initial_profile()
            return True, None
        active = self.active
        profile = self.profile_path(active)
        if profile.is_dir() and dirs_identical(live, profile):
            return False, None
        backup = None
        if dir_has_files(profile):
            backup = self.create_backup(tr("Sichern"), include_live=False, profiles=[active],
                                        details=tr("Manuelles Sichern von „%s“") % active)
        self._replace_profile_with_live(active)
        log.info("Aktueller Stand gesichert in Profil %s", active)
        return True, backup

    def switch_to(self, target: str) -> Path:
        """Live-Stand ins aktive Profil sichern, dann Zielprofil in den Live-Ordner laden."""
        target = self.resolve_profile(target)
        self._ensure_game_closed()
        self._require_live_dir()
        if self.active is None:
            self.ensure_initial_profile()
        active = self.active
        if target == active:
            raise ProfileError(tr("Das Profil „%s“ ist bereits aktiv.") % target)

        backup = self.create_backup(
            "Wechsel", include_live=True, profiles=self._profiles_needing_backup(active),
            details=tr("Wechsel von „%s“ zu „%s“") % (active, target),
        )
        self._replace_profile_with_live(active)
        try:
            self._load_profile_into_live(target)
        except Exception as exc:
            log.exception("Laden von %s fehlgeschlagen", target)
            self._restore_live_from(self.profile_path(active), backup)
            raise ProfileError(
                tr("Das Profil „%s“ konnte nicht geladen werden (%s).\n\n"
                "Der vorherige Stand von „%s“ wurde wiederhergestellt.") % (target, exc, active)
            )
        self.config.active_profile = target
        self._save_config_or_rollback(active, backup)
        log.info("Gewechselt: %s -> %s", active, target)
        return backup

    def new_profile(self, raw_name: str) -> Path:
        """Aktuellen Stand sichern, Live-Ordner leeren, neues (leeres) Profil aktivieren."""
        self._ensure_game_closed()
        live = self._require_live_dir()
        if self.active is None:
            self.ensure_initial_profile()
        name = self.validate_name(raw_name)
        active = self.active

        backup = self.create_backup(
            "NeuesProfil", include_live=True, profiles=self._profiles_needing_backup(active),
            details=tr("Neues Profil „%s“ (vorher aktiv: „%s“)") % (name, active),
        )
        self._replace_profile_with_live(active)
        new_dir = self.profile_path(name)
        new_dir.mkdir(parents=True, exist_ok=False)
        try:
            live.mkdir(parents=True, exist_ok=True)
            clear_dir(live)
        except Exception as exc:
            log.exception("Leeren des Save-Ordners fehlgeschlagen")
            self._restore_live_from(self.profile_path(active), backup)
            new_dir.rmdir()
            raise ProfileError(
                tr("Der Save-Ordner konnte nicht geleert werden (%s).\n\n"
                "Der vorherige Stand von „%s“ wurde wiederhergestellt.") % (exc, active)
            )
        self.config.active_profile = name
        self._save_config_or_rollback(active, backup)
        log.info("Neues Profil angelegt und aktiviert: %s (vorher %s)", name, active)
        return backup

    def rename_profile(self, old: str, new: str) -> str:
        old = self.resolve_profile(old)
        new = self.validate_name(new, exclude=old)
        if new == old:
            return old
        try:
            _rename_with_retry(self.profile_path(old), self.profile_path(new))
        except OSError as exc:
            raise ProfileError(
                tr("Das Profil konnte nicht umbenannt werden (%s). "
                "Ist der Ordner gerade in einem anderen Programm geöffnet?") % exc
            )
        if self.active == old:
            self.config.active_profile = new
            self.config.save()
        log.info("Profil umbenannt: %s -> %s", old, new)
        return new

    def delete_profile(self, name: str) -> Path:
        name = self.resolve_profile(name)
        if name == self.active:
            raise ProfileError(
                tr("Das aktive Profil kann nicht gelöscht werden.\n\n"
                "Wechsle zuerst zu einem anderen Profil (oder lege ein neues an).")
            )
        backup = self.create_backup("Loeschen", include_live=False, profiles=[name],
                                    details=tr("Löschen des Profils „%s“") % name)
        try:
            rmtree_safe(self.profile_path(name))
        except OSError as exc:
            raise ProfileError(
                tr("Das Profil konnte nicht vollständig gelöscht werden (%s).\n\nEin Backup liegt unter:\n%s")
                % (exc, backup)
            )
        log.info("Profil gelöscht: %s (Backup %s)", name, backup)
        return backup

    # -- Einzelne Spielstände (Infusions-Editor) ----------------------------

    def save_folder_for(self, profile: str) -> Path:
        """Wo die Spielstände eines Profils gerade liegen: aktiv -> Save-Ordner, sonst Profilordner."""
        profile = self.resolve_profile(profile)
        if profile == self.active:
            return self._require_live_dir()
        return self.profile_path(profile)

    def write_save_file(self, profile: str, path: Path, data: bytes, details: str) -> Path:
        """Ersetzt einen Spielstand durch `data` – mit Backup, atomarem Ersetzen und Gegenprobe.

        Der Zeitstempel der Datei bleibt erhalten, damit sich die Reihenfolge der
        Spielstände (z. B. für „Fortsetzen“) nicht verändert.
        """
        profile = self.resolve_profile(profile)
        folder = self.save_folder_for(profile)
        if not path.is_file() or not _is_same_or_inside(path, folder):
            raise ProfileError(tr("Der Spielstand „%s“ gehört nicht zum Profil „%s“.") % (path.name, profile))
        if profile == self.active:
            self._ensure_game_closed()

        # Backup der Originaldatei (mit relativem Pfad, damit man sie leicht zurücklegen kann)
        stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        base = self.backups_dir / ("%s_Spielwerte" % stamp)
        i = 2
        while base.exists():
            base = self.backups_dir / ("%s_Spielwerte_%d" % (stamp, i))
            i += 1
        sub = "live" if profile == self.active else "profil_%s" % profile
        target = base / sub / path.relative_to(folder)
        try:
            target.parent.mkdir(parents=True)
            shutil.copy2(path, target)
            if not files_identical(str(path), str(target)):
                raise OSError(tr("Backup-Kopie weicht ab"))
            (base / "info.txt").write_text(
                tr("Backup erstellt von %s %s\r\nZeitpunkt: %s\r\nAktion:    %s\r\nProfil:    %s\r\nDatei:     %s\r\n\r\n"
                "Wiederherstellen: Spiel beenden und die Datei aus %s\\ an ihren alten Platz kopieren.\r\n")
                % (APP_NAME, APP_VERSION, _dt.datetime.now().strftime("%d.%m.%Y %H:%M:%S"), details, profile,
                   path, sub), encoding="utf-8")
        except OSError as exc:
            raise ProfileError(tr("Backup konnte nicht erstellt werden (%s). Es wurde nichts verändert.") % exc)

        st = path.stat()
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_bytes(data)
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
            os.replace(tmp, path)
        except OSError as exc:
            if tmp.exists():
                tmp.unlink()
            raise ProfileError(tr("Der Spielstand konnte nicht geschrieben werden (%s). Das Original ist unverändert.") % exc)
        if path.read_bytes() != data:
            shutil.copy2(target, path)
            raise ProfileError(tr("Die Gegenprobe nach dem Schreiben ist fehlgeschlagen – das Original wurde "
                               "aus dem Backup wiederhergestellt."))
        log.info("Spielstand geändert: %s (%s), Backup %s", path, details, base)
        return base


# ---------------------------------------------------------------------------
# Spielstand-Dateien (.sav/.bkm): Infusionen lesen und ändern
# ---------------------------------------------------------------------------
#
# Aufbau (durch Analyse echter Spielstände ermittelt, nicht offiziell dokumentiert):
#   [16 Byte MD5 über den Rest der Datei]
#   [Kopf, Level-Liste …]
#   [UE3-Kompressionsblock: Tag 0x9E2A83C1, Blockgröße, (komp., roh) x2, zlib-Daten]
#        -> der erste Block enthält den persistenten Spielerzustand:
#           u32 Länge, u32 0xA13580C2, …, ab Byte 45:
#           u32 n + n x 16 Byte  „Dynamic Persistent Variables“
#           u32 n + n x Item     Inventar: u64 ID, i32 Menge, u8 Flag, i32, i32 k, k Zusatzbytes
#   [weitere Blöcke (Level-Zustände), Namenstabelle]
#
# Infusionen sind Inventar-Einträge; die Menge ist die Upgrade-Stufe (0–10).
# Beim Schreiben wird nur dieser eine Block neu komprimiert – exakt auf seine
# Originalgröße –, sodass alle anderen Bytes und Offsets der Datei unverändert bleiben.

INFUSION_STATS = (
    # Schlüssel, Item-ID, Anzeigename
    ("health", 276, "Leben"),
    ("shield", 329, "Schild"),
    ("salts", 235, "Salze"),
)
INFUSION_MAX_LEVEL = 10        # pro Wert (Item-Kapazität im Spiel)
INFUSIONS_MAIN_GAME = 24       # Infusionen im Hauptspiel
INFUSIONS_SEASON_PASS = 5      # zusätzlich am Blue Ribbon (Season Pass / Early Bird Pack)

UE3_CHUNK_TAG = b"\xc1\x83\x2a\x9e"
PLAYER_STATE_MAGIC = 0xA13580C2
PLAYER_DPV_OFFSET = 45

# Währungsblock im Spielerzustand (hinter dem Inventar):
#   Marker b2 81 25 c1, i32 Silberadler, 4x i32 (0), i32 Salze (aktuell), i32 (Zähler, unbekannt),
#   i32 Dietriche, i32 (0)
CURRENCY_MARKER = b"\xb2\x81\x25\xc1"
MONEY_MAX = 99999              # so hoch setzen auch gängige Trainer – vom Spiel nachweislich akzeptiert
LOCKPICKS_NORMAL_MAX = 30      # normale Obergrenze im Spiel (MaxLockpicksAttrib)
LOCKPICKS_MAX = 999            # technisch akzeptiert (Trainer-Wert), oberhalb von 30 wird gewarnt

SAVE_NAME_RE = re.compile(
    r"^(?P<map>.+)-(?P<campaign>\d+)_(?P<mo>\d+)_(?P<d>\d+)_(?P<y>\d{4})_(?P<h>\d+)_(?P<mi>\d+)_(?P<s>\d+)_(?P<seq>\d+)$"
)


class SaveFormatError(ProfileError):
    """Die Datei hat nicht den erwarteten Aufbau – sie wird dann nicht angefasst."""


def deflate_to_exact_size(raw: bytes, target: int, header: bytes) -> bytes:
    """Erzeugt einen gültigen zlib-Strom von genau `target` Bytes.

    Zuerst werden verschiedene Kompressionseinstellungen probiert. Passt keine exakt,
    wird der Strom gezielt etwas größer gemacht – entweder mit einem unkomprimierten
    „stored“-Block am Ende (byteweise einstellbar) oder durch Teilen des Stroms an
    einer Stelle p, wobei der zweite Teil den ersten als Wörterbuch nutzt. Das Ergebnis
    ist immer ein vollständig gültiger zlib-Strom ohne Füllbytes, und die Datei bleibt
    gleich groß.

    Das Spiel selbst komprimiert mit zlib-Stufe 1; mit Stufe 9 bleibt deshalb in echten
    Spielständen 30–470 Byte Spielraum – weit mehr, als eine Änderung je braucht.
    """
    adler = struct.pack(">I", zlib.adler32(raw) & 0xFFFFFFFF)
    deadline = time.monotonic() + 8.0

    def deflate(cfg, data: bytes, flush: int = zlib.Z_FINISH, zdict: bytes = b"") -> bytes:
        level, mem, strategy = cfg
        if zdict:
            co = zlib.compressobj(level, zlib.DEFLATED, -15, mem, strategy, zdict)
        else:
            co = zlib.compressobj(level, zlib.DEFLATED, -15, mem, strategy)
        return co.compress(data) + co.flush(flush)

    strategies = [zlib.Z_DEFAULT_STRATEGY, zlib.Z_FILTERED]
    for extra in ("Z_RLE", "Z_FIXED"):
        if hasattr(zlib, extra):
            strategies.append(getattr(zlib, extra))
    configs = [(lvl, mem, strat) for strat in strategies for lvl in (9, 6, 8, 7, 5, 4, 3, 2, 1) for mem in (8, 9, 7)]

    # 1) Direkt passende Einstellung?
    sizes = {}
    for cfg in configs:
        body = deflate(cfg, raw)
        if len(body) + 6 == target:
            return header + body + adler
        sizes[cfg] = len(body) + 6
    usable = sorted((cfg for cfg in configs if sizes[cfg] < target), key=lambda c: target - sizes[c])

    # 2) Zusätzliche Blockgrenze an Stelle p: neue Huffman-Tabellen verschieben die Größe
    #    um wenige Bytes (nach oben oder unten) – die feinste Stellschraube
    near = sorted((cfg for cfg in configs if abs(sizes[cfg] - target) <= 24), key=lambda c: abs(target - sizes[c]))
    modes = [m for m in (getattr(zlib, "Z_BLOCK", None), zlib.Z_PARTIAL_FLUSH) if m is not None]
    for cfg in near[:8]:
        step = max(1, len(raw) // 300)
        for mode in modes:
            for p in range(step, len(raw) - 1, step):
                level, mem, strategy = cfg
                co = zlib.compressobj(level, zlib.DEFLATED, -15, mem, strategy)
                body = co.compress(raw[:p]) + co.flush(mode) + co.compress(raw[p:]) + co.flush()
                if len(body) + 6 == target:
                    return header + body + adler
            if time.monotonic() > deadline:
                break

    # 3) Rest als „stored“-Block anhängen (Größe in 1-Byte-Schritten, ab ca. +10 Byte)
    for cfg in usable[:6]:
        for k in range(1, min(len(raw), 0xFFFF) + 1):
            body = deflate(cfg, raw[:-k], zlib.Z_SYNC_FLUSH)
            stored = b"\x01" + struct.pack("<HH", k, k ^ 0xFFFF) + raw[-k:]
            total = len(body) + len(stored) + 6
            if total == target:
                return header + body + stored + adler
            if total > target + 64:
                break

    # 4) Strom an Stelle p teilen und mit dem ersten Teil als Wörterbuch fortsetzen
    #    (ergibt fein abgestufte Größen knapp oberhalb der Normalgröße)
    for cfg in usable:
        step = max(1, len(raw) // 400)
        for p in range(step, len(raw) - 1, step):
            first = deflate(cfg, raw[:p], zlib.Z_SYNC_FLUSH)
            second = deflate(cfg, raw[p:], zdict=raw[max(0, p - 32768):p])
            if len(first) + len(second) + 6 == target:
                return header + first + second + adler
            if time.monotonic() > deadline:
                break
        if time.monotonic() > deadline:
            break
    raise SaveFormatError(tr("Die geänderten Daten lassen sich nicht in die Originalgröße des Spielstands einpassen."))


@dataclass
class SaveInfo:
    """Ein Spielstand im Save-Ordner (Dateiname zerlegt)."""
    path: Path
    map_name: str
    campaign: int
    sequence: int
    timestamp: Optional[_dt.datetime]
    is_bookmark: bool

    @property
    def kind(self) -> str:
        return tr("Kapitelanfang") if self.is_bookmark else tr("Autosave")

    @property
    def is_main_game(self) -> bool:
        return self.campaign == 0


def parse_save_name(path: Path) -> Optional[SaveInfo]:
    m = SAVE_NAME_RE.match(path.stem)
    if not m or path.suffix.lower() not in (".sav", ".bkm"):
        return None
    try:
        stamp = _dt.datetime(int(m["y"]), int(m["mo"]), int(m["d"]), int(m["h"]), int(m["mi"]), int(m["s"]))
    except ValueError:
        stamp = None
    return SaveInfo(path, m["map"], int(m["campaign"]), int(m["seq"]), stamp, path.suffix.lower() == ".bkm")


def list_saves(folder: Path) -> List[SaveInfo]:
    """Alle Spielstände eines Save- oder Profilordners, neueste zuerst."""
    save_dir = folder / "SaveData" if (folder / "SaveData").is_dir() else folder
    if not save_dir.is_dir():
        return []
    result = [info for info in (parse_save_name(p) for p in save_dir.iterdir() if p.is_file()) if info]
    return sorted(result, key=lambda i: (i.sequence, not i.is_bookmark), reverse=True)


@dataclass
class ItemRecord:
    offset: int      # Position im entpackten Spielerblock
    item_id: int
    amount: int


class BioShockSave:
    """Liest und ändert die Infusions-Stufen in einer .sav/.bkm-Datei."""

    def __init__(self, data: bytes, path: Optional[Path] = None):
        self.path = path
        self.data = bytes(data)
        self._parse()

    @classmethod
    def load(cls, path: Path) -> "BioShockSave":
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise SaveFormatError(tr("Spielstand konnte nicht gelesen werden: %s") % exc)
        return cls(data, path)

    # -- Lesen ---------------------------------------------------------------

    def _parse(self) -> None:
        d = self.data
        if len(d) < 256:
            raise SaveFormatError(tr("Die Datei ist zu klein für einen Spielstand."))
        if hashlib.md5(d[16:]).digest() != d[:16]:
            raise SaveFormatError(tr("Die Prüfsumme des Spielstands stimmt nicht (Datei beschädigt oder unbekanntes Format)."))

        pos = d.find(UE3_CHUNK_TAG, 16)
        while pos >= 0:
            if self._try_player_chunk(pos):
                break
            pos = d.find(UE3_CHUNK_TAG, pos + 1)
        if pos < 0:
            raise SaveFormatError(tr("Der Spielerzustand wurde in dieser Datei nicht gefunden."))
        self._parse_player_state()

    def _try_player_chunk(self, pos: int) -> bool:
        d = self.data
        if pos + 24 > len(d):
            return False
        _block, c1, r1, c2, r2 = struct.unpack_from("<5I", d, pos + 4)
        if c1 != c2 or r1 != r2 or not (0 < c1 <= r1 + 64) or r1 > 0x20000:
            return False  # nur Einzelblöcke bis 128 KB werden unterstützt
        start = pos + 24
        comp = d[start:start + c1]
        try:
            dec = zlib.decompressobj()
            raw = dec.decompress(comp)
        except zlib.error:
            return False
        if len(raw) != r1 or not dec.eof or dec.unused_data or len(raw) < PLAYER_DPV_OFFSET + 8:
            return False
        length, magic = struct.unpack_from("<II", raw, 0)
        if length != len(raw) - 4 or magic != PLAYER_STATE_MAGIC:
            return False
        self.comp_offset, self.comp_size, self.zlib_header = start, c1, comp[:2]
        self.raw = raw
        return True

    def _parse_player_state(self) -> None:
        raw = self.raw
        p = PLAYER_DPV_OFFSET
        n_vars = struct.unpack_from("<I", raw, p)[0]
        p += 4
        if n_vars > 20000 or p + 16 * n_vars + 4 > len(raw):
            raise SaveFormatError(tr("Unerwartete Variablentabelle im Spielerzustand."))
        for i in range(n_vars):
            if struct.unpack_from("<I", raw, p + 16 * i + 4)[0] != 0:
                raise SaveFormatError(tr("Unerwartete Variablentabelle im Spielerzustand."))
        p += 16 * n_vars
        n_items = struct.unpack_from("<I", raw, p)[0]
        p += 4
        if n_items > 20000:
            raise SaveFormatError(tr("Unerwartete Inventartabelle im Spielerzustand."))
        items: List[ItemRecord] = []
        for _ in range(n_items):
            if p + 21 > len(raw):
                raise SaveFormatError(tr("Die Inventartabelle ist unvollständig."))
            item_id, amount, flag, _unknown, extra = struct.unpack_from("<QiBii", raw, p)
            if flag not in (0, 1) or not (0 <= extra <= 64) or p + 21 + extra > len(raw):
                raise SaveFormatError(tr("Unerwarteter Inventareintrag im Spielerzustand."))
            items.append(ItemRecord(p, item_id, amount))
            p += 21 + extra
        self.items = items
        self.items_end = p
        self.currency_offset = self._find_currency_block(p)

    def _find_currency_block(self, start: int) -> Optional[int]:
        """Position des Geld-Werts – nur wenn der Block eindeutig und plausibel ist, sonst None."""
        raw = self.raw
        matches = []
        i = raw.find(CURRENCY_MARKER, start)
        while i >= 0 and i + 40 <= len(raw):
            money, z1, z2, z3, z4, salts, counter, picks, z5 = struct.unpack_from("<9i", raw, i + 4)
            if (money >= 0 and z1 == z2 == z3 == z4 == z5 == 0 and 0 <= salts <= 1000
                    and 0 <= counter <= 100000 and 0 <= picks <= 100000):
                matches.append(i + 4)
            i = raw.find(CURRENCY_MARKER, i + 1)
        return matches[0] if len(matches) == 1 else None

    def resources(self) -> Dict[str, Optional[int]]:
        """Silberadler, Dietriche und aktuelle Salze (None, falls der Block nicht sicher erkannt wurde)."""
        if self.currency_offset is None:
            return {"money": None, "lockpicks": None, "salts_current": None}
        money, *_zeros, salts, _counter, picks = struct.unpack_from("<8i", self.raw, self.currency_offset)
        return {"money": money, "lockpicks": picks, "salts_current": salts}

    def infusion_levels(self) -> Dict[str, Optional[int]]:
        """Stufe je Wert; None = noch nie eine Infusion dieses Typs gewählt (kein Eintrag)."""
        result: Dict[str, Optional[int]] = {}
        for key, item_id, _label in INFUSION_STATS:
            rec = next((r for r in self.items if r.item_id == item_id), None)
            result[key] = rec.amount if rec else None
        return result

    # -- Schreiben -----------------------------------------------------------

    def with_infusion_levels(self, levels: Dict[str, int]) -> bytes:
        """Neue Dateibytes mit geänderten Infusions-Stufen (die Datei selbst wird nicht geschrieben)."""
        return self.with_changes(infusions=levels)

    def with_changes(self, infusions: Optional[Dict[str, int]] = None,
                     money: Optional[int] = None, lockpicks: Optional[int] = None) -> bytes:
        """Neue Dateibytes mit geänderten Infusionen, Silberadlern und/oder Dietrichen."""
        levels = infusions or {}
        raw = bytearray(self.raw)
        expected = self.infusion_levels()
        expected_res = self.resources()
        if money is not None or lockpicks is not None:
            if self.currency_offset is None:
                raise SaveFormatError(tr("Silberadler und Dietriche wurden in diesem Spielstand nicht sicher erkannt "
                                      "und können deshalb nicht geändert werden."))
            if money is not None:
                if not 0 <= int(money) <= MONEY_MAX:
                    raise SaveFormatError(tr("Silberadler: %d liegt außerhalb von 0–%d.") % (money, MONEY_MAX))
                struct.pack_into("<i", raw, self.currency_offset, int(money))
                expected_res["money"] = int(money)
            if lockpicks is not None:
                if not 0 <= int(lockpicks) <= LOCKPICKS_MAX:
                    raise SaveFormatError(tr("Dietriche: %d liegt außerhalb von 0–%d.") % (lockpicks, LOCKPICKS_MAX))
                struct.pack_into("<i", raw, self.currency_offset + 28, int(lockpicks))
                expected_res["lockpicks"] = int(lockpicks)
        for key, item_id, label in INFUSION_STATS:
            if key not in levels:
                continue
            level = int(levels[key])
            if not 0 <= level <= INFUSION_MAX_LEVEL:
                raise SaveFormatError(tr("%s: Stufe %d liegt außerhalb von 0–%d.") % (tr(label), level, INFUSION_MAX_LEVEL))
            rec = next((r for r in self.items if r.item_id == item_id), None)
            if rec is None:
                if level == 0:
                    continue
                raise SaveFormatError(
                    tr("%s kann in diesem Spielstand noch nicht geändert werden, weil du im Spiel noch keine "
                    "%s-Infusion gewählt hast (der Eintrag existiert noch nicht).") % (tr(label), tr(label)))
            struct.pack_into("<i", raw, rec.offset + 8, level)
            expected[key] = level

        comp = deflate_to_exact_size(bytes(raw), self.comp_size, self.zlib_header)
        new = bytearray(self.data)
        new[self.comp_offset:self.comp_offset + self.comp_size] = comp
        new[:16] = hashlib.md5(bytes(new[16:])).digest()
        new = bytes(new)

        # Gegenprobe: neu einlesen, Werte vergleichen, Rest der Datei muss unverändert sein
        check = BioShockSave(new)
        if check.infusion_levels() != expected or check.resources() != expected_res or check.raw != bytes(raw):
            raise SaveFormatError(tr("Die Gegenprobe nach dem Ändern ist fehlgeschlagen – es wurde nichts gespeichert."))
        end = self.comp_offset + self.comp_size
        if len(new) != len(self.data) or new[16:self.comp_offset] != self.data[16:self.comp_offset] \
                or new[end:] != self.data[end:]:
            raise SaveFormatError(tr("Die Gegenprobe nach dem Ändern ist fehlgeschlagen – es wurde nichts gespeichert."))
        return new


# ---------------------------------------------------------------------------
# Kleine Bildverarbeitung (nur Standardbibliothek): PNG lesen, verkleinern, schreiben
# ---------------------------------------------------------------------------
#
# Tk kann Bilder nur grob (pixelweise) skalieren. Damit das BioShock-Infinite-Logo
# im Kopfbereich sauber aussieht, wird es hier selbst verkleinert. Das Logo wird
# zur Laufzeit aus dem lokalen Steam-Bibliotheks-Cache gelesen – es ist nicht Teil
# dieses Programms.

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_decode(data: bytes) -> Tuple[int, int, bytearray]:
    """Liest ein 8-Bit-PNG (RGB oder RGBA, ohne Interlacing) und liefert RGBA-Pixel."""
    if data[:8] != PNG_SIGNATURE:
        raise ValueError(tr("keine PNG-Datei"))
    pos, idat, header = 8, [], None
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    if header is None:
        raise ValueError(tr("PNG ohne Kopfdaten"))
    width, height, depth, color, _comp, _filter, interlace = header
    if depth != 8 or color not in (2, 6) or interlace:
        raise ValueError(tr("diese PNG-Variante wird nicht unterstützt"))
    bpp = 4 if color == 6 else 3
    raw = zlib.decompress(b"".join(idat))
    stride = width * bpp
    pixels = bytearray(height * stride)
    prev = bytearray(stride)
    p = 0
    for y in range(height):
        kind = raw[p]
        line = bytearray(raw[p + 1:p + 1 + stride])
        p += 1 + stride
        if kind == 1:      # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif kind == 2:    # Up
            line = bytearray((a + b) & 0xFF for a, b in zip(line, prev))
        elif kind == 3:    # Average
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif kind == 4:    # Paeth
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else (b if pb <= pc else c))) & 0xFF
        elif kind != 0:
            raise ValueError(tr("unbekannter PNG-Filter"))
        pixels[y * stride:(y + 1) * stride] = line
        prev = line
    if bpp == 3:
        rgba = bytearray(width * height * 4)
        rgba[0::4], rgba[1::4], rgba[2::4] = pixels[0::3], pixels[1::3], pixels[2::3]
        rgba[3::4] = b"\xff" * (width * height)
        pixels = rgba
    return width, height, pixels


def png_encode(width: int, height: int, rgba: bytes) -> bytes:
    """Schreibt RGBA-Pixel als PNG."""
    stride = width * 4
    raw = b"".join(b"\x00" + bytes(rgba[y * stride:(y + 1) * stride]) for y in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return (PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def alpha_bbox(width: int, height: int, rgba: bytes, threshold: int = 8) -> Tuple[int, int, int, int]:
    """Rechteck (x0, y0, x1, y1) um alle nicht durchsichtigen Pixel."""
    alpha = rgba[3::4]
    rows = [y for y in range(height) if max(alpha[y * width:(y + 1) * width]) > threshold]
    if not rows:
        return 0, 0, width, height
    y0, y1 = rows[0], rows[-1] + 1
    cols = [x for x in range(width) if max(alpha[y0 * width + x:y1 * width:width]) > threshold]
    return cols[0], y0, cols[-1] + 1, y1


def resize_rgba(width: int, height: int, rgba: bytes, box: Tuple[int, int, int, int],
                new_w: int, new_h: int) -> bytearray:
    """Verkleinert den Ausschnitt `box` mit einem Flächenfilter (Farben nach Deckkraft gewichtet)."""
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    xs = [(x0 + i * bw // new_w, x0 + max((i + 1) * bw // new_w, i * bw // new_w + 1)) for i in range(new_w)]
    ys = [(y0 + i * bh // new_h, y0 + max((i + 1) * bh // new_h, i * bh // new_h + 1)) for i in range(new_h)]
    out = bytearray(new_w * new_h * 4)
    o = 0
    for ya, yb in ys:
        for xa, xb in xs:
            r = g = b = a = 0
            for y in range(ya, yb):
                base = y * width * 4
                for i in range(base + 4 * xa, base + 4 * xb, 4):
                    al = rgba[i + 3]
                    r += rgba[i] * al
                    g += rgba[i + 1] * al
                    b += rgba[i + 2] * al
                    a += al
            n = (yb - ya) * (xb - xa)
            if a:
                out[o:o + 4] = bytes((r // a, g // a, b // a, (a + n // 2) // n))
            o += 4
    return out


def find_steam_logo() -> Optional[Path]:
    """Das Spiel-Logo aus dem lokalen Bibliotheks-Cache von Steam (falls vorhanden)."""
    for steam in find_steam_dirs():
        cache = steam / "appcache" / "librarycache"
        for candidate in (cache / STEAM_APP_ID / "logo.png", cache / ("%s_logo.png" % STEAM_APP_ID)):
            if candidate.is_file():
                return candidate
    return None


def prepare_logo(cache_dir: Path, height: int, source: Optional[Path] = None) -> Optional[bytes]:
    """Steam-Logo auf `height` Pixel Höhe verkleinern; das Ergebnis wird zwischengespeichert."""
    source = source or find_steam_logo()
    if source is None:
        return None
    st = source.stat()
    target = cache_dir / ("logo_%d_%d_%d.png" % (height, st.st_size, int(st.st_mtime)))
    if target.is_file():
        try:
            return target.read_bytes()
        except OSError:
            pass
    width, h, pixels = png_decode(source.read_bytes())
    box = alpha_bbox(width, h, pixels)
    bw, bh = box[2] - box[0], box[3] - box[1]
    new_h = min(height, bh)
    new_w = max(1, int(round(bw * new_h / bh)))
    png = png_encode(new_w, new_h, resize_rgba(width, h, pixels, box, new_w, new_h))
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        for old in cache_dir.glob("logo_*.png"):
            old.unlink()
        target.write_bytes(png)
    except OSError:
        pass
    return png


# ---------------------------------------------------------------------------
# Texte
# ---------------------------------------------------------------------------

CLOUD_WARNING_TITLE = "Wichtig: Steam Cloud für BioShock Infinite abschalten"
CLOUD_WARNING_TEXT = (
    "Damit deine Profile nicht überschrieben werden, sollte die Steam-Cloud-Synchronisierung "
    "für BioShock Infinite ausgeschaltet sein.\n\n"
    "So geht's:\n"
    "  1.  Steam öffnen → Bibliothek\n"
    "  2.  Rechtsklick auf „BioShock Infinite“ → Eigenschaften …\n"
    "  3.  Reiter „Allgemein“ → „Steam Cloud-Synchronisierung“\n"
    "       („Spielstände in der Steam Cloud speichern“) ausschalten\n\n"
    "Warum? Dieses Tool tauscht die Spielstände im Save-Ordner aus. Ist die Steam Cloud aktiv, "
    "kann Steam beim nächsten Spielstart ältere Cloud-Stände zurückspielen oder deine "
    "ausgetauschten Profile überschreiben."
)


# ---------------------------------------------------------------------------
# Design: "Columbia 1912" – Nachtblau, Messing und Pergament
# ---------------------------------------------------------------------------

class C:
    """Farbpalette."""
    BG = "#0e131b"
    HEADER = "#111824"
    PANEL = "#141b26"
    CARD = "#1a2230"
    CARD_HOVER = "#212b3b"
    CARD_SELECTED = "#232f41"
    LINE = "#2a3546"
    GOLD = "#c9a24f"
    GOLD_HI = "#e3c47c"
    GOLD_DIM = "#8b7139"
    GOLD_FAINT = "#3d3424"
    TEXT = "#efe6d2"
    MUTED = "#a99f8b"
    FAINT = "#6e695d"
    INK = "#17120a"        # Text auf Gold
    RED = "#d0604e"
    RED_DIM = "#5e2c26"
    RED_BG = "#2a1b1d"
    GREEN = "#93b673"
    AMBER = "#e0a74a"
    AMBER_BG = "#2a2418"
    SKY = "#7fb2cf"


def spaced(text: str) -> str:
    """Gesperrte Großbuchstaben im Stil alter Plakate: „P R O F I L E“."""
    return "   ".join(" ".join(word) for word in text.upper().split())


def _colorref(hex_color: str) -> int:
    r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    return (b << 16) | (g << 8) | r


def style_window(win: tk.Misc) -> None:
    """Dunkle Windows-Titelleiste (Windows 10/11) in der Farbe des Kopfbereichs."""
    if os.name != "nt":
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        dwm = ctypes.windll.dwmapi
        on = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (neue/alte Nummer)
            if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                break
        caption = ctypes.c_int(_colorref(C.HEADER))
        text = ctypes.c_int(_colorref(C.GOLD_HI))
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption))  # nur Windows 11
        dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text), ctypes.sizeof(text))
        rounded = ctypes.c_int(2)  # DWMWCP_ROUND: abgerundete Fensterecken (Windows 11)
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(rounded), ctypes.sizeof(rounded))
    except Exception:
        pass


class Fonts:
    """Schriften mit Rückfallebenen (nicht jede Schrift ist auf jedem PC installiert)."""

    def __init__(self, root: tk.Misc):
        families = {f.lower(): f for f in tkfont.families(root)}

        def pick(*names: str) -> str:
            for name in names:
                if name.lower() in families:
                    return families[name.lower()]
            return "TkDefaultFont"

        display = pick("Copperplate Gothic Bold", "Perpetua Titling MT", "Palatino Linotype", "Georgia", "Times New Roman")
        display_light = pick("Copperplate Gothic Light", "Copperplate Gothic Bold", "Palatino Linotype", "Georgia")
        serif = pick("Palatino Linotype", "Book Antiqua", "Constantia", "Georgia", "Times New Roman")
        sans = pick("Segoe UI", "Tahoma", "Arial")
        sans_semi = pick("Segoe UI Semibold", "Segoe UI", "Tahoma", "Arial")
        mono = pick("Cascadia Mono", "Consolas", "Courier New")

        self.display = tkfont.Font(root, family=display, size=22)
        self.display_small = tkfont.Font(root, family=display_light, size=9)
        self.number = tkfont.Font(root, family=display, size=26)
        self.h1 = tkfont.Font(root, family=serif, size=18, weight="bold")
        self.h2 = tkfont.Font(root, family=serif, size=13, weight="bold")
        self.italic = tkfont.Font(root, family=serif, size=10, slant="italic")
        self.body = tkfont.Font(root, family=sans, size=10)
        self.small = tkfont.Font(root, family=sans, size=9)
        self.caps = tkfont.Font(root, family=sans_semi, size=8)
        self.button = tkfont.Font(root, family=sans_semi, size=10)
        self.start = tkfont.Font(root, family=display, size=13)
        self.mono = tkfont.Font(root, family=mono, size=9)
        self.icon = tkfont.Font(root, family=pick("Segoe UI Symbol", "Segoe UI"), size=20)


_UI = {"scale": 1.0}  # Bildschirmskalierung (DPI); wird vom Hauptfenster gesetzt


def S(value: float) -> int:
    """Pixelmaß an die Bildschirmskalierung anpassen."""
    return max(1, int(round(value * _UI["scale"])))


def _bg_of(widget: tk.Misc, default: str = C.BG) -> str:
    try:
        return str(widget.cget("bg"))
    except (tk.TclError, AttributeError):
        return default


def _rgb(color: str) -> Tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


# -- Abgerundete Flächen ----------------------------------------------------

class CornerPainter:
    """Geglättete Ecken für abgerundete Flächen.

    Tk zeichnet Kurven unter Windows ohne Kantenglättung. Deshalb werden die vier
    Ecken als kleine Bilder berechnet: Jedes Pixel wird 4x4-fach abgetastet und
    Füllung, Rand und Hintergrund nach ihrem Flächenanteil gemischt (gecacht).
    """

    SAMPLES = 4

    def __init__(self, master: tk.Misc):
        self.master = master
        self.cache: Dict[Tuple[int, str, str, str], Dict[str, tk.PhotoImage]] = {}

    def corners(self, r: int, fill: str, border: str, outer: str) -> Dict[str, tk.PhotoImage]:
        key = (r, fill, border, outer)
        if key in self.cache:
            return self.cache[key]
        f, b, o = _rgb(fill), _rgb(border), _rgb(outer)
        n = self.SAMPLES
        total = n * n
        rows = []
        for y in range(r):
            row = []
            for x in range(r):
                inner = ring = 0
                for sy in range(n):
                    dy = r - (y + (sy + 0.5) / n)
                    for sx in range(n):
                        d = math.hypot(r - (x + (sx + 0.5) / n), dy)
                        if d <= r - 1:
                            inner += 1
                        elif d <= r:
                            ring += 1
                wi, wr = inner / total, ring / total
                wo = 1.0 - wi - wr
                row.append("#%02x%02x%02x" % tuple(int(round(f[i] * wi + b[i] * wr + o[i] * wo)) for i in range(3)))
            rows.append(row)
        variants = {"tl": rows, "tr": [row[::-1] for row in rows],
                    "bl": rows[::-1], "br": [row[::-1] for row in rows[::-1]]}
        images = {}
        for name, data in variants.items():
            img = tk.PhotoImage(master=self.master, width=r, height=r)
            img.put(" ".join("{%s}" % " ".join(row) for row in data))
            images[name] = img
        self.cache[key] = images
        return images


_PAINTERS: Dict[str, CornerPainter] = {}


def _painter(widget: tk.Misc) -> CornerPainter:
    root = widget._root()  # type: ignore[attr-defined]
    key = "%s:%d" % (root._w, id(root))
    if key not in _PAINTERS:
        _PAINTERS[key] = CornerPainter(root)
    return _PAINTERS[key]


def round_rect(canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int, r: int, fill: str,
               border: Optional[str] = None, outer: Optional[str] = None, tags: str = "bg") -> None:
    """Zeichnet ein abgerundetes Rechteck mit geglätteten Ecken auf ein Canvas."""
    border = border or fill
    outer = outer or _bg_of(canvas)
    w, h = x2 - x1, y2 - y1
    if w < 2 or h < 2:
        return
    r = max(1, min(r, w // 2, h // 2))
    images = _painter(canvas).corners(r, fill, border, outer)
    canvas.create_rectangle(x1, y1, x2, y2, fill=fill, outline="", width=0, tags=tags)
    if border != fill:
        canvas.create_line(x1 + r, y1, x2 - r, y1, fill=border, tags=tags)
        canvas.create_line(x1 + r, y2 - 1, x2 - r, y2 - 1, fill=border, tags=tags)
        canvas.create_line(x1, y1 + r, x1, y2 - r, fill=border, tags=tags)
        canvas.create_line(x2 - 1, y1 + r, x2 - 1, y2 - r, fill=border, tags=tags)
    for name, (x, y) in (("tl", (x1, y1)), ("tr", (x2 - r, y1)), ("bl", (x1, y2 - r)), ("br", (x2 - r, y2 - r))):
        canvas.create_image(x, y, image=images[name], anchor="nw", tags=tags)


def render_emblem(size: int) -> bytes:
    """Das Stern-Emblem des Kopfbereichs als RGBA-PNG (für das Fenstersymbol)."""
    n = 3
    c = size / 2.0
    radius = c - 0.5
    star = []
    for i in range(16):
        angle = -math.pi / 2 + i * math.pi / 8
        rr = radius * (0.62 if i % 2 == 0 else 0.25)
        star.append((c + rr * math.cos(angle), c + rr * math.sin(angle)))
    gold, dark = _rgb(C.GOLD), _rgb(C.HEADER)

    def in_star(x: float, y: float) -> bool:
        inside, j = False, len(star) - 1
        for i in range(len(star)):
            (xi, yi), (xj, yj) = star[i], star[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
        return inside

    pixels = bytearray(size * size * 4)
    o = 0
    for y in range(size):
        for x in range(size):
            acc, cov = [0, 0, 0], 0
            for sy in range(n):
                for sx in range(n):
                    px, py = x + (sx + 0.5) / n, y + (sy + 0.5) / n
                    d = math.hypot(px - c, py - c)
                    if d > radius:
                        continue
                    col = gold if (d > radius * 0.8 or in_star(px, py)) else dark
                    acc[0] += col[0]
                    acc[1] += col[1]
                    acc[2] += col[2]
                    cov += 1
            if cov:
                pixels[o:o + 4] = bytes((acc[0] // cov, acc[1] // cov, acc[2] // cov, cov * 255 // (n * n)))
            o += 4
    return png_encode(size, size, pixels)


def write_icon_file(path) -> None:
    """Windows-Symboldatei (.ico) mit dem Stern-Emblem – nutzt build.bat für das Symbol der .exe."""
    sizes = (16, 24, 32, 48, 64, 128, 256)
    images = [render_emblem(size) for size in sizes]
    offset = 6 + 16 * len(images)
    entries, data = b"", b""
    for size, png in zip(sizes, images):
        # Breite/Höhe 0 bedeutet 256 Pixel; Vista+ erlaubt PNG-Daten direkt in der .ico
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + entries + data)


class HoverButton(tk.Canvas):
    """Abgerundeter Button im Columbia-Stil (auf einem Canvas gezeichnet)."""

    STYLES = {
        # Füllung, Text, Füllung bei Hover, Rand
        "primary": (C.GOLD, C.INK, C.GOLD_HI, C.GOLD),
        "secondary": (C.CARD, C.TEXT, C.CARD_HOVER, C.GOLD_DIM),
        "danger": (C.CARD, C.RED, C.RED_BG, C.RED_DIM),
        "ghost": (None, C.MUTED, C.CARD_HOVER, None),   # None = Hintergrundfarbe der Umgebung
        "field": (C.CARD, C.TEXT, C.CARD_HOVER, C.LINE),
    }
    RIGHT_PAD = 0  # zusätzlicher Platz rechts (z. B. für einen Pfeil)

    def __init__(self, parent: tk.Misc, text: str, command: Optional[Callable[[], None]] = None,
                 style: str = "secondary", font=None, padx: int = 14, pady: int = 7,
                 anchor: str = "center", radius: int = 8):
        self._outer = _bg_of(parent)
        super().__init__(parent, bg=self._outer, highlightthickness=0, bd=0, cursor="hand2", takefocus=1)
        self._font = font if font is not None else tkfont.nametofont("TkDefaultFont")
        self._text, self._anchor, self._style = text, anchor, style
        self._padx, self._pady, self._radius = padx, pady, S(radius)
        self._command = command
        self._enabled, self._hover, self._focus = True, False, False
        self._request_size()
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Enter>", lambda _e: self._set_flag("_hover", True))
        self.bind("<Leave>", lambda _e: self._set_flag("_hover", False))
        self.bind("<FocusIn>", lambda _e: self._set_flag("_focus", True))
        self.bind("<FocusOut>", lambda _e: self._set_flag("_focus", False))
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Return>", lambda _e: self.invoke())
        self.bind("<space>", lambda _e: self.invoke())
        self.redraw()

    # tkinter-ähnliche Schnittstelle: configure(text=…, anchor=…) und cget("text")
    def configure(self, cnf=None, **kw):
        if cnf:
            kw = dict(cnf, **kw)
        changed = False
        for key in ("text", "anchor"):
            if key in kw:
                setattr(self, "_" + key, kw.pop(key))
                changed = True
        result = tk.Canvas.configure(self, **kw) if kw else None
        if changed:
            self._request_size()
            self.redraw()
        return result

    config = configure

    def cget(self, key: str):
        if key == "text":
            return self._text
        return tk.Canvas.cget(self, key)

    def _request_size(self) -> None:
        w = self._font.measure(self._text) + 2 * self._padx + S(self.RIGHT_PAD) + 2
        h = self._font.metrics("linespace") + 2 * self._pady + 2
        tk.Canvas.configure(self, width=w, height=h)

    def _size(self) -> Tuple[int, int]:
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            w, h = int(tk.Canvas.cget(self, "width")), int(tk.Canvas.cget(self, "height"))
        return w, h

    def _colors(self) -> Tuple[str, str, str]:
        fill, fg, hover, border = self.STYLES[self._style]
        fill = fill or self._outer
        border = border or self._outer
        if not self._enabled:
            if self._style == "ghost":
                return self._outer, C.FAINT, self._outer
            return (C.PANEL if self._outer == C.CARD else C.CARD), C.FAINT, C.LINE
        if self._hover:
            fill = hover
        if self._focus:
            border = C.GOLD_HI
        return fill, fg, border

    def _fit(self, text: str, max_width: int) -> str:
        if self._font.measure(text) <= max_width:
            return text
        while text and self._font.measure(text + tr("…")) > max_width:
            text = text[:-1]
        return text + tr("…")

    def redraw(self) -> None:
        self.delete("all")
        w, h = self._size()
        fill, fg, border = self._colors()
        round_rect(self, 0, 0, w, h, self._radius, fill, border, self._outer)
        text = self._fit(self._text, w - 2 * self._padx - S(self.RIGHT_PAD))
        if self._anchor == "w":
            self.create_text(self._padx + 1, h / 2, text=text, font=self._font, fill=fg, anchor="w")
        else:
            self.create_text((w - S(self.RIGHT_PAD)) / 2, h / 2, text=text, font=self._font, fill=fg)

    def _set_flag(self, name: str, value: bool) -> None:
        setattr(self, name, value)
        self.redraw()

    def _on_release(self, event: tk.Event) -> None:
        if self.winfo_containing(event.x_root, event.y_root) is self:
            self.invoke()

    def invoke(self) -> None:
        if self._enabled and self._command is not None:
            self._command()

    def set_enabled(self, on: bool) -> None:
        self._enabled = bool(on)
        tk.Canvas.configure(self, cursor="hand2" if self._enabled else "arrow")
        self.redraw()

    def set_style(self, style: str) -> None:
        self._style = style
        self.redraw()

    def set_outer(self, color: str) -> None:
        """Hintergrundfarbe der Umgebung (für die Ecken) ändern."""
        self._outer = color
        tk.Canvas.configure(self, bg=color)
        self.redraw()


class Pill(tk.Canvas):
    """Kleine abgerundete Plakette, z. B. „AKTIV“."""

    def __init__(self, parent: tk.Misc, text: str, font, fill: str = C.GOLD, fg: str = C.INK,
                 padx: int = 8, pady: int = 2):
        self._outer = _bg_of(parent)
        super().__init__(parent, bg=self._outer, highlightthickness=0, bd=0)
        self._text, self._font, self._fill, self._fg = text, font, fill, fg
        self._pw = font.measure(text) + 2 * S(padx)
        self._ph = font.metrics("linespace") + 2 * S(pady)
        tk.Canvas.configure(self, width=self._pw, height=self._ph)
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        round_rect(self, 0, 0, self._pw, self._ph, self._ph // 2, self._fill, self._fill, self._outer)
        self.create_text(self._pw / 2, self._ph / 2, text=self._text, font=self._font, fill=self._fg)

    def set_outer(self, color: str) -> None:
        self._outer = color
        tk.Canvas.configure(self, bg=color)
        self.redraw()


class RoundedCard(tk.Frame):
    """Abgerundete Karte. Kinder werden ganz normal in die Karte gepackt.

    Hinter den Kindern liegt ein Canvas über die ganze Fläche, das die abgerundete
    Form zeichnet; der Innenabstand (padx/pady) lässt die Ecken frei.
    """

    def __init__(self, parent: tk.Misc, fill: str = C.CARD, border: str = C.LINE, radius: int = 12,
                 padx: Optional[int] = None, pady: Optional[int] = None,
                 decorate: Optional[Callable[[tk.Canvas, int, int], None]] = None):
        self.outer = _bg_of(parent)
        px = S(16) if padx is None else padx
        py = S(14) if pady is None else pady
        super().__init__(parent, bg=self.outer, padx=px, pady=py)
        self.fill, self.border, self.radius, self.decorate = fill, border, S(radius), decorate
        self._bgc = tk.Canvas(self, bg=self.outer, highlightthickness=0, bd=0)
        # „place“ misst ab der Innenkante (nach dem Innenabstand) – daher um den Abstand nach außen versetzen
        self._bgc.place(x=-px, y=-py, relwidth=1, relheight=1, width=2 * px, height=2 * py)
        tk.Misc.lower(self._bgc)
        self.bind("<Configure>", lambda _e: self.redraw(), add="+")

    def redraw(self) -> None:
        c = self._bgc
        c.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4:
            return
        round_rect(c, 0, 0, w, h, self.radius, self.fill, self.border, self.outer)
        if self.decorate is not None:
            self.decorate(c, w, h)

    def surfaces(self) -> List[tk.Misc]:
        """Alle Kinder, die direkt auf der Kartenfläche liegen (ohne verschachtelte Karten)."""
        result, stack = [], [w for w in self.winfo_children() if w is not self._bgc]
        while stack:
            w = stack.pop()
            result.append(w)
            if not isinstance(w, (HoverButton, Pill, RoundedCard, tk.Canvas)):
                stack.extend(w.winfo_children())
        return result

    def set_colors(self, fill: Optional[str] = None, border: Optional[str] = None) -> None:
        old = self.fill
        if fill and fill != old:
            for w in self.surfaces():
                if isinstance(w, (HoverButton, Pill, RoundedCard)):
                    w.set_outer(fill)
                else:
                    try:
                        if _bg_of(w, "") == old:
                            w.configure(bg=fill)
                    except tk.TclError:
                        pass
            self.fill = fill
        if border:
            self.border = border
        self.redraw()

    def set_outer(self, color: str) -> None:
        self.outer = color
        tk.Frame.configure(self, bg=color)
        self._bgc.configure(bg=color)
        self.redraw()


def card(parent: tk.Misc, padx: Optional[int] = None, pady: Optional[int] = None,
         border: str = C.LINE, fill: str = C.CARD) -> RoundedCard:
    """Standard-Karte mit dünnem Rand und abgerundeten Ecken."""
    return RoundedCard(parent, fill=fill, border=border, padx=padx, pady=pady)


def caps_label(parent: tk.Misc, text: str, fonts: Fonts, fg: str = C.GOLD, bg: str = C.CARD) -> tk.Label:
    return tk.Label(parent, text=spaced(text), font=fonts.caps, fg=fg, bg=bg, anchor="w")


def gold_rule(parent: tk.Misc, bg: str = C.CARD, color: str = C.GOLD_FAINT) -> tk.Frame:
    return tk.Frame(parent, bg=color, height=1)


class SlimScrollbar(tk.Canvas):
    """Schmale Scrollleiste mit abgerundetem Griff."""

    def __init__(self, parent: tk.Misc, command: Callable[..., object], bg: Optional[str] = None):
        self._bg = bg or _bg_of(parent)
        super().__init__(parent, width=S(10), bg=self._bg, highlightthickness=0, bd=0)
        self._command = command
        self._first, self._last = 0.0, 1.0
        self._drag: Optional[Tuple[int, float]] = None
        self._hover = False
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)

    def set(self, first, last) -> None:
        self._first, self._last = float(first), float(last)
        self.redraw()

    def _thumb(self) -> Tuple[int, int]:
        h = max(1, self.winfo_height())
        y1, y2 = self._first * h, self._last * h
        minimum = S(28)
        if y2 - y1 < minimum:
            y1 = min(y1, h - minimum)
            y2 = y1 + minimum
        return int(y1), int(y2)

    def redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if h < 4 or self._last - self._first >= 1.0:
            return
        y1, y2 = self._thumb()
        color = C.GOLD_DIM if (self._hover or self._drag) else C.LINE
        x1, x2 = S(2), w - S(2)
        round_rect(self, x1, y1, x2, y2, (x2 - x1) // 2, color, color, self._bg)

    def _set_hover(self, on: bool) -> None:
        self._hover = on
        self.redraw()

    def _press(self, event: tk.Event) -> None:
        y1, y2 = self._thumb()
        if y1 <= event.y <= y2:
            self._drag = (event.y, self._first)
        else:
            self._command("scroll", -1 if event.y < y1 else 1, "pages")

    def _motion(self, event: tk.Event) -> None:
        if self._drag is not None:
            y0, first0 = self._drag
            self._command("moveto", first0 + (event.y - y0) / max(1, self.winfo_height()))

    def _release(self, _event: tk.Event) -> None:
        self._drag = None
        self.redraw()


class ScrollFrame(tk.Frame):
    """Senkrecht scrollbarer Bereich; die Scrollleiste erscheint nur bei Bedarf."""

    def __init__(self, parent: tk.Misc, bg: str = C.PANEL):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.vsb = SlimScrollbar(self, command=self.canvas.yview, bg=bg)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.inner.bind("<Configure>", lambda _e: self._update())
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.bind("<Enter>", lambda _e: self.canvas.bind_all("<MouseWheel>", self._on_wheel))
        self.bind("<Leave>", lambda _e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._win, width=event.width)
        self._update()

    def _update(self) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        need = self.inner.winfo_reqheight() > self.canvas.winfo_height() > 1
        if need and not self.vsb.winfo_ismapped():
            self.vsb.grid(row=0, column=1, sticky="ns", padx=(S(4), 0))
        elif not need and self.vsb.winfo_ismapped():
            self.vsb.grid_remove()
            self.canvas.yview_moveto(0)

    def _on_wheel(self, event: tk.Event) -> None:
        if self.vsb.winfo_ismapped():
            self.canvas.yview_scroll(int(-event.delta / 120), "units")


class Dropdown(HoverButton):
    """Auswahlfeld mit abgerundeter Aufklappliste (ersetzt ttk.Combobox)."""

    RIGHT_PAD = 22
    MAX_ROWS = 12

    def __init__(self, parent: tk.Misc, font, on_select: Callable[[str], None], min_chars: int = 20):
        # vor super().__init__ setzen – der Konstruktor zeichnet bereits
        self._min_chars = min_chars
        self.values: List[str] = []
        self.value = ""
        self._on_select = on_select
        self._popup: Optional[tk.Toplevel] = None
        super().__init__(parent, tr("–"), command=self._open, style="field", font=font, padx=S(12), pady=S(6),
                         anchor="w", radius=8)

    def _request_size(self) -> None:
        w = self._font.measure("0") * self._min_chars + 2 * self._padx + S(self.RIGHT_PAD)
        h = self._font.metrics("linespace") + 2 * self._pady + 2
        tk.Canvas.configure(self, width=w, height=h)

    def redraw(self) -> None:
        super().redraw()
        w, h = self._size()
        self.create_text(w - S(14), h / 2, text="▾", font=self._font,
                         fill=C.GOLD if (self._enabled and self.values) else C.FAINT)

    def set_values(self, values: Sequence[str]) -> None:
        self.values = list(values)
        self.redraw()

    def set(self, value: str) -> None:
        self.value = value
        self.configure(text=value or tr("–"))

    def get(self) -> str:
        return self.value

    def _open(self) -> None:
        if not self.values or self._popup is not None:
            return
        top = tk.Toplevel(self)
        top.withdraw()
        top.overrideredirect(True)
        top.configure(bg=C.GOLD_DIM)
        lb = tk.Listbox(top, bg=C.CARD, fg=C.TEXT, selectbackground=C.GOLD_DIM, selectforeground=C.TEXT,
                        activestyle="none", highlightthickness=0, bd=0, relief="flat", font=self._font,
                        height=min(len(self.values), self.MAX_ROWS), exportselection=False)
        for value in self.values:
            lb.insert("end", "  " + value)
        lb.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        if len(self.values) > self.MAX_ROWS:
            sb = SlimScrollbar(top, command=lb.yview, bg=C.CARD)
            lb.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y", pady=1, padx=(0, 1))
        if self.value in self.values:
            idx = self.values.index(self.value)
            lb.selection_set(idx)
            lb.activate(idx)
            lb.see(idx)
        top.update_idletasks()
        w, h = self.winfo_width(), top.winfo_reqheight()
        x, y = self.winfo_rootx(), self.winfo_rooty() + self.winfo_height() + S(4)
        if y + h > self.winfo_screenheight() - S(48):
            y = self.winfo_rooty() - h - S(4)
        top.geometry("%dx%d+%d+%d" % (w, h, x, y))
        top.deiconify()
        style_window(top)
        top.lift()
        lb.focus_set()
        top.grab_set()
        self._popup = top
        lb.bind("<ButtonRelease-1>", lambda e: self._choose(lb.nearest(e.y)))
        lb.bind("<Return>", lambda _e: self._choose(lb.index("active")))
        lb.bind("<Escape>", lambda _e: self._close())
        lb.bind("<FocusOut>", lambda _e: self.after(80, self._close_if_unfocused))
        top.bind("<Button-1>", self._click_outside)

    def _click_outside(self, event: tk.Event) -> None:
        top = self._popup
        if top is None:
            return
        inside = (top.winfo_rootx() <= event.x_root < top.winfo_rootx() + top.winfo_width()
                  and top.winfo_rooty() <= event.y_root < top.winfo_rooty() + top.winfo_height())
        if not inside:
            self._close()

    def _close_if_unfocused(self) -> None:
        if self._popup is not None and self.focus_get() is None:
            self._close()

    def _choose(self, index: int) -> None:
        if not 0 <= index < len(self.values):
            return
        value = self.values[index]
        self._close()
        if value != self.value:
            self.set(value)
            self._on_select(value)

    def _close(self) -> None:
        top, self._popup = self._popup, None
        if top is not None:
            try:
                top.grab_release()
                top.destroy()
            except tk.TclError:
                pass


class Header(tk.Canvas):
    """Kopfbereich: BioShock-Infinite-Logo (aus dem Steam-Cache) bzw. Emblem, Titel und Spielstatus."""

    def __init__(self, parent: tk.Misc, fonts: Fonts, scale: float):
        super().__init__(parent, height=int(112 * scale), bg=C.HEADER, highlightthickness=0, bd=0)
        self.fonts = fonts
        self.s = scale
        self.logo: Optional[tk.PhotoImage] = None
        self.state_text = tr("Spielstatus wird geprüft …")
        self.state_color = C.FAINT
        self.bind("<Configure>", lambda _e: self.redraw())

    def logo_height(self) -> int:
        return int(86 * self.s)

    def set_logo(self, image: tk.PhotoImage) -> None:
        self.logo = image
        self.redraw()

    def set_game_state(self, text: str, color: str) -> None:
        self.state_text, self.state_color = text, color
        self.redraw()

    def _emblem(self, cx: float, cy: float) -> None:
        s = self.s
        for i in range(36):
            a = i * math.pi * 2 / 36
            r1, r2 = 34 * s, (70 if i % 2 == 0 else 54) * s
            self.create_line(cx + r1 * math.cos(a), cy + r1 * math.sin(a), cx + r2 * math.cos(a), cy + r2 * math.sin(a),
                             fill=C.GOLD_FAINT if i % 2 else C.GOLD_DIM, width=1)
        R = 30 * s
        self.create_oval(cx - R, cy - R, cx + R, cy + R, outline=C.GOLD, width=2, fill=C.HEADER)
        self.create_oval(cx - R + 5 * s, cy - R + 5 * s, cx + R - 5 * s, cy + R - 5 * s, outline=C.GOLD_DIM, width=1)
        star = []
        for i in range(16):
            a = -math.pi / 2 + i * math.pi / 8
            r = (17 if i % 2 == 0 else 6.5) * s
            star += [cx + r * math.cos(a), cy + r * math.sin(a)]
        self.create_polygon(star, fill=C.GOLD, outline="")
        self.create_oval(cx - 3 * s, cy - 3 * s, cx + 3 * s, cy + 3 * s, fill=C.HEADER, outline="")

    def redraw(self) -> None:
        self.delete("all")
        w, h, s = self.winfo_width(), self.winfo_height(), self.s
        if w < 10:
            return
        cy = (h - 6) // 2

        if self.logo is not None:
            # Dezenter Strahlenkranz hinter dem Logo, dann Logo, Trennlinie und Titel
            lx = int(22 * s)
            lw = self.logo.width()
            cx = lx + lw / 2
            for i in range(28):
                a = i * math.pi * 2 / 28
                r1, r2 = lw * 0.42, lw * (0.62 if i % 2 == 0 else 0.53)
                self.create_line(cx + r1 * math.cos(a), cy + r1 * 0.55 * math.sin(a),
                                 cx + r2 * math.cos(a), cy + r2 * 0.55 * math.sin(a), fill=C.GOLD_FAINT)
            self.create_image(lx, cy, image=self.logo, anchor="w")
            x = lx + lw + int(26 * s)
            self.create_line(x, cy - 30 * s, x, cy + 30 * s, fill=C.GOLD_DIM)
            self.create_line(x + 3, cy - 22 * s, x + 3, cy + 22 * s, fill=C.GOLD_FAINT)
            x += int(22 * s)
            self.create_text(x, cy - 10 * s, text=tr("PROFIL-ARCHIV"), font=self.fonts.display, fill=C.TEXT, anchor="w")
            self.create_text(x + 2, cy + 20 * s, text=tr("Spielstände sicher verwahrt  ·  Columbia, anno 1912"),
                             font=self.fonts.italic, fill=C.MUTED, anchor="w")
        else:
            self._emblem(64 * s, cy)
            x = int(150 * s)
            self.create_text(x, cy - 30 * s, text=spaced("BioShock Infinite"), font=self.fonts.display_small,
                             fill=C.GOLD, anchor="w")
            self.create_text(x, cy + 2 * s, text=tr("PROFIL-ARCHIV"), font=self.fonts.display, fill=C.TEXT, anchor="w")
            self.create_text(x + 2, cy + 31 * s, text=tr("Spielstände sicher verwahrt  ·  Columbia, anno 1912"),
                             font=self.fonts.italic, fill=C.MUTED, anchor="w")

        # Doppellinie mit Raute am unteren Rand
        self.create_line(0, h - 5, w, h - 5, fill=C.GOLD_FAINT)
        self.create_line(0, h - 2, w, h - 2, fill=C.GOLD_DIM)
        mx, d = w // 2, 5 * s
        self.create_polygon(mx, h - 3 - d, mx + d, h - 3, mx, h - 3 + d, mx - d, h - 3, fill=C.GOLD, outline=C.HEADER)

        # Status-Plakette rechts
        tw = self.fonts.small.measure(self.state_text)
        pw, ph = tw + int(42 * s), int(30 * s)
        x2, y1 = w - int(22 * s), cy - ph // 2
        round_rect(self, x2 - pw, y1, x2, y1 + ph, ph // 2, C.CARD, C.LINE, C.HEADER, tags="pill")
        self.create_oval(x2 - pw + 14 * s, y1 + ph / 2 - 4 * s, x2 - pw + 22 * s, y1 + ph / 2 + 4 * s,
                         fill=self.state_color, outline="")
        self.create_text(x2 - pw + 30 * s, y1 + ph / 2, text=self.state_text, font=self.fonts.small,
                         fill=C.TEXT, anchor="w")


class Dialog(tk.Toplevel):
    """Modaler Dialog im Columbia-Stil (Meldung, Rückfrage oder Texteingabe)."""

    ICONS = {"info": ("ℹ", C.SKY), "ok": ("✔", C.GREEN), "warn": ("⚠", C.AMBER),
             "error": ("✖", C.RED), "question": ("?", C.GOLD)}

    def __init__(self, app: "App", title: str, message: str, kind: str = "info",
                 buttons: Sequence[Tuple[str, str, object]] = (("OK", "primary", True),),
                 entry: Optional[str] = None, cancel_value: object = None):
        super().__init__(app.root)
        self.withdraw()
        self.result: object = cancel_value
        self._cancel_value = cancel_value
        self._entry: Optional[tk.Entry] = None
        f = app.fonts
        self.title(title)
        self.configure(bg=C.PANEL)
        self.resizable(False, False)
        self.transient(app.root)

        outer = tk.Frame(self, bg=C.PANEL, padx=S(24), pady=S(20))
        outer.pack(fill="both", expand=True)
        top = tk.Frame(outer, bg=C.PANEL)
        top.pack(fill="x")
        glyph, color = self.ICONS.get(kind, self.ICONS["info"])
        tk.Label(top, text=glyph, font=f.icon, fg=color, bg=C.PANEL).pack(side="left", padx=(0, S(12)))
        tk.Label(top, text=title, font=f.h2, fg=C.TEXT, bg=C.PANEL, anchor="w",
                 wraplength=S(420), justify="left").pack(side="left", fill="x")
        gold_rule(outer, color=C.GOLD_FAINT).pack(fill="x", pady=(S(12), S(12)))
        tk.Label(outer, text=message, font=f.body, fg=C.TEXT, bg=C.PANEL, justify="left",
                 wraplength=S(470), anchor="w").pack(fill="x")

        if entry is not None:
            box = RoundedCard(outer, fill=C.CARD, border=C.LINE, radius=8, padx=S(10), pady=S(7))
            box.pack(fill="x", pady=(S(14), 0))
            self._entry = tk.Entry(box, font=f.body, bg=C.CARD, fg=C.TEXT, insertbackground=C.GOLD,
                                   relief="flat", bd=0, highlightthickness=0,
                                   selectbackground=C.GOLD_DIM, selectforeground=C.TEXT)
            self._entry.pack(fill="x")
            self._entry.insert(0, entry)
            self._entry.select_range(0, "end")
            self._entry.bind("<FocusIn>", lambda _e: box.set_colors(border=C.GOLD))
            self._entry.bind("<FocusOut>", lambda _e: box.set_colors(border=C.LINE))

        row = tk.Frame(outer, bg=C.PANEL)
        row.pack(fill="x", pady=(S(20), 0))
        default = None
        for label, style, value in reversed(list(buttons)):
            btn = HoverButton(row, label, command=lambda v=value: self._finish(v), style=style, font=f.button)
            btn.pack(side="right", padx=(S(8), 0))
            if style in ("primary", "danger") and default is None:
                default = value
        self._default = default if default is not None else buttons[0][2]

        self.protocol("WM_DELETE_WINDOW", lambda: self._finish(self._cancel_value))
        self.bind("<Escape>", lambda _e: self._finish(self._cancel_value))
        self.bind("<Return>", lambda _e: self._finish(self._default))

        style_window(self)
        self.update_idletasks()
        parent = app.root
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_reqwidth()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_reqheight()) // 3)
        self.geometry("+%d+%d" % (x, y))
        self.deiconify()
        self.grab_set()
        (self._entry or self).focus_set()

    def _finish(self, value: object) -> None:
        if self._entry is not None:
            self.result = self._entry.get() if value is True else None
        else:
            self.result = value
        self.grab_release()
        self.destroy()

    def show(self) -> object:
        self.wait_window()
        return self.result


class ProfileCard(RoundedCard):
    """Eine Profilkarte in der Liste (Klick = auswählen, Doppelklick = wechseln)."""

    def __init__(self, parent: tk.Misc, fonts: Fonts, scale: float, name: str, meta: str, detail: str,
                 active: bool, selected: bool, on_select: Callable[[str], None], on_activate: Callable[[str], None]):
        self.name, self.active, self.selected = name, active, selected
        super().__init__(parent, fill=C.CARD_SELECTED if selected else C.CARD,
                         border=C.GOLD if selected else C.LINE, padx=S(22), pady=S(12), decorate=self._accent)
        self.configure(cursor="hand2")
        top = tk.Frame(self, bg=self.fill)
        top.pack(fill="x")
        tk.Label(top, text=name, font=fonts.h2, fg=C.TEXT, bg=self.fill, anchor="w").pack(side="left")
        if active:
            Pill(top, spaced(tr("aktiv")), fonts.caps).pack(side="right")
        tk.Label(self, text=meta, font=fonts.small, fg=C.MUTED, bg=self.fill, anchor="w").pack(fill="x", pady=(2, 0))
        if detail:
            tk.Label(self, text=detail, font=fonts.small, fg=C.GOLD_DIM if active else C.FAINT,
                     bg=self.fill, anchor="w").pack(fill="x")
        for widget in [self, self._bgc] + self.surfaces():
            widget.bind("<Button-1>", lambda _e: on_select(name))
            widget.bind("<Double-1>", lambda _e: on_activate(name))
            widget.bind("<Enter>", lambda _e: self._hover(True), add="+")
            widget.bind("<Leave>", lambda _e: self._hover(False), add="+")

    def _accent(self, canvas: tk.Canvas, _w: int, h: int) -> None:
        if self.active:  # goldener Akzent links für das aktive Profil
            round_rect(canvas, S(9), S(14), S(13), h - S(14), S(2), C.GOLD, C.GOLD, self.fill)

    def _hover(self, on: bool) -> None:
        if not self.selected:
            self.set_colors(fill=C.CARD_HOVER if on else C.CARD)

    def set_selected(self, selected: bool) -> None:
        self.selected = selected
        self.set_colors(fill=C.CARD_SELECTED if selected else C.CARD, border=C.GOLD if selected else C.LINE)


# ---------------------------------------------------------------------------
# Spielwerte-Editor (Infusionen, Silberadler, Dietriche)
# ---------------------------------------------------------------------------

STAT_COLORS = {"health": "#d9604f", "shield": "#7fb2cf", "salts": "#a291e8"}
STAT_EFFECTS = {"health": "je Stufe ca. +15 % Max. Leben", "shield": "je Stufe ca. +30 % Schild",
                "salts": "je Stufe ca. +15 Salze"}  # wird bei der Anzeige übersetzt
RESOURCE_COLORS = {"money": "#d3dae3", "lockpicks": "#c98f5a", "salts_current": "#a291e8"}


def format_number(value: int) -> str:
    """Tausenderpunkte (Deutsch) bzw. -kommas (Englisch)."""
    text = "{:,}".format(value)
    return text.replace(",", ".") if current_language() == "de" else text


class PipBar(tk.Canvas):
    """Zehn Rauten für die Stufen 1–10; ein Klick auf eine Raute setzt die Stufe."""

    def __init__(self, parent: tk.Misc, color: str, scale: float, on_pick: Callable[[int], None]):
        self._pip = int(17 * scale)
        self.gap = int(6 * scale)
        width = INFUSION_MAX_LEVEL * (self._pip + self.gap)
        super().__init__(parent, width=width, height=self._pip + 4, bg=C.CARD, highlightthickness=0, bd=0)
        self.color = color
        self.enabled = False
        self._on_pick = on_pick
        self.bind("<Button-1>", self._click)

    def draw(self, level: int, enabled: bool) -> None:
        self.enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self.delete("all")
        r = self._pip / 2
        for i in range(INFUSION_MAX_LEVEL):
            cx = i * (self._pip + self.gap) + r + 1
            cy = r + 2
            filled = i < level
            self.create_polygon(cx, cy - r, cx + r, cy, cx, cy + r, cx - r, cy,
                                fill=self.color if filled else C.PANEL,
                                outline=self.color if (filled or enabled) else C.LINE, width=1)

    def _click(self, event: tk.Event) -> None:
        if self.enabled:
            idx = int(event.x // (self._pip + self.gap))
            if 0 <= idx < INFUSION_MAX_LEVEL:
                self._on_pick(idx + 1)


class StatCard(RoundedCard):
    """Karte für einen Infusions-Wert (Leben, Schild oder Salze)."""

    def __init__(self, page: "StatsPage", parent: tk.Misc, key: str, label: str):
        app = page.app
        s, f = app.scale, app.fonts
        self.key = key
        self.color = color = STAT_COLORS[key]
        super().__init__(parent, fill=C.CARD, border=C.LINE, padx=S(18), pady=S(16), decorate=self._accent)
        tk.Label(self, text=spaced(tr(label)), font=f.caps, fg=color, bg=C.CARD, anchor="w").pack(fill="x")
        row = tk.Frame(self, bg=C.CARD)
        row.pack(fill="x", pady=(int(6 * s), int(4 * s)))
        self.number = tk.Label(row, text=tr("–"), font=f.number, fg=C.TEXT, bg=C.CARD)
        self.number.pack(side="left")
        self.of = tk.Label(row, text=" / %d" % INFUSION_MAX_LEVEL, font=f.h2, fg=C.MUTED, bg=C.CARD)
        self.of.pack(side="left", anchor="s", pady=(0, int(6 * s)))
        self.minus = HoverButton(row, "−", lambda: page.change(key, -1), "secondary", font=f.h2, padx=12, pady=0)
        self.plus = HoverButton(row, "+", lambda: page.change(key, +1), "secondary", font=f.h2, padx=11, pady=0)
        self.plus.pack(side="right")
        self.minus.pack(side="right", padx=(0, int(6 * s)))
        self.pips = PipBar(self, color, s, lambda lvl: page.set_level(key, lvl))
        self.pips.pack(anchor="w", pady=(int(4 * s), int(8 * s)))
        self.note = tk.Label(self, text="", font=f.small, fg=C.MUTED, bg=C.CARD, anchor="w", justify="left",
                             wraplength=int(250 * s))
        self.note.pack(fill="x")
        tk.Label(self, text=tr(STAT_EFFECTS[key]), font=f.small, fg=C.FAINT, bg=C.CARD,
                 anchor="w").pack(fill="x", pady=(4, 0))

    def _accent(self, canvas: tk.Canvas, _w: int, _h: int) -> None:
        # farbiger Reiter am oberen Rand in der Farbe des Werts
        round_rect(canvas, S(18), 0, S(62), S(4), S(2), self.color, self.color, self.fill)

    def show(self, original: Optional[int], value: Optional[int], editable: bool) -> None:
        if value is None:
            self.number.configure(text=tr("–"), fg=C.FAINT)
            self.pips.draw(0, False)
            self.minus.set_enabled(False)
            self.plus.set_enabled(False)
            self.note.configure(text=tr("Noch keine Infusion dieses Typs gewählt – nimm im Spiel einmal diese "
                                        "Option, danach lässt sich der Wert hier ändern."), fg=C.MUTED)
            return
        changed = value != original
        over = value > INFUSION_MAX_LEVEL
        self.number.configure(text=str(value), fg=C.AMBER if over else (C.GOLD_HI if changed else C.TEXT))
        self.pips.draw(min(value, INFUSION_MAX_LEVEL), editable)
        self.minus.set_enabled(editable and value > 0)
        self.plus.set_enabled(editable and value < INFUSION_MAX_LEVEL)
        if over:
            note, color = (tr("Über dem Maximum von %d – stammt sehr wahrscheinlich von einem Trainer/Cheat. "
                              "Mit „−“ oder einem Klick auf die Rauten lässt sich der Wert korrigieren.")
                           % INFUSION_MAX_LEVEL, C.AMBER)
        elif value == INFUSION_MAX_LEVEL:
            note, color = tr("Maximum erreicht – diese Option lässt sich bei weiteren Infusionen nicht mehr "
                             "wählen."), C.GOLD
        elif changed:
            note, color = tr("Geändert (vorher %s) – noch nicht gespeichert.") % original, C.GOLD_HI
        else:
            note, color = "", C.MUTED
        self.note.configure(text=note, fg=color)


class ResourceCard(RoundedCard):
    """Karte für Silberadler bzw. Dietriche: Zahl direkt eintippen oder per Schritt-Buttons ändern.

    Mit read_only=True nur Anzeige (aktueller Salz-Vorrat)."""

    def __init__(self, page: "StatsPage", parent: tk.Misc, key: str, label: str, maximum: int,
                 steps: Sequence[int] = (), read_only: bool = False):
        app = page.app
        s, f = app.scale, app.fonts
        self.key, self.page, self.maximum, self.read_only = key, page, maximum, read_only
        self.color = RESOURCE_COLORS[key]
        self._updating = False
        super().__init__(parent, fill=C.CARD, border=C.LINE, padx=S(18), pady=S(16), decorate=self._accent)
        tk.Label(self, text=spaced(tr(label)), font=f.caps, fg=self.color, bg=C.CARD, anchor="w").pack(fill="x")
        row = tk.Frame(self, bg=C.CARD)
        row.pack(fill="x", pady=(int(6 * s), int(6 * s)))
        self.var = tk.StringVar(value=tr("–"))
        if read_only:
            self.entry = None
            self.value_label = tk.Label(row, textvariable=self.var, font=f.number, fg=C.TEXT, bg=C.CARD)
            self.value_label.pack(side="left")
        else:
            vcmd = (self.register(self._validate), "%P")
            box = RoundedCard(row, fill=C.PANEL, border=C.LINE, radius=8, padx=S(10), pady=S(2))
            box.pack(side="left")
            self.box = box
            self.entry = tk.Entry(box, textvariable=self.var, font=f.number, width=len(str(maximum)),
                                  bg=C.PANEL, fg=C.TEXT, insertbackground=C.GOLD, relief="flat", bd=0,
                                  highlightthickness=0, validate="key", validatecommand=vcmd,
                                  disabledbackground=C.PANEL, disabledforeground=C.FAINT,
                                  selectbackground=C.GOLD_DIM, selectforeground=C.TEXT)
            self.entry.pack()
            self.entry.bind("<FocusIn>", lambda _e: box.set_colors(border=C.GOLD))
            self.entry.bind("<FocusOut>", lambda _e: box.set_colors(border=C.LINE))
            self.var.trace_add("write", lambda *_a: self._typed())
        self.buttons: List[HoverButton] = []
        if steps:
            btns = tk.Frame(row, bg=C.CARD)
            btns.pack(side="right")
            for step in steps:
                if step == 0:  # 0 = Taste „Max.“
                    text, command = tr("Max."), (lambda: page.set_resource(key, page.resource_cap(key)))
                else:
                    text = ("+%s" if step > 0 else "−%s") % format_number(abs(step))
                    command = (lambda st=step: page.step_resource(key, st))
                btn = HoverButton(btns, text, command, "secondary", font=f.small, padx=8, pady=3)
                btn.pack(side="left", padx=(S(4), 0))
                self.buttons.append(btn)
        self.note = tk.Label(self, text="", font=f.small, fg=C.MUTED, bg=C.CARD, anchor="w", justify="left",
                             wraplength=int(250 * s))
        self.note.pack(fill="x")
        self.range = tk.Label(self, text="", font=f.small, fg=C.FAINT, bg=C.CARD, anchor="w", justify="left",
                              wraplength=int(250 * s))
        self.range.pack(fill="x", pady=(4, 0))

    def _accent(self, canvas: tk.Canvas, _w: int, _h: int) -> None:
        round_rect(canvas, S(18), 0, S(62), S(4), S(2), self.color, self.color, self.fill)

    def _validate(self, proposed: str) -> bool:
        return proposed == "" or (proposed.isdigit() and len(proposed) <= len(str(self.maximum)))

    def _typed(self) -> None:
        if not self._updating:
            self.page.resource_typed(self.key, self.var.get())

    def show(self, original: Optional[int], value: Optional[int], editable: bool,
             note: str = "", note_color: str = C.MUTED, range_text: str = "") -> None:
        self._updating = True
        try:
            if self.read_only:
                self.var.set(tr("–") if value is None else format_number(value))
            else:
                # Nur überschreiben, wenn sich der Zahlenwert unterscheidet (Tippen nicht stören)
                current = self.var.get()
                same = (value is None and current == "") or (
                    value is not None and current.isdigit() and int(current) == value)
                if not same:
                    self.var.set("" if value is None else str(value))
        finally:
            self._updating = False
        changed = value is not None and value != original
        if self.entry is not None:
            self.entry.configure(state="normal" if (editable and original is not None) else "disabled",
                                 fg=C.GOLD_HI if changed else C.TEXT)
        for btn in self.buttons:
            btn.set_enabled(editable and original is not None)
        self.note.configure(text=note, fg=note_color)
        self.range.configure(text=range_text)


class StatsPage(tk.Frame):
    """Seite „Spielwerte“: Profil und Spielstand wählen, Infusionen, Silberadler und Dietriche ändern."""

    def __init__(self, app: "App", parent: tk.Misc):
        super().__init__(parent, bg=C.BG)
        self.app = app
        s, f = app.scale, app.fonts
        self.save_obj: Optional[BioShockSave] = None
        self.saves: List[SaveInfo] = []
        self.original: Dict[str, Optional[int]] = {}
        self.values: Dict[str, Optional[int]] = {}
        self.res_original: Dict[str, Optional[int]] = {}
        self.res_values: Dict[str, Optional[int]] = {}
        self.load_error: Optional[str] = None

        # Feste Fußzeile der Seite (bleibt sichtbar), darüber scrollbarer Inhalt
        bottom = tk.Frame(self, bg=C.BG)
        bottom.pack(fill="x", side="bottom", pady=(int(10 * s), 0))
        self.btn_save = HoverButton(bottom, "✔   " + tr("Änderungen speichern"), self.save, "primary", font=f.button)
        self.btn_save.pack(side="right")
        self.btn_reset = HoverButton(bottom, tr("Zurücksetzen"), self.reset, "secondary", font=f.button)
        self.btn_reset.pack(side="right", padx=(0, int(8 * s)))
        tk.Label(bottom, text=tr("Experimentell: Das Save-Format ist nicht offiziell dokumentiert. Vor jedem "
                                 "Speichern entsteht ein Backup der Datei – prüfe die Werte nach dem Laden im Spiel."),
                 font=f.small, fg=C.FAINT, bg=C.BG, anchor="w", justify="left",
                 wraplength=int(560 * s)).pack(side="left", fill="x", expand=True)
        scroll = ScrollFrame(self, bg=C.BG)
        scroll.pack(fill="both", expand=True)
        self.scroll = scroll
        area = scroll.inner

        # Auswahl: Profil + Spielstand
        pick = tk.Frame(area, bg=C.BG)
        pick.pack(fill="x")
        tk.Label(pick, text=spaced(tr("Profil")), font=f.caps, fg=C.GOLD, bg=C.BG).grid(row=0, column=0, sticky="w")
        tk.Label(pick, text=spaced(tr("Spielstand")), font=f.caps, fg=C.GOLD, bg=C.BG).grid(
            row=0, column=1, sticky="w", padx=(int(16 * s), 0))
        self.profile_box = Dropdown(pick, f.body, lambda _v: self._on_profile(), min_chars=22)
        self.profile_box.grid(row=1, column=0, sticky="w", pady=(S(4), 0))
        self.save_box = Dropdown(pick, f.body, lambda _v: self._load_selected(), min_chars=40)
        self.save_box.grid(row=1, column=1, sticky="ew", padx=(S(16), 0), pady=(S(4), 0))
        pick.columnconfigure(1, weight=1)
        self.save_hint = tk.Label(area, text="", font=f.small, fg=C.FAINT, bg=C.BG, anchor="w")
        self.save_hint.pack(fill="x", pady=(4, 0))

        # Infusionen: drei Wertekarten
        tk.Label(area, text=spaced(tr("Infusionen")), font=f.caps, fg=C.GOLD, bg=C.BG,
                 anchor="w").pack(fill="x", pady=(int(14 * s), 0))
        cards = tk.Frame(area, bg=C.BG)
        cards.pack(fill="x", pady=(int(6 * s), 0))
        self.cards: Dict[str, StatCard] = {}
        for i, (key, _item, label) in enumerate(INFUSION_STATS):
            c = StatCard(self, cards, key, label)
            c.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else int(12 * s), 0))
            cards.columnconfigure(i, weight=1, uniform="stat")
            self.cards[key] = c
        self.summary = tk.Label(area, text="", font=f.body, fg=C.TEXT, bg=C.BG, anchor="w", justify="left",
                                wraplength=int(980 * s))
        self.summary.pack(fill="x", pady=(int(10 * s), 0))

        # Vorräte: Silberadler, Dietriche, Salze (nur Anzeige)
        tk.Label(area, text=spaced(tr("Vorräte")), font=f.caps, fg=C.GOLD, bg=C.BG,
                 anchor="w").pack(fill="x", pady=(int(16 * s), 0))
        res = tk.Frame(area, bg=C.BG)
        res.pack(fill="x", pady=(int(6 * s), 0))
        self.res_cards = {
            "money": ResourceCard(self, res, "money", tr("Silberadler"), MONEY_MAX, steps=(-100, 100, 1000, 0)),
            "lockpicks": ResourceCard(self, res, "lockpicks", tr("Dietriche"), LOCKPICKS_MAX, steps=(-1, 1, 0)),
            "salts_current": ResourceCard(self, res, "salts_current", tr("Salze im Vorrat"), 9999, read_only=True),
        }
        for i, c in enumerate(self.res_cards.values()):
            c.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else int(12 * s), 0))
            res.columnconfigure(i, weight=1, uniform="res")

        self.warn_box = RoundedCard(area, fill=C.AMBER_BG, border=C.GOLD_FAINT, padx=S(16), pady=S(12))
        self.warn_label = tk.Label(self.warn_box, text="", font=f.body, fg=C.AMBER, bg=C.AMBER_BG, anchor="w",
                                   justify="left", wraplength=int(960 * s))
        self.warn_label.pack(fill="x")

    # -- Daten -----------------------------------------------------------

    def _folder_for(self, profile: str) -> Optional[Path]:
        m = self.app.manager
        if profile == m.active:
            return m.live_dir
        return m.profiles_dir / profile

    def _is_live(self) -> bool:
        return self.profile_box.get() == self.app.manager.active

    def refresh(self) -> None:
        m = self.app.manager
        try:
            names = m.list_profiles()
        except OSError:
            names = []
        self.profile_box.set_values(names)
        if self.profile_box.get() not in names:
            self.profile_box.set(m.active if m.active in names else (names[0] if names else ""))
        self._on_profile(keep_selection=True)

    def _on_profile(self, keep_selection: bool = False) -> None:
        previous = self.save_box.get() if keep_selection else ""
        profile = self.profile_box.get()
        folder = self._folder_for(profile) if profile else None
        self.saves = [i for i in list_saves(folder) if i.is_main_game] if folder else []
        labels = [self._label(i) for i in self.saves]
        self.save_box.set_values(labels)
        if previous in labels:
            self.save_box.set(previous)
        else:
            self.save_box.set(labels[0] if labels else "")
        self._load_selected()

    @staticmethod
    def _label(info: SaveInfo) -> str:
        fmt = "%d.%m.%Y %H:%M" if current_language() == "de" else "%Y-%m-%d %H:%M"
        when = info.timestamp.strftime(fmt) if info.timestamp else "?"
        return "%s  ·  %s  ·  %s" % (info.map_name, info.kind, when)

    def _selected_info(self) -> Optional[SaveInfo]:
        label = self.save_box.get()
        for info in self.saves:
            if self._label(info) == label:
                return info
        return None

    def _load_selected(self) -> None:
        info = self._selected_info()
        self.save_obj, self.load_error = None, None
        self.original, self.values, self.res_original, self.res_values = {}, {}, {}, {}
        if info is None:
            self.load_error = (tr("In diesem Profil gibt es noch keinen Spielstand des Hauptspiels.")
                               if self.profile_box.get() else tr("Noch kein Profil vorhanden."))
        else:
            try:
                self.save_obj = BioShockSave.load(info.path)
                self.original = self.save_obj.infusion_levels()
                self.values = dict(self.original)
                self.res_original = self.save_obj.resources()
                self.res_values = dict(self.res_original)
            except SaveFormatError as exc:
                self.load_error = str(exc)
        newest_sav = next((i for i in self.saves if not i.is_bookmark), None)
        if info is not None and info is newest_sav:
            hint = tr("Neuester Autosave – den lädt das Spiel über „Fortsetzen“.")
        elif info is not None and info.is_bookmark:
            hint = tr("Kapitelanfang – wird über die Kapitelauswahl geladen.")
        elif info is not None:
            hint = tr("Älterer Autosave.")
        else:
            hint = ""
        where = tr("liegt im Save-Ordner des Spiels (aktives Profil)") if self._is_live() else tr("liegt im Profilordner")
        self.save_hint.configure(text=("%s  ·  %s" % (hint, where)) if info else "")
        self._update()

    # -- Bearbeiten: Infusionen ------------------------------------------

    def _editable(self) -> bool:
        if self.save_obj is None:
            return False
        return not (self._is_live() and self.app._game_state is True)

    def change(self, key: str, delta: int) -> None:
        value = self.values.get(key)
        if value is None or not self._editable():
            return
        if value > INFUSION_MAX_LEVEL:  # z. B. 1000 durch einen Trainer: „−“ korrigiert auf das Maximum
            new = INFUSION_MAX_LEVEL if delta < 0 else value
        else:
            new = max(0, min(INFUSION_MAX_LEVEL, value + delta))
        self.values[key] = new
        self._update()

    def set_level(self, key: str, level: int) -> None:
        if not self._editable() or self.values.get(key) is None:
            return
        current = self.values[key]
        if level == current and current is not None and level > 0:
            level -= 1  # erneuter Klick auf die letzte gefüllte Raute senkt um eine Stufe
        self.values[key] = max(0, min(INFUSION_MAX_LEVEL, level))
        self._update()

    # -- Bearbeiten: Silberadler und Dietriche ----------------------------

    @staticmethod
    def resource_cap(key: str) -> int:
        return MONEY_MAX if key == "money" else LOCKPICKS_NORMAL_MAX

    @staticmethod
    def resource_max(key: str) -> int:
        return MONEY_MAX if key == "money" else LOCKPICKS_MAX

    def set_resource(self, key: str, value: int) -> None:
        if not self._editable() or self.res_original.get(key) is None:
            return
        self.res_values[key] = max(0, min(self.resource_max(key), int(value)))
        self._update()

    def step_resource(self, key: str, step: int) -> None:
        current = self.res_values.get(key)
        self.set_resource(key, (current or 0) + step)

    def resource_typed(self, key: str, text: str) -> None:
        if not self._editable() or self.res_original.get(key) is None:
            return
        self.res_values[key] = int(text) if text.isdigit() else None
        self._update()

    def reset(self) -> None:
        self.values = dict(self.original)
        self.res_values = dict(self.res_original)
        self._update()

    # -- Auswertung ------------------------------------------------------

    def _changes(self) -> Dict[str, int]:
        return {k: v for k, v in self.values.items() if v is not None and v != self.original.get(k)}

    def _res_changes(self) -> Dict[str, int]:
        return {k: v for k, v in self.res_values.items()
                if k in ("money", "lockpicks") and v is not None and v != self.res_original.get(k)}

    def _res_invalid(self) -> bool:
        return any(self.res_original.get(k) is not None and self.res_values.get(k) is None
                   for k in ("money", "lockpicks"))

    def _warnings(self) -> List[str]:
        vals = {k: v for k, v in self.values.items() if v is not None}
        warnings = []
        maxed = [tr(label) for key, _i, label in INFUSION_STATS if vals.get(key, 0) >= INFUSION_MAX_LEVEL]
        if len(vals) == 3 and len(maxed) == 3:
            warnings.append(
                tr("Alle drei Werte stehen auf Maximum. Weitere Infusionen lassen sich dann voraussichtlich nicht "
                   "mehr aufnehmen. Am Blue Ribbon (Season-Pass-Infusionen) kann das Spiel dadurch hängen bleiben, "
                   "weil die Kellnerin erst weitergeht, wenn du die Infusionen genommen hast. Empfehlung: Lass einen "
                   "Wert auf %d, solange du noch Infusionen einsammelst.") % (INFUSION_MAX_LEVEL - 1))
        elif maxed:
            warnings.append(tr("%s auf Maximum: Bei der nächsten Infusion ist diese Option gesperrt – du wählst "
                               "dann einen der anderen Werte.") % tr(" und ").join(maxed))
        total = sum(min(v, INFUSION_MAX_LEVEL) for v in vals.values())
        if total > INFUSIONS_MAIN_GAME + INFUSIONS_SEASON_PASS:
            warnings.append(tr("Zusammen %d Stufen – mehr, als das Spiel regulär hergibt (%d im Hauptspiel, %d mit "
                               "Season Pass).") % (total, INFUSIONS_MAIN_GAME,
                                                   INFUSIONS_MAIN_GAME + INFUSIONS_SEASON_PASS))
        if any(v > INFUSION_MAX_LEVEL for v in vals.values()):
            warnings.append(tr("Mindestens ein Wert liegt über %d. So etwas entsteht durch Trainer/Cheats; wie das "
                               "Spiel damit umgeht, ist nicht sicher.") % INFUSION_MAX_LEVEL)
        picks = self.res_values.get("lockpicks")
        if picks is not None and picks > LOCKPICKS_NORMAL_MAX:
            warnings.append(tr("Mehr als %d Dietriche: Das ist über der normalen Obergrenze des Spiels (Trainer-"
                               "Bereich). Das Spiel lädt solche Werte, nach dem Aufheben weiterer Dietriche kann es "
                               "aber auf %d begrenzen.") % (LOCKPICKS_NORMAL_MAX, LOCKPICKS_NORMAL_MAX))
        return warnings

    def _update(self) -> None:
        editable = self._editable()
        for key, card_ in self.cards.items():
            if self.save_obj is None:
                card_.show(None, None, False)
                card_.note.configure(text="")
            else:
                card_.show(self.original.get(key), self.values.get(key), editable)
        self._update_resources(editable)

        if self.load_error:
            self.summary.configure(text=self.load_error, fg=C.MUTED)
        else:
            vals = [v for v in self.values.values() if v is not None]
            total = sum(min(v, INFUSION_MAX_LEVEL) for v in vals)
            self.summary.configure(
                fg=C.TEXT,
                text=tr("Eingesetzte Infusionen: %d   ·   im Hauptspiel gibt es %d, mit Season Pass %d   ·   "
                        "für alle drei Werte auf Maximum wären %d nötig")
                % (total, INFUSIONS_MAIN_GAME, INFUSIONS_MAIN_GAME + INFUSIONS_SEASON_PASS, 3 * INFUSION_MAX_LEVEL))

        warnings = self._warnings() if self.save_obj is not None else []
        if self.save_obj is not None and self._is_live() and self.app._game_state is True:
            warnings.insert(0, tr("BioShock Infinite läuft – Spielstände des aktiven Profils lassen sich erst nach "
                                  "dem Beenden ändern."))
        if warnings:
            self.warn_label.configure(text="\n\n".join("⚠  " + w for w in warnings))
            self.warn_box.pack(fill="x", pady=(int(12 * self.app.scale), 0))
        else:
            self.warn_box.pack_forget()
        has_changes = bool(self._changes() or self._res_changes())
        self.btn_save.set_enabled(editable and has_changes and not self._res_invalid())
        self.btn_reset.set_enabled(has_changes or self._res_invalid())

    def _update_resources(self, editable: bool) -> None:
        if self.save_obj is None:
            for c in self.res_cards.values():
                c.show(None, None, False)
            return
        unknown = tr("In diesem Spielstand nicht sicher erkannt – Bearbeiten nicht möglich.")
        for key in ("money", "lockpicks"):
            card_ = self.res_cards[key]
            original, value = self.res_original.get(key), self.res_values.get(key)
            if original is None:
                card_.show(None, None, False, note=unknown)
                continue
            cap = self.resource_cap(key)
            if value is None:
                note, color = tr("Bitte eine Zahl eingeben."), C.AMBER
            elif key == "lockpicks" and value > LOCKPICKS_NORMAL_MAX:
                note, color = tr("Über der normalen Obergrenze von %d (vermutlich Trainer).") % LOCKPICKS_NORMAL_MAX, C.AMBER
            elif value != original:
                note, color = tr("Geändert (vorher %s) – noch nicht gespeichert.") % format_number(original), C.GOLD_HI
            else:
                note, color = "", C.MUTED
            if key == "money":
                range_text = tr("Erlaubt: 0 – %s") % format_number(MONEY_MAX)
            else:
                range_text = tr("Erlaubt: 0 – %s (normal bis %d)") % (format_number(LOCKPICKS_MAX), cap)
            card_.show(original, value, editable, note=note, note_color=color, range_text=range_text)
        salts = self.res_original.get("salts_current")
        level = self.values.get("salts") or 0
        capacity = 100 + 15 * min(level, INFUSION_MAX_LEVEL)
        self.res_cards["salts_current"].show(
            salts, salts, False,
            note=(tr("Aktueller Vorrat; das Maximum liegt bei etwa %d (100 + 15 je Salz-Infusion).") % capacity)
            if salts is not None else tr("In diesem Spielstand nicht sicher erkannt – Bearbeiten nicht möglich."),
            range_text=tr("Nur zur Info – füllt sich im Spiel von selbst auf."))

    # -- Speichern -------------------------------------------------------

    def save(self) -> None:
        info = self._selected_info()
        changes, res_changes = self._changes(), self._res_changes()
        if info is None or self.save_obj is None or not (changes or res_changes) or self._res_invalid():
            return
        if self._is_live() and not self.app._confirm_game_closed():
            return
        labels = {k: tr(label) for k, _i, label in INFUSION_STATS}
        labels.update({"money": tr("Silberadler"), "lockpicks": tr("Dietriche")})
        lines = ["  •  %s: %s → %s" % (labels[k], self.original[k], v) for k, v in changes.items()]
        lines += ["  •  %s: %s → %s" % (labels[k], format_number(self.res_original[k]), format_number(v))
                  for k, v in res_changes.items()]
        warnings = self._warnings()
        text = (tr("Folgende Werte werden in „%s“ gespeichert:\n\n%s\n\nVorher wird ein Backup der Datei angelegt.")
                % (info.path.name, "\n".join(lines)))
        if warnings:
            text += "\n\n" + "\n\n".join("⚠  " + w for w in warnings)
        if not self.app.ui_confirm(tr("Spielwerte speichern"), text, ok=tr("Speichern"),
                                   kind="warn" if warnings else "question"):
            return
        profile = self.profile_box.get()
        save_obj = self.save_obj
        details = ", ".join(
            ["%s %s→%s" % (labels[k], self.original[k], v) for k, v in changes.items()]
            + ["%s %s→%s" % (labels[k], self.res_original[k], v) for k, v in res_changes.items()])

        def action() -> object:
            data = save_obj.with_changes(infusions=changes, money=res_changes.get("money"),
                                         lockpicks=res_changes.get("lockpicks"))
            return self.app.manager.write_save_file(profile, info.path, data, tr("Spielwerte: ") + details)

        self.app._run(action, lambda b: tr("Spielwerte in „%s“ gespeichert. (Backup: %s)") % (info.path.name, b.name))
        self._load_selected()


# ---------------------------------------------------------------------------
# Hauptfenster
# ---------------------------------------------------------------------------

class App:
    """Hauptfenster: Kopf, Navigation (Profile / Infusionen / Einstellungen), Fußzeile mit Spielstart."""

    def __init__(self, root: tk.Tk, manager: ProfileManager,
                 live_dir_override: Optional[Path] = None, log_path: Optional[Path] = None):
        self.root = root
        self.manager = manager
        self.log_path = log_path
        self._live_override = live_dir_override
        self._game_state: Optional[bool] = None
        self._bg_game_state: Optional[bool] = None
        self._stop_event = threading.Event()
        self.selected: Optional[str] = None
        self.current_page = "profiles"

        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        _UI["scale"] = self.scale
        s = self.scale
        root.title("%s  —  by %s" % (APP_NAME, APP_AUTHOR))
        root.configure(bg=C.BG)
        root.geometry("%dx%d" % (1080 * s, 830 * s))
        root.minsize(int(900 * s), int(640 * s))
        self.fonts = Fonts(root)
        self._icons: List[tk.PhotoImage] = []
        self._logo_result: Optional[Tuple[Optional[bytes]]] = None
        self._logo_img: Optional[tk.PhotoImage] = None
        self._build()
        style_window(root)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(60, self._startup)

    # -- Logo und Fenstersymbol -------------------------------------------

    @property
    def cache_dir(self) -> Path:
        return self.manager.profiles_dir / "_cache"

    def _load_logo(self) -> None:
        """BioShock-Infinite-Logo aus dem Steam-Cache im Hintergrund vorbereiten."""
        height = self.header.logo_height()

        def work() -> None:
            try:
                self._logo_result = (prepare_logo(self.cache_dir, height),)
            except Exception:
                log.info("Logo konnte nicht geladen werden", exc_info=True)
                self._logo_result = (None,)

        threading.Thread(target=work, name="logo", daemon=True).start()
        self.root.after(100, self._poll_logo)

    def _poll_logo(self) -> None:
        if self._stop_event.is_set():
            return
        if self._logo_result is None:
            self.root.after(100, self._poll_logo)
            return
        png = self._logo_result[0]
        if png:
            try:
                self._logo_img = tk.PhotoImage(master=self.root, data=base64.b64encode(png).decode("ascii"))
                self.header.set_logo(self._logo_img)
            except tk.TclError:
                log.info("Logo konnte nicht angezeigt werden", exc_info=True)

    def _set_icon(self) -> None:
        """Fenstersymbol: das Stern-Emblem (statt der Tk-Feder)."""
        try:
            for size in (48, 32, 16):
                path = self.cache_dir / ("emblem_%d.png" % size)
                try:
                    data = path.read_bytes()
                except OSError:
                    data = render_emblem(size)
                    try:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data)
                    except OSError:
                        pass
                self._icons.append(tk.PhotoImage(master=self.root, data=base64.b64encode(data).decode("ascii")))
            self.root.iconphoto(True, *self._icons)
        except (tk.TclError, ValueError):
            log.info("Fenstersymbol konnte nicht gesetzt werden", exc_info=True)

    # -- Aufbau ------------------------------------------------------------

    def _build(self) -> None:
        s = self.scale
        f = self.fonts
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(3, weight=1)

        self.header = Header(self.root, f, s)
        self.header.grid(row=0, column=0, sticky="ew")

        # Navigation
        nav = tk.Frame(self.root, bg=C.BG, padx=int(22 * s))
        nav.grid(row=1, column=0, sticky="ew", pady=(int(10 * s), 0))
        self.nav_items: Dict[str, Tuple[tk.Label, tk.Frame]] = {}
        for key, label in (("profiles", tr("Profile")), ("stats", tr("Spielwerte")), ("settings", tr("Einstellungen"))):
            box = tk.Frame(nav, bg=C.BG)
            box.pack(side="left", padx=(0, int(26 * s)))
            lbl = tk.Label(box, text=spaced(label), font=f.caps, bg=C.BG, fg=C.MUTED, cursor="hand2", pady=int(6 * s))
            lbl.pack()
            bar = tk.Frame(box, bg=C.BG, height=2)
            bar.pack(fill="x")
            lbl.bind("<Button-1>", lambda _e, k=key: self.show_page(k))
            lbl.bind("<Enter>", lambda _e, l=lbl, k=key: l.configure(fg=C.GOLD_HI if k != self.current_page else C.GOLD))
            lbl.bind("<Leave>", lambda _e, l=lbl, k=key: l.configure(fg=C.GOLD if k == self.current_page else C.MUTED))
            self.nav_items[key] = (lbl, bar)
        credit = tk.Label(nav, text=tr("v%s  ·  von %s") % (APP_VERSION, APP_AUTHOR), font=f.small, bg=C.BG,
                          fg=C.FAINT, cursor="hand2")
        credit.pack(side="right")
        credit.bind("<Button-1>", lambda _e: webbrowser.open(APP_URL))
        credit.bind("<Enter>", lambda _e: credit.configure(fg=C.GOLD))
        credit.bind("<Leave>", lambda _e: credit.configure(fg=C.FAINT))
        tk.Frame(self.root, bg=C.LINE, height=1).grid(row=2, column=0, sticky="ew", padx=int(22 * s))

        # Seiten
        self.content = tk.Frame(self.root, bg=C.BG)
        self.content.grid(row=3, column=0, sticky="nsew", padx=int(22 * s), pady=(int(16 * s), int(12 * s)))
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(0, weight=1)
        self.pages: Dict[str, tk.Frame] = {}
        self._build_profiles_page()
        self._build_stats_page()
        self._build_settings_page()

        # Fußzeile
        footer = tk.Frame(self.root, bg=C.HEADER)
        footer.grid(row=4, column=0, sticky="ew")
        tk.Frame(footer, bg=C.GOLD_FAINT, height=1).pack(fill="x", side="top")
        inner = tk.Frame(footer, bg=C.HEADER, padx=int(22 * s), pady=int(12 * s))
        inner.pack(fill="x")
        self.status_label = tk.Label(inner, text=tr("Bereit."), font=f.body, fg=C.MUTED, bg=C.HEADER, anchor="w",
                                     justify="left", wraplength=int(620 * s))
        self.status_label.pack(side="left", fill="x", expand=True)
        self.btn_start = HoverButton(inner, "▶   " + spaced(tr("Spiel starten")), command=self.on_start_game,
                                     style="primary", font=f.start, padx=int(26 * s), pady=int(10 * s))
        self.btn_start.pack(side="right")

        self.show_page("profiles")

    def _page(self, key: str, title: str, subtitle: str) -> tk.Frame:
        page = tk.Frame(self.content, bg=C.BG)
        page.grid(row=0, column=0, sticky="nsew")
        tk.Label(page, text=title, font=self.fonts.h1, fg=C.TEXT, bg=C.BG, anchor="w").pack(fill="x")
        tk.Label(page, text=subtitle, font=self.fonts.italic, fg=C.MUTED, bg=C.BG, anchor="w").pack(fill="x", pady=(0, int(12 * self.scale)))
        self.pages[key] = page
        return page

    def _build_profiles_page(self) -> None:
        s, f = self.scale, self.fonts
        page = self._page("profiles", tr("Deine Profile"),
                          tr("Der Save-Ordner des Spiels entspricht immer dem aktiven Profil."))
        body = tk.Frame(page, bg=C.BG)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        # Liste
        list_card = RoundedCard(body, fill=C.PANEL, border=C.LINE, radius=14, padx=S(12), pady=S(12))
        list_card.grid(row=0, column=0, sticky="nsew")
        self.profile_list = ScrollFrame(list_card, bg=C.PANEL)
        self.profile_list.pack(fill="both", expand=True)

        # Save-Ordner als kompakte Leiste unter der Liste
        folder = card(body, pady=int(10 * s))
        folder.grid(row=1, column=0, sticky="ew", pady=(int(12 * s), 0))
        folder.columnconfigure(1, weight=1)
        caps_label(folder, tr("Save-Ordner"), f).grid(row=0, column=0, sticky="w", padx=(0, int(14 * s)))
        self.live_desc = tk.Label(folder, text=tr("–"), font=f.body, fg=C.TEXT, bg=C.CARD, anchor="w")
        self.live_desc.grid(row=0, column=1, sticky="w")
        btns = tk.Frame(folder, bg=C.CARD)
        btns.grid(row=0, column=2, rowspan=2, sticky="e")
        HoverButton(btns, tr("Ändern …"), self.change_live_dir, "secondary", font=f.small, padx=10, pady=4).pack(side="left")
        HoverButton(btns, tr("Öffnen"), lambda: self._open_folder(self.manager.live_dir), "ghost", font=f.small,
                    padx=10, pady=4).pack(side="left", padx=(6, 0))
        self.live_path = tk.Label(folder, text="", font=f.mono, fg=C.MUTED, bg=C.CARD, anchor="w",
                                  justify="left", wraplength=int(520 * s))
        self.live_path.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self.cloud_hint = tk.Label(folder, text=tr("⚠  Steam-Cloud-Ordner – bitte die Cloud-Synchronisierung für "
                                   "BioShock Infinite ausschalten (siehe Einstellungen)."), font=f.small, fg=C.AMBER,
                                   bg=C.CARD, anchor="w", justify="left")
        self.cloud_hint.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # Seitenleiste
        side = tk.Frame(body, bg=C.BG, width=int(300 * s))
        side.grid(row=0, column=1, rowspan=2, sticky="ns", padx=(int(16 * s), 0))
        side.grid_propagate(False)
        side.columnconfigure(0, weight=1)

        actions = card(side)
        actions.grid(row=0, column=0, sticky="ew")
        caps_label(actions, tr("Ausgewählt"), f).pack(fill="x")
        self.sel_name = tk.Label(actions, text=tr("–"), font=f.h2, fg=C.TEXT, bg=C.CARD, anchor="w")
        self.sel_name.pack(fill="x", pady=(2, int(10 * s)))
        self.btn_switch = HoverButton(actions, tr("⇄   Zu diesem Profil wechseln"), self.on_switch, "primary", font=f.button)
        self.btn_new = HoverButton(actions, tr("✚   Neues Profil …"), self.on_new, "secondary", font=f.button)
        self.btn_save = HoverButton(actions, tr("⤓   Aktuellen Stand sichern"), self.on_save, "secondary", font=f.button)
        self.btn_rename = HoverButton(actions, tr("✎   Umbenennen …"), self.on_rename, "secondary", font=f.button)
        self.btn_delete = HoverButton(actions, tr("✖   Löschen …"), self.on_delete, "danger", font=f.button)
        for i, btn in enumerate((self.btn_switch, self.btn_new, self.btn_save, self.btn_rename, self.btn_delete)):
            btn.configure(anchor="w")
            btn.pack(fill="x", pady=(0 if i == 0 else int(6 * s), 0))
        tk.Label(actions, text=tr("Vor jeder Änderung entsteht automatisch ein Backup."), font=f.small,
                 fg=C.FAINT, bg=C.CARD, anchor="w", wraplength=int(270 * s), justify="left").pack(fill="x", pady=(int(10 * s), 0))

    def _build_stats_page(self) -> None:
        page = self._page("stats", tr("Spielwerte"),
                          tr("Infusionen, Silberadler und Dietriche eines Spielstands ansehen und anpassen."))
        self.infusions = StatsPage(self, page)
        self.infusions.pack(fill="both", expand=True)

    def _build_settings_page(self) -> None:
        s, f = self.scale, self.fonts
        page = self._page("settings", tr("Einstellungen"), tr("Speicherorte, Steam Cloud und Informationen."))
        scroll = ScrollFrame(page, bg=C.BG)
        scroll.pack(fill="both", expand=True)
        self.settings_scroll = scroll
        inner = scroll.inner
        inner.columnconfigure(0, weight=1)

        lang = card(inner)
        lang.grid(row=0, column=0, sticky="ew")
        caps_label(lang, tr("Sprache / Language"), f).pack(fill="x")
        lang_row = tk.Frame(lang, bg=C.CARD)
        lang_row.pack(fill="x", pady=(int(8 * s), 0))
        for code, name in LANGUAGES.items():
            HoverButton(lang_row, name, lambda c=code: self.change_language(c),
                        "primary" if code == current_language() else "secondary", font=f.button,
                        padx=16, pady=5).pack(side="left", padx=(0, int(8 * s)))

        places = card(inner)
        places.grid(row=1, column=0, sticky="ew", pady=(int(14 * s), 0))
        places.columnconfigure(1, weight=1)
        caps_label(places, tr("Speicherorte"), f).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, int(8 * s)))
        self.settings_paths: Dict[str, tk.Label] = {}
        rows = (("live", tr("Save-Ordner (Spiel)")), ("profiles", tr("Profile")), ("backups", tr("Backups")), ("log", tr("Protokoll")))
        for i, (key, label) in enumerate(rows, start=1):
            tk.Label(places, text=label, font=f.body, fg=C.TEXT, bg=C.CARD, anchor="w").grid(
                row=i, column=0, sticky="w", pady=int(4 * s), padx=(0, int(16 * s)))
            val = tk.Label(places, text=tr("–"), font=f.mono, fg=C.MUTED, bg=C.CARD, anchor="w", justify="left",
                           wraplength=int(560 * s))
            val.grid(row=i, column=1, sticky="w")
            self.settings_paths[key] = val
            btns = tk.Frame(places, bg=C.CARD)
            btns.grid(row=i, column=2, sticky="e")
            if key == "live":
                HoverButton(btns, tr("Ändern …"), self.change_live_dir, "secondary", font=f.small, padx=10, pady=3).pack(side="left", padx=(0, 6))
            target = {"live": lambda: self.manager.live_dir, "profiles": lambda: self.manager.profiles_dir,
                      "backups": lambda: self.manager.backups_dir,
                      "log": lambda: self.log_path.parent if self.log_path else None}[key]
            HoverButton(btns, tr("Öffnen"), lambda t=target: self._open_folder(t()), "ghost", font=f.small,
                        padx=10, pady=3).pack(side="left")

        cloud = card(inner, border=C.GOLD_FAINT)
        cloud.grid(row=2, column=0, sticky="ew", pady=(int(14 * s), 0))
        caps_label(cloud, tr("Steam Cloud"), f, fg=C.AMBER).pack(fill="x")
        tk.Label(cloud, text=tr(CLOUD_WARNING_TITLE), font=f.h2, fg=C.TEXT, bg=C.CARD, anchor="w").pack(fill="x", pady=(4, 6))
        tk.Label(cloud, text=tr(CLOUD_WARNING_TEXT), font=f.body, fg=C.MUTED, bg=C.CARD, anchor="w", justify="left",
                 wraplength=int(820 * s)).pack(fill="x")
        row = tk.Frame(cloud, bg=C.CARD)
        row.pack(fill="x", pady=(int(10 * s), 0))
        self.btn_cloud_toggle = HoverButton(row, "", self._toggle_cloud_warning, "secondary", font=f.small, padx=10, pady=4)
        self.btn_cloud_toggle.pack(side="left")

        about = card(inner)
        about.grid(row=3, column=0, sticky="ew", pady=(int(14 * s), 0))
        caps_label(about, tr("Über"), f).pack(fill="x")
        tk.Label(about, text=tr("%s  ·  Version %s") % (APP_NAME, APP_VERSION), font=f.h2, fg=C.TEXT, bg=C.CARD,
                 anchor="w").pack(fill="x", pady=(4, 2))
        tk.Label(about, text=tr("Entwickelt von %s  ·  © %s") % (APP_AUTHOR, APP_YEAR), font=f.body, fg=C.GOLD,
                 bg=C.CARD, anchor="w").pack(fill="x", pady=(0, 6))
        tk.Label(about, text=tr("Inoffizielles Fan-Werkzeug. Verwaltet unabhängige Spielstand-Profile für "
                 "BioShock Infinite (Steam-App %s), bearbeitet Spielwerte, legt vor jeder Änderung ein Backup an "
                 "und startet das Spiel über Steam.") % STEAM_APP_ID, font=f.body, fg=C.MUTED, bg=C.CARD, anchor="w",
                 justify="left", wraplength=int(820 * s)).pack(fill="x")
        links = tk.Frame(about, bg=C.CARD)
        links.pack(fill="x", pady=(int(10 * s), int(10 * s)))
        HoverButton(links, tr("Projektseite auf GitHub"), lambda: webbrowser.open(APP_URL), "secondary",
                    font=f.small, padx=10, pady=4).pack(side="left")
        HoverButton(links, tr("Fehler melden"), lambda: webbrowser.open(APP_URL + "/issues"), "ghost",
                    font=f.small, padx=10, pady=4).pack(side="left", padx=(6, 0))
        tk.Label(about, text=tr("Kein offizielles Produkt und keine Verbindung zu 2K, Irrational Games oder Take-Two "
                 "Interactive. BioShock ist eine Marke von Take-Two Interactive Software, Inc. Das Logo im Kopfbereich "
                 "wird zur Laufzeit aus deiner lokalen Steam-Bibliothek geladen und ist nicht Teil dieses Programms."),
                 font=f.small, fg=C.FAINT, bg=C.CARD, anchor="w", justify="left",
                 wraplength=int(820 * s)).pack(fill="x")

    def change_language(self, code: str) -> None:
        """Sprache umstellen und die Oberfläche in der neuen Sprache neu aufbauen."""
        if code == current_language():
            return
        set_language(code)
        self.manager.config.data["language"] = code
        self._save_config_quietly()
        page = self.current_page
        for child in self.root.winfo_children():
            child.destroy()
        self._build()
        if self._logo_img is not None:
            self.header.set_logo(self._logo_img)
        self._apply_game_state(self._game_state)
        self.refresh()
        self.show_page(page)
        self.set_status(tr("Sprache geändert."), "ok")

    def show_page(self, key: str) -> None:
        self.current_page = key
        for k, (lbl, bar) in self.nav_items.items():
            lbl.configure(fg=C.GOLD if k == key else C.MUTED)
            bar.configure(bg=C.GOLD if k == key else C.BG)
        self.pages[key].tkraise()
        if key == "stats":
            self.refresh_stats()
        elif key == "settings":
            self.refresh_settings()

    # -- Start -------------------------------------------------------------

    def _startup(self) -> None:
        m = self.manager
        self._load_logo()
        self._set_icon()
        if m.config.load_problem:
            self.ui_info(tr("Konfiguration zurückgesetzt"), m.config.load_problem, kind="warn")

        self._init_live_dir()
        if not m.config.hide_cloud_warning:
            self.show_cloud_warning()

        if m.live_dir is not None:
            try:
                created = m.ensure_initial_profile()
            except ProfileError as exc:
                self._show_error(tr("Profil konnte nicht angelegt werden"), str(exc))
            else:
                if created:
                    self.set_status(tr("Dein vorhandener Spielstand wurde als Profil „%s“ übernommen (umbenennbar).")
                                    % created, "ok")

        state = is_game_running()
        self._bg_game_state = state
        self._apply_game_state(state)
        self.refresh()
        threading.Thread(target=self._game_watch_worker, name="game-watch", daemon=True).start()
        self.root.after(1000, self._poll_game_state)

    def _init_live_dir(self) -> None:
        """Save-Ordner bestimmen: Kommandozeile > config.json > automatische Erkennung > Dialog."""
        m = self.manager
        if self._live_override is not None:
            try:
                m.set_live_dir(self._live_override)
            except ProfileError as exc:
                self._show_error(tr("Ungültiger Save-Ordner"), str(exc))
            return

        current = m.live_dir
        if current is not None:
            if not current.is_dir():
                self.set_status(tr("Achtung: Der gespeicherte Save-Ordner existiert (noch) nicht: %s") % current, "warn")
            return

        candidates = find_save_candidates()
        for c in candidates:
            log.info("Save-Ordner-Kandidat: %s (%s, %d Dateien)", c.path, c.description, c.file_count)
        best = choose_save_dir(candidates)
        if best is not None:
            try:
                m.set_live_dir(best.path)
            except ProfileError as exc:
                self._show_error(tr("Ungültiger Save-Ordner"), str(exc))
            else:
                extra = ""
                if len(candidates) > 1:
                    extra = tr("  (%d weitere Ordner gefunden – über „Ändern …“ wählbar)") % (len(candidates) - 1)
                self.set_status(tr("Save-Ordner automatisch erkannt: %s%s") % (best.path, extra), "ok")
                return

        self.ui_info(
            tr("Save-Ordner nicht gefunden"),
            tr("Der Save-Ordner von BioShock Infinite wurde nicht automatisch gefunden.\n\n"
            "Gesucht wurde in:\n"
            "  •  Dokumente\\My Games\\BioShock Infinite\\SaveData\n"
            "  •  Steam\\userdata\\<Steam-ID>\\8870\\remote\n\n"
            "Bitte wähle den Ordner im nächsten Dialog selbst aus. "
            "(Tipp: Starte das Spiel einmal, dann entsteht der Ordner.)"),
            kind="warn",
        )
        self.change_live_dir(first_time=True)

    # -- Anzeige aktualisieren ---------------------------------------------

    def refresh(self) -> None:
        m = self.manager
        live = m.live_dir
        if live is None:
            self.live_desc.configure(text=tr("Kein Save-Ordner festgelegt"))
            self.live_path.configure(text=tr("Bitte über „Ändern …“ den Save-Ordner auswählen."))
            self.cloud_hint.grid_remove()
        else:
            self.live_desc.configure(text=describe_live_dir(live) + ("" if live.is_dir() else tr("  (fehlt noch)")))
            self.live_path.configure(text=str(live))
            if is_steam_cloud_dir(live):
                self.cloud_hint.grid()
            else:
                self.cloud_hint.grid_remove()

        try:
            names = m.list_profiles()
        except OSError as exc:
            names = []
            self.set_status(tr("Profil-Ordner konnte nicht gelesen werden: %s") % exc, "error")
        active = m.active
        if self.selected not in names:
            self.selected = active if active in names else (names[0] if names else None)

        for child in self.profile_list.inner.winfo_children():
            child.destroy()
        self.cards: Dict[str, ProfileCard] = {}
        if not names:
            tk.Label(self.profile_list.inner, text=tr("Noch keine Profile vorhanden."), font=self.fonts.italic,
                     fg=C.MUTED, bg=C.PANEL).pack(pady=int(30 * self.scale))
        for name in names:
            is_active = name == active
            source = live if (is_active and live is not None) else m.profiles_dir / name
            count, size, newest = dir_stats(source)
            meta = tr("%d %s  ·  %s  ·  Stand %s") % (count, tr("Datei") if count == 1 else tr("Dateien"),
                                                 format_size(size), format_time(newest))
            detail = tr("Liegt gerade im Save-Ordner des Spiels") if is_active else ""
            c = ProfileCard(self.profile_list.inner, self.fonts, self.scale, name, meta, detail, is_active,
                            name == self.selected, self._select, self._activate)
            c.pack(fill="x", pady=(0, int(8 * self.scale)))
            self.cards[name] = c
        self.sel_name.configure(text=self.selected or tr("–"))
        self._update_buttons()
        if self.current_page == "settings":
            self.refresh_settings()

    def _select(self, name: str) -> None:
        """Auswahl ändern, ohne die Liste neu aufzubauen (sonst ginge der Doppelklick verloren)."""
        if name == self.selected:
            return
        self.selected = name
        for card_name, c in self.cards.items():
            c.set_selected(card_name == name)
        self.sel_name.configure(text=name)
        self._update_buttons()

    def _activate(self, name: str) -> None:
        self._select(name)
        self.on_switch()

    def _update_buttons(self) -> None:
        has_live = self.manager.live_dir is not None
        running = self._game_state is True
        sel = self.selected
        active = self.manager.active
        self.btn_new.set_enabled(has_live and not running)
        self.btn_switch.set_enabled(has_live and not running and sel is not None and sel != active)
        self.btn_save.set_enabled(has_live and active is not None)
        self.btn_rename.set_enabled(sel is not None)
        self.btn_delete.set_enabled(sel is not None and sel != active)
        self.btn_start.set_enabled(not running)
        self.btn_start.configure(text=spaced(tr("Spiel läuft …")) if running else "▶   " + spaced(tr("Spiel starten")))

    def refresh_settings(self) -> None:
        m = self.manager
        values = {
            "live": str(m.live_dir) if m.live_dir else tr("–"),
            "profiles": str(m.profiles_dir),
            "backups": "%s\n%s" % (m.backups_dir, self._backup_summary()),
            "log": str(self.log_path) if self.log_path else tr("–"),
        }
        for key, label in self.settings_paths.items():
            label.configure(text=values[key])
        hidden = m.config.hide_cloud_warning
        self.btn_cloud_toggle.configure(
            text=tr("Hinweis beim Start wieder anzeigen") if hidden else tr("Hinweis beim Start nicht mehr anzeigen"))

    def _backup_summary(self) -> str:
        d = self.manager.backups_dir
        if not d.is_dir():
            return tr("noch keine Backups")
        dirs = [p for p in d.iterdir() if p.is_dir()]
        _count, size, _newest = dir_stats(d)
        return tr("%d Backups, zusammen %s") % (len(dirs), format_size(size))

    def refresh_stats(self) -> None:
        self.infusions.refresh()

    def set_status(self, text: str, level: str = "info") -> None:
        colors = {"info": C.MUTED, "ok": C.GREEN, "warn": C.AMBER, "error": C.RED}
        self.status_label.configure(text=text.replace("\n", " "), fg=colors.get(level, C.MUTED))

    # -- Dialoge -----------------------------------------------------------

    def ui_info(self, title: str, message: str, kind: str = "info") -> None:
        self.set_status(message.split("\n")[0], {"error": "error", "warn": "warn"}.get(kind, "info"))
        Dialog(self, title, message, kind=kind).show()

    def _show_error(self, title: str, message: str) -> None:
        self.ui_info(title, message, kind="error")

    def ui_confirm(self, title: str, message: str, ok: Optional[str] = None, cancel: Optional[str] = None,
                   danger: bool = False, kind: str = "question") -> bool:
        ok = ok or tr("Ja")
        cancel = cancel or tr("Abbrechen")
        return bool(Dialog(self, title, message, kind=kind, cancel_value=False,
                           buttons=((ok, "danger" if danger else "primary", True), (cancel, "secondary", False))).show())

    def ui_ask_text(self, title: str, message: str, initial: str = "") -> Optional[str]:
        result = Dialog(self, title, message, kind="question", entry=initial, cancel_value=None,
                        buttons=((tr("Übernehmen"), "primary", True), (tr("Abbrechen"), "secondary", False))).show()
        return result if isinstance(result, str) else None

    def show_cloud_warning(self, force: bool = False) -> None:
        if not force and self.manager.config.hide_cloud_warning:
            return
        result = Dialog(self, tr(CLOUD_WARNING_TITLE), tr(CLOUD_WARNING_TEXT), kind="warn", cancel_value="ok",
                        buttons=((tr("Verstanden"), "primary", "ok"), (tr("Nicht mehr anzeigen"), "secondary", "never"))).show()
        if result == "never":
            self.manager.config.hide_cloud_warning = True
            self._save_config_quietly()

    def _toggle_cloud_warning(self) -> None:
        cfg = self.manager.config
        cfg.hide_cloud_warning = not cfg.hide_cloud_warning
        self._save_config_quietly()
        self.refresh_settings()

    def _save_config_quietly(self) -> None:
        try:
            self.manager.config.save()
        except OSError as exc:
            self.set_status(tr("Einstellung konnte nicht gespeichert werden: %s") % exc, "error")

    # -- Spielstatus überwachen --------------------------------------------

    def _game_watch_worker(self) -> None:
        """Hintergrund-Thread: prüft alle paar Sekunden per tasklist (kein Tk-Zugriff hier!)."""
        while not self._stop_event.wait(4.0):
            self._bg_game_state = is_game_running()

    def _poll_game_state(self) -> None:
        if self._stop_event.is_set():
            return
        state = self._bg_game_state
        if state != self._game_state:
            was_running = self._game_state is True
            self._apply_game_state(state)
            if was_running and state is False:
                self.set_status(tr("BioShock Infinite wurde beendet. Wechseln und Neues Profil sind wieder möglich."), "ok")
                if self.current_page == "stats":
                    self.refresh_stats()
            elif state is True:
                self.set_status(tr("BioShock Infinite läuft – Wechseln, Neues Profil und Bearbeiten sind gesperrt."), "warn")
        self.root.after(1000, self._poll_game_state)

    def _apply_game_state(self, state: Optional[bool]) -> None:
        self._game_state = state
        if state is True:
            self.header.set_game_state(tr("Spiel läuft – Austausch gesperrt"), C.RED)
        elif state is False:
            self.header.set_game_state(tr("Spiel ist beendet"), C.GREEN)
        else:
            self.header.set_game_state(tr("Spielstatus unbekannt"), C.FAINT)
        self._update_buttons()
        if hasattr(self, "infusions"):
            self.infusions._update()

    def _confirm_game_closed(self) -> bool:
        """Frische Prüfung direkt vor einer Aktion, die Spielstände verändert."""
        state = is_game_running()
        self._bg_game_state = state
        self._apply_game_state(state)
        if state is True:
            self._show_error(
                tr("Spiel läuft"),
                tr("BioShock Infinite läuft gerade (BioShockInfinite.exe).\n\n"
                "Bitte beende das Spiel vollständig, bevor du Spielstände austauschst oder bearbeitest. "
                "Sonst würde das Spiel die Dateien gleich wieder überschreiben."),
            )
            return False
        if state is None:
            return self.ui_confirm(tr("Prüfung nicht möglich"),
                                   tr("Es konnte nicht geprüft werden, ob BioShock Infinite läuft.\n\n"
                                   "Ist das Spiel sicher beendet?"), kind="warn")
        return True

    # -- Aktionen ----------------------------------------------------------

    def _run(self, action: Callable[[], object], success: Callable[[object], str]) -> None:
        """Führt eine Aktion mit Sanduhr aus und zeigt Fehler verständlich an."""
        self.root.configure(cursor="watch")
        self.root.update_idletasks()
        try:
            result = action()
        except GameRunningError as exc:
            self._apply_game_state(True)
            self._show_error(tr("Spiel läuft"), str(exc))
        except ProfileError as exc:
            self._show_error(tr("Aktion nicht möglich"), str(exc))
        except PermissionError as exc:
            log.exception("Zugriff verweigert")
            self._show_error(
                tr("Zugriff verweigert"),
                tr("Auf eine Datei oder einen Ordner konnte nicht zugegriffen werden:\n%s\n\n"
                "Ist die Datei gerade in einem anderen Programm geöffnet (z. B. Explorer, Virenscanner, Steam)? "
                "Bitte kurz warten und erneut versuchen. Alle bisherigen Stände liegen im Backup-Ordner.") % exc,
            )
        except Exception as exc:  # unerwartet – trotzdem verständlich melden
            log.exception("Unerwarteter Fehler")
            where = (tr("\n\nDetails stehen in der Log-Datei:\n%s") % self.log_path) if self.log_path else ""
            self._show_error(tr("Unerwarteter Fehler"), tr("Es ist ein unerwarteter Fehler aufgetreten:\n%s%s") % (exc, where))
        else:
            self.set_status(success(result), "ok")
        finally:
            self.root.configure(cursor="")
            self.refresh()

    def _require_live(self) -> bool:
        if self.manager.live_dir is None:
            self._show_error(tr("Kein Save-Ordner"), tr("Bitte zuerst über „Ändern …“ den Save-Ordner von BioShock Infinite auswählen."))
            return False
        return True

    def _ask_profile_name(self, title: str, prompt: str, initial: str = "", exclude: Optional[str] = None) -> Optional[str]:
        value: Optional[str] = initial
        while True:
            value = self.ui_ask_text(title, prompt, initial=value or "")
            if value is None:
                return None
            try:
                return self.manager.validate_name(value, exclude=exclude)
            except ProfileError as exc:
                self.ui_info(tr("Ungültiger Name"), str(exc), kind="error")

    def on_new(self) -> None:
        if not self._require_live() or not self._confirm_game_closed():
            return
        active = self.manager.active or DEFAULT_PROFILE_NAME
        name = self._ask_profile_name(
            tr("Neues Profil"),
            tr("Wie soll das neue Profil heißen?\n\n"
            "Vorher wird der aktuelle Stand ins Profil „%s“ gesichert und ein Backup angelegt. "
            "Danach wird der Save-Ordner geleert – BioShock Infinite bietet dann ein neues Spiel an.") % active,
        )
        if name is None:
            return
        self.selected = name
        self._run(lambda: self.manager.new_profile(name),
                  lambda b: tr("Neues Profil „%s“ ist aktiv – starte jetzt ein neues Spiel. (Backup: %s)") % (name, b.name))

    def on_switch(self) -> None:
        target = self.selected
        active = self.manager.active
        if not target:
            return
        if target == active:
            self.set_status(tr("„%s“ ist bereits das aktive Profil.") % target)
            return
        if not self._require_live() or not self._confirm_game_closed():
            return
        if not self.ui_confirm(
            tr("Profil wechseln"),
            tr("Zu Profil „%s“ wechseln?\n\n"
            "Der aktuelle Stand wird vorher ins Profil „%s“ gesichert und zusätzlich ein Backup angelegt.")
            % (target, active or tr("–")), ok=tr("Wechseln"),
        ):
            return
        self._run(lambda: self.manager.switch_to(target),
                  lambda b: tr("Profil „%s“ ist jetzt aktiv. (Backup: %s)") % (target, b.name))

    def on_save(self) -> None:
        if not self._require_live():
            return
        if is_game_running() is True and not self.ui_confirm(
            tr("Spiel läuft"),
            tr("BioShock Infinite läuft noch. Gesichert wird nur, was das Spiel bereits gespeichert hat.\n\n"
            "Trotzdem jetzt sichern?"), ok=tr("Sichern"), kind="warn",
        ):
            return
        active = self.manager.active

        def message(result: object) -> str:
            changed, backup = result  # type: ignore[misc]
            if not changed:
                return tr("Keine Änderungen – das Profil „%s“ ist bereits aktuell.") % active
            suffix = (tr(" (vorheriger Stand im Backup: %s)") % backup.name) if backup else ""
            return tr("Aktueller Stand wurde ins Profil „%s“ gesichert.%s") % (self.manager.active, suffix)

        self._run(self.manager.save_current, message)

    def on_rename(self) -> None:
        name = self.selected
        if not name:
            return
        new = self._ask_profile_name(tr("Profil umbenennen"), tr("Neuer Name für „%s“:") % name, initial=name, exclude=name)
        if new is None or new == name:
            return
        self.selected = new
        self._run(lambda: self.manager.rename_profile(name, new),
                  lambda _r: tr("Profil „%s“ heißt jetzt „%s“.") % (name, new))

    def on_delete(self) -> None:
        name = self.selected
        if not name:
            return
        if name == self.manager.active:
            self.ui_info(tr("Aktives Profil"), tr("Das aktive Profil kann nicht gelöscht werden.\n\n"
                         "Wechsle zuerst zu einem anderen Profil."), kind="warn")
            return
        if not self.ui_confirm(
            tr("Profil löschen"),
            tr("Profil „%s“ wirklich löschen?\n\n"
            "Vorher wird automatisch ein Backup angelegt unter:\n%s") % (name, self.manager.backups_dir),
            ok=tr("Löschen"), danger=True, kind="warn",
        ):
            return
        self._run(lambda: self.manager.delete_profile(name),
                  lambda b: tr("Profil „%s“ wurde gelöscht. (Backup: %s)") % (name, b.name))

    def on_start_game(self) -> None:
        if self._game_state is True:
            self.set_status(tr("BioShock Infinite läuft bereits."), "warn")
            return
        try:
            starter = getattr(os, "startfile", None)
            if starter is not None:
                starter(STEAM_RUN_URL)
            elif not webbrowser.open(STEAM_RUN_URL):
                raise OSError(tr("kein Programm für steam:// registriert"))
        except OSError as exc:
            self._show_error(tr("Start fehlgeschlagen"),
                             tr("Steam konnte nicht gestartet werden (%s).\n\nIst Steam installiert?") % exc)
            return
        self.set_status(tr("Spiel wird über Steam gestartet … (aktives Profil: „%s“)") % (self.manager.active or tr("–")), "ok")

    # -- Save-Ordner wählen ------------------------------------------------

    def change_live_dir(self, first_time: bool = False) -> None:
        m = self.manager
        current = m.live_dir
        if self._game_state is True:
            self._show_error(tr("Spiel läuft"), tr("Bitte beende BioShock Infinite, bevor du den Save-Ordner änderst."))
            return
        initial = current if (current is not None and current.is_dir()) else get_documents_dir()
        chosen = filedialog.askdirectory(parent=self.root, title=tr("Save-Ordner von BioShock Infinite auswählen"),
                                         initialdir=str(initial), mustexist=True)
        if not chosen:
            if first_time:
                self.set_status(tr("Kein Save-Ordner festgelegt – bitte über „Ändern …“ auswählen."), "warn")
            return
        path = Path(os.path.normpath(chosen))
        if current is not None and _norm(path) == _norm(current):
            return
        problem = check_live_dir_safety(path, m.profiles_dir)
        if problem:
            self._show_error(tr("Ungeeigneter Ordner"), problem)
            return
        if not looks_like_save_dir(path) and not self.ui_confirm(
            tr("Ordner prüfen"),
            tr("Dieser Ordner sieht nicht wie ein Save-Ordner von BioShock Infinite aus:\n%s\n\n"
            "Achtung: „Neues Profil“ leert den Save-Ordner (nach einem Backup).\n\nTrotzdem verwenden?") % path,
            ok=tr("Trotzdem verwenden"), danger=True, kind="warn",
        ):
            return
        if m.active and not first_time and not self.ui_confirm(
            tr("Save-Ordner ändern"),
            tr("Ab sofort gilt dieser Ordner als Save-Ordner:\n%s\n\n"
            "Sein Inhalt gehört dann zum aktiven Profil „%s“ und wird beim nächsten Sichern/Wechseln "
            "in dieses Profil übernommen (vorher wird ein Backup angelegt).\n\nFortfahren?") % (path, m.active),
            ok=tr("Übernehmen"),
        ):
            return
        try:
            m.set_live_dir(path)
            created = m.ensure_initial_profile()
        except ProfileError as exc:
            self._show_error(tr("Save-Ordner"), str(exc))
        else:
            msg = tr("Save-Ordner festgelegt: %s") % path
            if created:
                msg += tr("  –  Spielstand als Profil „%s“ übernommen.") % created
            self.set_status(msg, "ok")
        self.refresh()

    # -- Sonstiges ---------------------------------------------------------

    def _open_folder(self, path: Optional[Path]) -> None:
        if path is None:
            self.set_status(tr("Kein Ordner festgelegt."), "warn")
            return
        try:
            path.mkdir(parents=True, exist_ok=True)
            starter = getattr(os, "startfile", None)
            if starter is not None:
                starter(str(path))
            else:
                webbrowser.open(path.as_uri())
        except OSError as exc:
            self._show_error(tr("Ordner öffnen"), tr("Der Ordner konnte nicht geöffnet werden:\n%s\n\n%s") % (path, exc))

    def _on_close(self) -> None:
        self._stop_event.set()
        self.root.destroy()


# ---------------------------------------------------------------------------
# Englische Texte (Schlüssel = deutscher Originaltext, siehe tr())
# ---------------------------------------------------------------------------

EN: Dict[str, str] = {
    '\n\nDetails stehen in der Log-Datei:\n%s':
        '\n\nDetails can be found in the log file:\n%s',
    '  (%d weitere Ordner gefunden – über „Ändern …“ wählbar)':
        '  (%d more folders found – selectable via “Change …”)',
    '  (fehlt noch)':
        '  (does not exist yet)',
    '  live\\  = Inhalt des Save-Ordners (gehörte zum Profil „%s“)':
        '  live\\  = contents of the save folder (belonged to profile “%s”)',
    '  profil_%s\\  = bisheriger Inhalt des Profils „%s“':
        '  profil_%s\\  = previous contents of profile “%s”',
    '  –  Spielstand als Profil „%s“ übernommen.':
        '  –  save game imported as profile “%s”.',
    ' (vorheriger Stand im Backup: %s)':
        ' (previous state in backup: %s)',
    ' und ':
        ' and ',
    '%d %s  ·  %s  ·  Stand %s':
        '%d %s  ·  %s  ·  as of %s',
    '%d Backups, zusammen %s':
        '%d backups, %s in total',
    '%s  ·  Version %s':
        '%s  ·  Version %s',
    '%s auf Maximum: Bei der nächsten Infusion ist diese Option gesperrt – du wählst dann einen der anderen Werte.':
        '%s at maximum: this option is locked at the next Infusion – you will pick one of the other stats instead.',
    '%s kann in diesem Spielstand noch nicht geändert werden, weil du im Spiel noch keine %s-Infusion gewählt hast (der Eintrag existiert noch nicht).':
        '%s cannot be changed in this save yet because you have not chosen a %s Infusion in the game (the entry does not exist yet).',
    '%s: Stufe %d liegt außerhalb von 0–%d.':
        '%s: level %d is outside 0–%d.',
    'Ab sofort gilt dieser Ordner als Save-Ordner:\n%s\n\nSein Inhalt gehört dann zum aktiven Profil „%s“ und wird beim nächsten Sichern/Wechseln in dieses Profil übernommen (vorher wird ein Backup angelegt).\n\nFortfahren?':
        'From now on this folder is used as the save folder:\n%s\n\nIts contents will belong to the active profile “%s” and will be copied into that profile on the next save/switch (a backup is created first).\n\nContinue?',
    'Abbrechen':
        'Cancel',
    'Achtung: Der gespeicherte Save-Ordner existiert (noch) nicht: %s':
        'Note: the configured save folder does not exist (yet): %s',
    'Aktion nicht möglich':
        'Action not possible',
    'Aktion:         %s':
        'Action:         %s',
    'Aktives Profil':
        'Active profile',
    'Aktives Profil: %s':
        'Active profile: %s',
    'Aktueller Stand wurde ins Profil „%s“ gesichert.%s':
        'Current state was saved to profile “%s”.%s',
    'Aktueller Vorrat; das Maximum liegt bei etwa %d (100 + 15 je Salz-Infusion).':
        'Current supply; the maximum is about %d (100 + 15 per Salts Infusion).',
    'Alle drei Werte stehen auf Maximum. Weitere Infusionen lassen sich dann voraussichtlich nicht mehr aufnehmen. Am Blue Ribbon (Season-Pass-Infusionen) kann das Spiel dadurch hängen bleiben, weil die Kellnerin erst weitergeht, wenn du die Infusionen genommen hast. Empfehlung: Lass einen Wert auf %d, solange du noch Infusionen einsammelst.':
        'All three stats are at maximum. You will most likely not be able to pick up further Infusions. At the Blue Ribbon (Season Pass Infusions) the game can get stuck, because the waitress only moves on once you have taken the Infusions. Recommendation: keep one stat at %d while you are still collecting Infusions.',
    'Auf eine Datei oder einen Ordner konnte nicht zugegriffen werden:\n%s\n\nIst die Datei gerade in einem anderen Programm geöffnet (z. B. Explorer, Virenscanner, Steam)? Bitte kurz warten und erneut versuchen. Alle bisherigen Stände liegen im Backup-Ordner.':
        'A file or folder could not be accessed:\n%s\n\nIs the file currently open in another program (e.g. Explorer, antivirus, Steam)? Please wait a moment and try again. All previous states are kept in the backup folder.',
    'Ausgewählt':
        'Selected',
    'Autosave':
        'Autosave',
    'Backup erstellt von %s %s':
        'Backup created by %s %s',
    'Backup erstellt von %s %s\r\nZeitpunkt: %s\r\nAktion:    %s\r\nProfil:    %s\r\nDatei:     %s\r\n\r\nWiederherstellen: Spiel beenden und die Datei aus %s\\ an ihren alten Platz kopieren.\r\n':
        'Backup created by %s %s\r\nTime:      %s\r\nAction:    %s\r\nProfile:   %s\r\nFile:      %s\r\n\r\nRestore: quit the game and copy the file from %s\\ back to its original location.\r\n',
    'Backup fehlgeschlagen: %s\n\nVorgang abgebrochen – es wurde nichts verändert.':
        'Backup failed: %s\n\nOperation cancelled – nothing was changed.',
    'Backup konnte nicht erstellt werden (%s).\n\nVorgang abgebrochen – es wurde nichts verändert.':
        'The backup could not be created (%s).\n\nOperation cancelled – nothing was changed.',
    'Backup konnte nicht erstellt werden (%s). Es wurde nichts verändert.':
        'The backup could not be created (%s). Nothing was changed.',
    'Backup-Kopie weicht ab':
        'backup copy differs',
    'Backups':
        'Backups',
    'Bereit.':
        'Ready.',
    'BioShock Infinite läuft bereits.':
        'BioShock Infinite is already running.',
    'BioShock Infinite läuft gerade (BioShockInfinite.exe).\n\nBitte beende das Spiel vollständig, bevor du Spielstände austauschst oder bearbeitest. Sonst würde das Spiel die Dateien gleich wieder überschreiben.':
        'BioShock Infinite is currently running (BioShockInfinite.exe).\n\nPlease quit the game completely before swapping or editing save games. Otherwise the game would overwrite the files again right away.',
    'BioShock Infinite läuft gerade.\n\nBitte beende das Spiel vollständig und versuche es dann erneut. Sonst würde das Spiel die ausgetauschten Spielstände sofort wieder überschreiben.':
        'BioShock Infinite is currently running.\n\nPlease quit the game completely and try again. Otherwise the game would immediately overwrite the swapped save games.',
    'BioShock Infinite läuft noch. Gesichert wird nur, was das Spiel bereits gespeichert hat.\n\nTrotzdem jetzt sichern?':
        'BioShock Infinite is still running. Only what the game has already saved will be backed up.\n\nSave anyway?',
    'BioShock Infinite läuft – Spielstände des aktiven Profils lassen sich erst nach dem Beenden ändern.':
        'BioShock Infinite is running – save games of the active profile can only be changed after quitting.',
    'BioShock Infinite läuft – Wechseln, Neues Profil und Bearbeiten sind gesperrt.':
        'BioShock Infinite is running – switching, new profile and editing are locked.',
    'BioShock Infinite wurde beendet. Wechseln und Neues Profil sind wieder möglich.':
        'BioShock Infinite was closed. Switching and new profiles are possible again.',
    'Bitte beende BioShock Infinite, bevor du den Save-Ordner änderst.':
        'Please quit BioShock Infinite before changing the save folder.',
    'Bitte eine Zahl eingeben.':
        'Please enter a number.',
    'Bitte einen Namen eingeben.':
        'Please enter a name.',
    'Bitte zuerst über „Ändern …“ den Save-Ordner von BioShock Infinite auswählen.':
        'Please select the BioShock Infinite save folder via “Change …” first.',
    'Bitte über „Ändern …“ den Save-Ordner auswählen.':
        'Please select the save folder via “Change …”.',
    "Damit deine Profile nicht überschrieben werden, sollte die Steam-Cloud-Synchronisierung für BioShock Infinite ausgeschaltet sein.\n\nSo geht's:\n  1.  Steam öffnen → Bibliothek\n  2.  Rechtsklick auf „BioShock Infinite“ → Eigenschaften …\n  3.  Reiter „Allgemein“ → „Steam Cloud-Synchronisierung“\n       („Spielstände in der Steam Cloud speichern“) ausschalten\n\nWarum? Dieses Tool tauscht die Spielstände im Save-Ordner aus. Ist die Steam Cloud aktiv, kann Steam beim nächsten Spielstart ältere Cloud-Stände zurückspielen oder deine ausgetauschten Profile überschreiben.":
        'To keep your profiles from being overwritten, Steam Cloud synchronization should be turned off for BioShock Infinite.\n\nHow to:\n  1.  Open Steam → Library\n  2.  Right-click “BioShock Infinite” → Properties …\n  3.  “General” tab → turn off “Steam Cloud”\n       (“Keep games saves in the Steam Cloud”)\n\nWhy? This tool swaps the save games in the save folder. While Steam Cloud is active, Steam may restore older cloud saves on the next launch or overwrite your swapped profiles.',
    'Das Profil konnte nicht umbenannt werden (%s). Ist der Ordner gerade in einem anderen Programm geöffnet?':
        'The profile could not be renamed (%s). Is the folder currently open in another program?',
    'Das Profil konnte nicht vollständig gelöscht werden (%s).\n\nEin Backup liegt unter:\n%s':
        'The profile could not be deleted completely (%s).\n\nA backup is located at:\n%s',
    'Das Profil „%s“ existiert nicht (mehr).':
        'The profile “%s” does not exist (anymore).',
    'Das Profil „%s“ ist bereits aktiv.':
        'The profile “%s” is already active.',
    'Das Profil „%s“ konnte nicht aktualisiert werden (%s). Ist der Ordner gerade in einem anderen Programm geöffnet?':
        'The profile “%s” could not be updated (%s). Is the folder currently open in another program?',
    'Das Profil „%s“ konnte nicht geladen werden (%s).\n\nDer vorherige Stand von „%s“ wurde wiederhergestellt.':
        'The profile “%s” could not be loaded (%s).\n\nThe previous state of “%s” was restored.',
    'Das aktive Profil kann nicht gelöscht werden.\n\nWechsle zuerst zu einem anderen Profil (oder lege ein neues an).':
        'The active profile cannot be deleted.\n\nSwitch to another profile first (or create a new one).',
    'Das aktive Profil kann nicht gelöscht werden.\n\nWechsle zuerst zu einem anderen Profil.':
        'The active profile cannot be deleted.\n\nSwitch to another profile first.',
    'Datei':
        'file',
    'Dateien':
        'files',
    'Dein vorhandener Spielstand wurde als Profil „%s“ übernommen (umbenennbar).':
        'Your existing save game was imported as profile “%s” (you can rename it).',
    'Deine Profile':
        'Your profiles',
    'Der Name darf diese Zeichen nicht enthalten:  < > : " / \\ | ? *':
        'The name must not contain these characters:  < > : " / \\ | ? *',
    'Der Name darf höchstens %d Zeichen lang sein.':
        'The name may be at most %d characters long.',
    'Der Name darf nicht mit einem Punkt enden.':
        'The name must not end with a dot.',
    'Der Name darf nicht mit „_“ oder „.“ beginnen (intern reserviert).':
        'The name must not start with “_” or “.” (reserved for internal use).',
    'Der Ordner konnte nicht geöffnet werden:\n%s\n\n%s':
        'The folder could not be opened:\n%s\n\n%s',
    'Der Profil-Ordner konnte nicht angelegt werden:\n%s\n\n%s':
        'The profiles folder could not be created:\n%s\n\n%s',
    'Der Save-Ordner darf weder im Profil-Ordner liegen noch ihn enthalten:\n%s':
        'The save folder must neither be inside the profiles folder nor contain it:\n%s',
    'Der Save-Ordner des Spiels entspricht immer dem aktiven Profil.':
        "The game's save folder always holds the active profile.",
    'Der Save-Ordner konnte nicht geleert werden (%s).\n\nDer vorherige Stand von „%s“ wurde wiederhergestellt.':
        'The save folder could not be emptied (%s).\n\nThe previous state of “%s” was restored.',
    'Der Save-Ordner von BioShock Infinite wurde nicht automatisch gefunden.\n\nGesucht wurde in:\n  •  Dokumente\\My Games\\BioShock Infinite\\SaveData\n  •  Steam\\userdata\\<Steam-ID>\\8870\\remote\n\nBitte wähle den Ordner im nächsten Dialog selbst aus. (Tipp: Starte das Spiel einmal, dann entsteht der Ordner.)':
        'The BioShock Infinite save folder was not found automatically.\n\nSearched in:\n  •  Documents\\My Games\\BioShock Infinite\\SaveData\n  •  Steam\\userdata\\<Steam ID>\\8870\\remote\n\nPlease choose the folder yourself in the next dialog. (Tip: start the game once to create the folder.)',
    'Der Save-Ordner „%s“ enthält über %d Dateien und ist daher sehr wahrscheinlich nicht der Save-Ordner von BioShock Infinite. Aus Sicherheitsgründen abgebrochen.':
        'The save folder “%s” contains more than %d files and is therefore very likely not the BioShock Infinite save folder. Cancelled for safety reasons.',
    'Der Spielerzustand wurde in dieser Datei nicht gefunden.':
        'The player state was not found in this file.',
    'Der Spielstand konnte nicht geschrieben werden (%s). Das Original ist unverändert.':
        'The save game could not be written (%s). The original is unchanged.',
    'Der Spielstand „%s“ gehört nicht zum Profil „%s“.':
        'The save game “%s” does not belong to profile “%s”.',
    'Der Vorgang ist fehlgeschlagen und der Save-Ordner konnte nicht automatisch wiederhergestellt werden (%s).\n\nDein Spielstand ist im Backup gesichert:\n%s\\live':
        'The operation failed and the save folder could not be restored automatically (%s).\n\nYour save game is kept in the backup:\n%s\\live',
    'Die Datei ist zu klein für einen Spielstand.':
        'The file is too small to be a save game.',
    'Die Gegenprobe nach dem Schreiben ist fehlgeschlagen – das Original wurde aus dem Backup wiederhergestellt.':
        'The verification after writing failed – the original was restored from the backup.',
    'Die Gegenprobe nach dem Ändern ist fehlgeschlagen – es wurde nichts gespeichert.':
        'The verification after editing failed – nothing was saved.',
    'Die Inventartabelle ist unvollständig.':
        'The inventory table is incomplete.',
    'Die Konfigurationsdatei war beschädigt und wurde zurückgesetzt (%s). %s\n\nDeine Profile und Backups sind davon nicht betroffen.':
        'The configuration file was damaged and has been reset (%s). %s\n\nYour profiles and backups are not affected.',
    'Die Kopie nach „%s“ ist unvollständig (fehlend: %d, abweichend: %d, zusätzlich: %d).':
        'The copy to “%s” is incomplete (missing: %d, different: %d, extra: %d).',
    'Die Prüfsumme des Spielstands stimmt nicht (Datei beschädigt oder unbekanntes Format).':
        'The save game checksum does not match (file damaged or unknown format).',
    'Die geänderten Daten lassen sich nicht in die Originalgröße des Spielstands einpassen.':
        'The changed data cannot be fitted into the original size of the save game.',
    'Dieser Ordner sieht nicht wie ein Save-Ordner von BioShock Infinite aus:\n%s\n\nAchtung: „Neues Profil“ leert den Save-Ordner (nach einem Backup).\n\nTrotzdem verwenden?':
        'This folder does not look like a BioShock Infinite save folder:\n%s\n\nCaution: “New profile” empties the save folder (after a backup).\n\nUse it anyway?',
    'Dietriche':
        'Lockpicks',
    'Dietriche: %d liegt außerhalb von 0–%d.':
        'Lockpicks: %d is outside 0–%d.',
    'Dokumente\\My Games':
        'Documents\\My Games',
    'Dokumente\\My Games (Steam-ID %s)':
        'Documents\\My Games (Steam ID %s)',
    'Dokumente\\My Games\\BioShock Infinite\\SaveData':
        'Documents\\My Games\\BioShock Infinite\\SaveData',
    'Ein Profil mit dem Namen „%s“ gibt es bereits.':
        'A profile named “%s” already exists.',
    'Ein ganzes Laufwerk (z. B. C:\\) kann nicht als Save-Ordner verwendet werden.':
        'An entire drive (e.g. C:\\) cannot be used as the save folder.',
    'Eingesetzte Infusionen: %d   ·   im Hauptspiel gibt es %d, mit Season Pass %d   ·   für alle drei Werte auf Maximum wären %d nötig':
        'Infusions used: %d   ·   the main game has %d, %d with the Season Pass   ·   maxing all three stats would take %d',
    'Einstellung konnte nicht gespeichert werden: %s':
        'The setting could not be saved: %s',
    'Einstellungen':
        'Settings',
    'Entwickelt von %s  ·  © %s':
        'Developed by %s  ·  © %s',
    'Erlaubt: 0 – %s':
        'Allowed: 0 – %s',
    'Erlaubt: 0 – %s (normal bis %d)':
        'Allowed: 0 – %s (normally up to %d)',
    'Es ist ein unerwarteter Fehler aufgetreten:\n%s%s':
        'An unexpected error occurred:\n%s%s',
    'Es ist noch kein Save-Ordner festgelegt. Bitte über „Ändern…“ auswählen.':
        'No save folder has been set yet. Please select one via “Change…”.',
    'Es konnte nicht geprüft werden, ob BioShock Infinite läuft.\n\nIst das Spiel sicher beendet?':
        'It could not be checked whether BioShock Infinite is running.\n\nIs the game definitely closed?',
    'Experimentell: Das Save-Format ist nicht offiziell dokumentiert. Vor jedem Speichern entsteht ein Backup der Datei – prüfe die Werte nach dem Laden im Spiel.':
        'Experimental: the save format is not officially documented. A backup of the file is created before every save – check the values after loading the game.',
    'Fehler melden':
        'Report a bug',
    'Folgende Werte werden in „%s“ gespeichert:\n\n%s\n\nVorher wird ein Backup der Datei angelegt.':
        'The following values will be saved to “%s”:\n\n%s\n\nA backup of the file is created first.',
    'Geändert (vorher %s) – noch nicht gespeichert.':
        'Changed (was %s) – not saved yet.',
    'Hinweis beim Start nicht mehr anzeigen':
        "Don't show the notice at startup",
    'Hinweis beim Start wieder anzeigen':
        'Show the notice at startup again',
    'In diesem Profil gibt es noch keinen Spielstand des Hauptspiels.':
        'This profile does not contain a main game save yet.',
    'In diesem Spielstand nicht sicher erkannt – Bearbeiten nicht möglich.':
        'Not reliably detected in this save – editing is not possible.',
    'Infusionen':
        'Infusions',
    'Infusionen, Silberadler und Dietriche eines Spielstands ansehen und anpassen.':
        'View and adjust the Infusions, Silver Eagles and lockpicks of a save game.',
    'Inoffizielles Fan-Werkzeug. Verwaltet unabhängige Spielstand-Profile für BioShock Infinite (Steam-App %s), bearbeitet Spielwerte, legt vor jeder Änderung ein Backup an und startet das Spiel über Steam.':
        'Unofficial fan tool. Manages independent save game profiles for BioShock Infinite (Steam app %s), edits stats, creates a backup before every change and launches the game via Steam.',
    'Ja':
        'Yes',
    'Kapitelanfang':
        'Chapter start',
    'Kapitelanfang – wird über die Kapitelauswahl geladen.':
        'Chapter start – loaded via chapter select.',
    'Kein Ordner festgelegt.':
        'No folder set.',
    'Kein Save-Ordner':
        'No save folder',
    'Kein Save-Ordner festgelegt':
        'No save folder set',
    'Kein Save-Ordner festgelegt – bitte über „Ändern …“ auswählen.':
        'No save folder set – please select one via “Change …”.',
    'Kein offizielles Produkt und keine Verbindung zu 2K, Irrational Games oder Take-Two Interactive. BioShock ist eine Marke von Take-Two Interactive Software, Inc. Das Logo im Kopfbereich wird zur Laufzeit aus deiner lokalen Steam-Bibliothek geladen und ist nicht Teil dieses Programms.':
        'Not an official product and not affiliated with 2K, Irrational Games or Take-Two Interactive. BioShock is a trademark of Take-Two Interactive Software, Inc. The logo in the header is loaded at runtime from your local Steam library and is not part of this program.',
    'Keine Änderungen – das Profil „%s“ ist bereits aktuell.':
        'No changes – the profile “%s” is already up to date.',
    'Konfiguration zurückgesetzt':
        'Configuration reset',
    'Leben':
        'Health',
    'Liegt gerade im Save-Ordner des Spiels':
        "Currently in the game's save folder",
    'Löschen':
        'Delete',
    'Löschen des Profils „%s“':
        'Deleting profile “%s”',
    'Manuelles Sichern von „%s“':
        'Manual save of “%s”',
    'Max.':
        'Max',
    'Maximum erreicht – diese Option lässt sich bei weiteren Infusionen nicht mehr wählen.':
        'Maximum reached – this option can no longer be chosen at further Infusions.',
    'Mehr als %d Dietriche: Das ist über der normalen Obergrenze des Spiels (Trainer-Bereich). Das Spiel lädt solche Werte, nach dem Aufheben weiterer Dietriche kann es aber auf %d begrenzen.':
        "More than %d lockpicks: this is above the game's normal limit (trainer territory). The game loads such values, but may cap them at %d once you pick up more lockpicks.",
    'Mindestens ein Wert liegt über %d. So etwas entsteht durch Trainer/Cheats; wie das Spiel damit umgeht, ist nicht sicher.':
        'At least one stat is above %d. This is caused by trainers/cheats; how the game handles it is uncertain.',
    'Neuer Name für „%s“:':
        'New name for “%s”:',
    'Neues Profil':
        'New profile',
    'Neues Profil „%s“ (vorher aktiv: „%s“)':
        'New profile “%s” (previously active: “%s”)',
    'Neues Profil „%s“ ist aktiv – starte jetzt ein neues Spiel. (Backup: %s)':
        'New profile “%s” is active – start a new game now. (Backup: %s)',
    'Neuester Autosave – den lädt das Spiel über „Fortsetzen“.':
        'Latest autosave – the game loads it via “Continue”.',
    'Nicht mehr anzeigen':
        "Don't show again",
    'Noch kein Profil vorhanden.':
        'No profile yet.',
    'Noch keine Infusion dieses Typs gewählt – nimm im Spiel einmal diese Option, danach lässt sich der Wert hier ändern.':
        'No Infusion of this type chosen yet – pick this option once in the game, then the value can be changed here.',
    'Noch keine Profile vorhanden.':
        'No profiles yet.',
    'Nur zur Info – füllt sich im Spiel von selbst auf.':
        'For information only – refills by itself in the game.',
    'Ordner innerhalb von Windows können nicht als Save-Ordner verwendet werden.':
        'Folders inside Windows cannot be used as the save folder.',
    'Ordner prüfen':
        'Check folder',
    'Ordner öffnen':
        'Open folder',
    'PNG ohne Kopfdaten':
        'PNG without header',
    'PROFIL-ARCHIV':
        'PROFILE ARCHIVE',
    'Profil':
        'Profile',
    'Profil konnte nicht angelegt werden':
        'Profile could not be created',
    'Profil löschen':
        'Delete profile',
    'Profil umbenennen':
        'Rename profile',
    'Profil wechseln':
        'Switch profile',
    'Profil „%s“ heißt jetzt „%s“.':
        'Profile “%s” is now called “%s”.',
    'Profil „%s“ ist jetzt aktiv. (Backup: %s)':
        'Profile “%s” is now active. (Backup: %s)',
    'Profil „%s“ wirklich löschen?\n\nVorher wird automatisch ein Backup angelegt unter:\n%s':
        'Really delete profile “%s”?\n\nA backup is created automatically first at:\n%s',
    'Profil „%s“ wurde gelöscht. (Backup: %s)':
        'Profile “%s” was deleted. (Backup: %s)',
    'Profil-Ordner konnte nicht gelesen werden: %s':
        'The profiles folder could not be read: %s',
    'Profile':
        'Profiles',
    'Projektseite auf GitHub':
        'Project page on GitHub',
    'Protokoll':
        'Log',
    'Prüfung nicht möglich':
        'Check not possible',
    'Salze':
        'Salts',
    'Salze im Vorrat':
        'Salts in stock',
    'Save-Ordner':
        'Save folder',
    'Save-Ordner (Spiel)':
        'Save folder (game)',
    'Save-Ordner automatisch erkannt: %s%s':
        'Save folder detected automatically: %s%s',
    'Save-Ordner festgelegt: %s':
        'Save folder set: %s',
    'Save-Ordner nicht gefunden':
        'Save folder not found',
    'Save-Ordner von BioShock Infinite auswählen':
        'Select the BioShock Infinite save folder',
    'Save-Ordner ändern':
        'Change save folder',
    'Save-Ordner:    %s':
        'Save folder:    %s',
    'Schild':
        'Shield',
    'Sichern':
        'Save',
    'Sie wurde als „%s“ aufbewahrt.':
        'It was kept as “%s”.',
    'Silberadler':
        'Silver Eagles',
    'Silberadler und Dietriche wurden in diesem Spielstand nicht sicher erkannt und können deshalb nicht geändert werden.':
        'Silver Eagles and lockpicks were not reliably detected in this save and therefore cannot be changed.',
    'Silberadler: %d liegt außerhalb von 0–%d.':
        'Silver Eagles: %d is outside 0–%d.',
    'Speichern':
        'Save',
    'Speicherorte':
        'Storage locations',
    'Speicherorte, Steam Cloud und Informationen.':
        'Storage locations, Steam Cloud and information.',
    'Spiel ist beendet':
        'Game is closed',
    'Spiel läuft':
        'Game is running',
    'Spiel läuft – Austausch gesperrt':
        'Game running – swapping locked',
    'Spiel läuft …':
        'Game running …',
    'Spiel starten':
        'Start game',
    'Spiel wird über Steam gestartet … (aktives Profil: „%s“)':
        'Starting the game via Steam … (active profile: “%s”)',
    'Spielstand':
        'Save game',
    'Spielstand konnte nicht gelesen werden: %s':
        'The save game could not be read: %s',
    'Spielstatus unbekannt':
        'Game status unknown',
    'Spielstatus wird geprüft …':
        'Checking game status …',
    'Spielstände sicher verwahrt  ·  Columbia, anno 1912':
        'Save games kept safe  ·  Columbia, anno 1912',
    'Spielwerte':
        'Stats',
    'Spielwerte in „%s“ gespeichert. (Backup: %s)':
        'Stats saved to “%s”. (Backup: %s)',
    'Spielwerte speichern':
        'Save stats',
    'Spielwerte: ':
        'Stats: ',
    'Sprache / Language':
        'Language / Sprache',
    'Sprache geändert.':
        'Language changed.',
    'Start fehlgeschlagen':
        'Launch failed',
    'Steam Cloud':
        'Steam Cloud',
    'Steam konnte nicht gestartet werden (%s).\n\nIst Steam installiert?':
        'Steam could not be started (%s).\n\nIs Steam installed?',
    'Steam-Cloud-Ordner (Steam-ID %s)':
        'Steam Cloud folder (Steam ID %s)',
    'Trotzdem verwenden':
        'Use anyway',
    'Unerwartete Inventartabelle im Spielerzustand.':
        'Unexpected inventory table in the player state.',
    'Unerwartete Variablentabelle im Spielerzustand.':
        'Unexpected variable table in the player state.',
    'Unerwarteter Fehler':
        'Unexpected error',
    'Unerwarteter Fehler:\n%s\n\nDetails: %s':
        'Unexpected error:\n%s\n\nDetails: %s',
    'Unerwarteter Inventareintrag im Spielerzustand.':
        'Unexpected inventory entry in the player state.',
    'Ungeeigneter Ordner':
        'Unsuitable folder',
    'Ungültiger Name':
        'Invalid name',
    'Ungültiger Profilname: „%s“':
        'Invalid profile name: “%s”',
    'Ungültiger Save-Ordner':
        'Invalid save folder',
    'Verstanden':
        'Got it',
    'Vor jeder Änderung entsteht automatisch ein Backup.':
        'A backup is created automatically before every change.',
    'Vorräte':
        'Supplies',
    'Wechsel von „%s“ zu „%s“':
        'Switch from “%s” to “%s”',
    'Wechseln':
        'Switch',
    'Wichtig: Steam Cloud für BioShock Infinite abschalten':
        'Important: turn off Steam Cloud for BioShock Infinite',
    'Wie soll das neue Profil heißen?\n\nVorher wird der aktuelle Stand ins Profil „%s“ gesichert und ein Backup angelegt. Danach wird der Save-Ordner geleert – BioShock Infinite bietet dann ein neues Spiel an.':
        'What should the new profile be called?\n\nFirst the current state is saved to profile “%s” and a backup is created. Then the save folder is emptied – BioShock Infinite will offer a new game.',
    'Wiederherstellen: Spiel beenden, dann den Inhalt des gewünschten Unterordners':
        'Restore: quit the game, then copy the contents of the desired subfolder',
    'Zeitpunkt:      %s':
        'Time:           %s',
    'Zu Profil „%s“ wechseln?\n\nDer aktuelle Stand wird vorher ins Profil „%s“ gesichert und zusätzlich ein Backup angelegt.':
        'Switch to profile “%s”?\n\nThe current state is saved to profile “%s” first, and a backup is created as well.',
    'Zugriff verweigert':
        'Access denied',
    'Zurücksetzen':
        'Reset',
    'Zusammen %d Stufen – mehr, als das Spiel regulär hergibt (%d im Hauptspiel, %d mit Season Pass).':
        '%d levels in total – more than the game normally offers (%d in the main game, %d with the Season Pass).',
    'aktiv':
        'active',
    'config.json konnte nicht gespeichert werden (%s). Der vorherige Stand wurde wiederhergestellt.':
        'config.json could not be saved (%s). The previous state was restored.',
    'diese PNG-Variante wird nicht unterstützt':
        'this PNG variant is not supported',
    'in den Save-Ordner bzw. in den Profilordner kopieren.':
        'into the save folder or the profile folder.',
    'je Stufe ca. +15 % Max. Leben':
        'approx. +15% max health per level',
    'je Stufe ca. +15 Salze':
        'approx. +15 salts per level',
    'je Stufe ca. +30 % Schild':
        'approx. +30% more shield per level',
    'kein JSON-Objekt':
        'not a JSON object',
    'kein Programm für steam:// registriert':
        'no program registered for steam://',
    'keine PNG-Datei':
        'not a PNG file',
    'liegt im Profilordner':
        'stored in the profile folder',
    'liegt im Save-Ordner des Spiels (aktives Profil)':
        "stored in the game's save folder (active profile)",
    'manuell gewählter Ordner':
        'manually selected folder',
    'noch keine Backups':
        'no backups yet',
    'unbekannter PNG-Filter':
        'unknown PNG filter',
    'v%s  ·  von %s':
        'v%s  ·  by %s',
    'Älterer Autosave.':
        'Older autosave.',
    'Ändern …':
        'Change …',
    'Änderungen speichern':
        'Save changes',
    'Öffnen':
        'Open',
    'Über':
        'About',
    'Über dem Maximum von %d – stammt sehr wahrscheinlich von einem Trainer/Cheat. Mit „−“ oder einem Klick auf die Rauten lässt sich der Wert korrigieren.':
        'Above the maximum of %d – most likely caused by a trainer/cheat. Use “−” or click the diamonds to correct the value.',
    'Über der normalen Obergrenze von %d (vermutlich Trainer).':
        'Above the normal limit of %d (probably a trainer).',
    'Übernahme des vorhandenen Spielstands als Profil „%s“':
        'Import of the existing save game as profile “%s”',
    'Übernehmen':
        'Apply',
    '„%s“ ist bereits das aktive Profil.':
        '“%s” is already the active profile.',
    '„%s“ ist ein allgemeiner Benutzerordner und kann nicht als Save-Ordner verwendet werden.':
        '“%s” is a general user folder and cannot be used as the save folder.',
    '„%s“ ist ein allgemeiner System- oder Programmordner und kann nicht als Save-Ordner verwendet werden.':
        '“%s” is a general system or program folder and cannot be used as the save folder.',
    '„%s“ ist kein Ordner.':
        '“%s” is not a folder.',
    '„%s“ ist unter Windows ein reservierter Name.':
        '“%s” is a reserved name on Windows.',
    '⇄   Zu diesem Profil wechseln':
        '⇄   Switch to this profile',
    '⚠  Steam-Cloud-Ordner – bitte die Cloud-Synchronisierung für BioShock Infinite ausschalten (siehe Einstellungen).':
        '⚠  Steam Cloud folder – please turn off cloud sync for BioShock Infinite (see Settings).',
    '✎   Umbenennen …':
        '✎   Rename …',
    '✖   Löschen …':
        '✖   Delete …',
    '✚   Neues Profil …':
        '✚   New profile …',
    '⤓   Aktuellen Stand sichern':
        '⤓   Save current state',
}


# ---------------------------------------------------------------------------
# Programmstart
# ---------------------------------------------------------------------------

def setup_logging(profiles_dir: Path) -> Optional[Path]:
    path = profiles_dir / LOG_FILENAME
    try:
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=512_000, backupCount=2, encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    return path


def enable_dpi_awareness() -> None:
    """Scharfe Darstellung auf hochauflösenden Bildschirmen."""
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def write_version_file(path) -> None:
    """Versionsinfo für PyInstaller (Explorer → Eigenschaften → Details der .exe) – nutzt build.bat."""
    nums = tuple(int(x) for x in (APP_VERSION.split(".") + ["0", "0", "0"])[:4])
    text = """VSVersionInfo(
  ffi=FixedFileInfo(filevers=%(nums)r, prodvers=%(nums)r, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', %(author)r),
      StringStruct('FileDescription', %(name)r),
      StringStruct('FileVersion', %(version)r),
      StringStruct('InternalName', 'BioShockProfileManager'),
      StringStruct('LegalCopyright', %(copyright)r),
      StringStruct('OriginalFilename', 'BioShockProfileManager.exe'),
      StringStruct('ProductName', %(name)r),
      StringStruct('ProductVersion', %(version)r),
      StringStruct('Comments', %(comments)r)])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""" % {"nums": nums, "author": APP_AUTHOR, "name": APP_NAME, "version": APP_VERSION,
       "copyright": "Copyright (c) %s %s - %s License" % (APP_YEAR, APP_AUTHOR, APP_LICENSE),
       "comments": "Unofficial fan tool, not affiliated with 2K or Take-Two. %s" % APP_URL}
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--profiles-dir", help="use a different profiles folder (e.g. for testing)")
    parser.add_argument("--live-dir", help="use this game save folder instead of auto-detection")
    parser.add_argument("--lang", choices=sorted(LANGUAGES), help="user interface language (de/en)")
    args = parser.parse_args(argv)

    set_language(args.lang or system_language())
    enable_dpi_awareness()
    profiles_dir = Path(args.profiles_dir) if args.profiles_dir else get_default_profiles_dir()
    try:
        profiles_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        tk.Tk().withdraw()
        messagebox.showerror(APP_NAME, tr("Der Profil-Ordner konnte nicht angelegt werden:\n%s\n\n%s") % (profiles_dir, exc))
        return 1

    log_path = setup_logging(profiles_dir)
    log.info("Start %s %s by %s, Profile: %s", APP_NAME, APP_VERSION, APP_AUTHOR, profiles_dir)
    config = Config(profiles_dir / CONFIG_FILENAME)
    config.load()
    if not args.lang:
        saved = config.data.get("language")
        set_language(saved if saved in LANGUAGES else system_language())
    manager = ProfileManager(profiles_dir, config)

    root = tk.Tk()
    root.withdraw()  # erst nach dem Einfärben der Titelleiste zeigen (kein weißes Aufblitzen)

    def report_callback_exception(exc_type, exc_value, exc_tb):
        log.error("Unbehandelter Fehler", exc_info=(exc_type, exc_value, exc_tb))
        messagebox.showerror(APP_NAME, tr("Unerwarteter Fehler:\n%s\n\nDetails: %s") % (exc_value, log_path or tr("–")))

    root.report_callback_exception = report_callback_exception
    App(root, manager, live_dir_override=Path(args.live_dir) if args.live_dir else None, log_path=log_path)
    root.deiconify()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
