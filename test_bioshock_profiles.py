# -*- coding: utf-8 -*-
"""Tests der Dateilogik von bioshock_profiles.py – arbeitet nur in temporären Dummy-Ordnern.

Start:  python -m unittest test_bioshock_profiles -v
"""

import json
import logging
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import bioshock_profiles as bp

bp.log.addHandler(logging.NullHandler())  # erwartete Fehler-Logs nicht auf die Konsole schreiben


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def snapshot(path: Path) -> dict:
    """Relativer Pfad -> Dateiinhalt, zum Vergleichen ganzer Ordner."""
    result = {}
    for root, _dirs, files in os.walk(path):
        for name in files:
            full = Path(root) / name
            result[full.relative_to(path).as_posix()] = full.read_text(encoding="utf-8")
    return result


class DummySetup(unittest.TestCase):
    """Legt pro Test einen frischen Dummy-Save-Ordner und Profil-Ordner an."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bsi_test_"))
        self.live = self.tmp / "Steam" / "userdata" / "12345" / "8870" / "remote"
        self.profiles = self.tmp / "Documents" / "BioShockProfiles"
        self.profiles.mkdir(parents=True)
        # "Alter" Spielstand wie im echten Steam-Cloud-Ordner
        write(self.live / "profile.bin", "profil-alt")
        write(self.live / "SaveData" / "Emp-0_1.bkm", "bookmark-alt")
        write(self.live / "SaveData" / "Hub-0_2.sav", "save-alt")
        self.game_running = False
        self.config = bp.Config(self.profiles / bp.CONFIG_FILENAME)
        self.config.live_dir = self.live
        self.mgr = bp.ProfileManager(self.profiles, self.config, game_running=lambda: self.game_running)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def backups(self):
        d = self.profiles / bp.BACKUP_DIRNAME
        return sorted(p for p in d.iterdir() if p.is_dir()) if d.is_dir() else []

    def simulate_new_game(self, tag: str):
        """So, als hätte das Spiel einen neuen Spielstand geschrieben."""
        write(self.live / "profile.bin", "profil-" + tag)
        write(self.live / "SaveData" / ("Start-%s.sav" % tag), "save-" + tag)


class InitialProfileTests(DummySetup):
    def test_first_start_takes_over_existing_save(self):
        old = snapshot(self.live)
        created = self.mgr.ensure_initial_profile()
        self.assertEqual(created, "Standard")
        self.assertEqual(self.mgr.active, "Standard")
        self.assertEqual(snapshot(self.profiles / "Standard"), old)
        self.assertEqual(snapshot(self.live), old, "Live-Ordner darf nicht verändert werden")
        # Backup des Ausgangszustands
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(snapshot(self.backups()[0] / "live"), old)
        # config.json persistiert
        data = json.loads((self.profiles / bp.CONFIG_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(data["active_profile"], "Standard")

    def test_second_call_is_noop(self):
        self.mgr.ensure_initial_profile()
        self.assertIsNone(self.mgr.ensure_initial_profile())
        self.assertEqual(self.mgr.list_profiles(), ["Standard"])

    def test_internal_folders_are_not_profiles(self):
        self.mgr.ensure_initial_profile()
        (self.profiles / "_work").mkdir(exist_ok=True)
        self.assertEqual(self.mgr.list_profiles(), ["Standard"])


class NewProfileTests(DummySetup):
    def test_new_profile_saves_backs_up_and_clears(self):
        self.mgr.ensure_initial_profile()
        self.simulate_new_game("weiter")  # Fortschritt seit dem letzten Sichern
        progressed = snapshot(self.live)

        backup = self.mgr.new_profile("  Zweiter Durchlauf ")

        self.assertEqual(self.mgr.active, "Zweiter Durchlauf")
        self.assertEqual(list(self.live.iterdir()), [], "Live-Ordner muss leer sein")
        self.assertTrue(self.live.is_dir(), "Live-Ordner selbst bleibt bestehen")
        self.assertEqual(snapshot(self.profiles / "Standard"), progressed)
        self.assertEqual(snapshot(backup / "live"), progressed)
        # Der ältere Profilstand (vor dem Fortschritt) steckt ebenfalls im Backup
        self.assertIn("SaveData/Hub-0_2.sav", snapshot(backup / "profil_Standard"))
        self.assertNotIn("SaveData/Start-weiter.sav", snapshot(backup / "profil_Standard"))
        self.assertTrue((backup / "info.txt").is_file())
        self.assertEqual(sorted(self.mgr.list_profiles()), ["Standard", "Zweiter Durchlauf"])

    def test_new_profile_blocked_while_game_runs(self):
        self.mgr.ensure_initial_profile()
        before = snapshot(self.live)
        self.game_running = True
        with self.assertRaises(bp.GameRunningError):
            self.mgr.new_profile("Neu")
        self.assertEqual(snapshot(self.live), before)
        self.assertEqual(self.mgr.active, "Standard")
        self.assertNotIn("Neu", self.mgr.list_profiles())

    def test_new_profile_without_initial_profile_keeps_old_save(self):
        old = snapshot(self.live)
        self.mgr.new_profile("Neu")
        self.assertEqual(self.mgr.active, "Neu")
        self.assertEqual(snapshot(self.profiles / "Standard"), old)


class SwitchTests(DummySetup):
    def test_full_roundtrip(self):
        self.mgr.ensure_initial_profile()
        original = snapshot(self.live)
        self.mgr.new_profile("Zweit")
        self.simulate_new_game("zweit")
        second = snapshot(self.live)

        backup = self.mgr.switch_to("Standard")
        self.assertEqual(self.mgr.active, "Standard")
        self.assertEqual(snapshot(self.live), original)
        self.assertEqual(snapshot(self.profiles / "Zweit"), second)
        self.assertEqual(snapshot(backup / "live"), second)

        # Im Standard-Profil weiterspielen, dann zurück
        self.simulate_new_game("standard2")
        standard_progress = snapshot(self.live)
        self.mgr.switch_to("zweit")  # Groß/Klein egal
        self.assertEqual(self.mgr.active, "Zweit")
        self.assertEqual(snapshot(self.live), second)
        self.assertEqual(snapshot(self.profiles / "Standard"), standard_progress)

    def test_switch_blocked_while_game_runs(self):
        self.mgr.ensure_initial_profile()
        self.mgr.new_profile("Zweit")
        self.simulate_new_game("x")
        before_live = snapshot(self.live)
        before_backups = len(self.backups())
        self.game_running = True
        with self.assertRaises(bp.GameRunningError):
            self.mgr.switch_to("Standard")
        self.assertEqual(snapshot(self.live), before_live)
        self.assertEqual(self.mgr.active, "Zweit")
        self.assertEqual(len(self.backups()), before_backups, "kein Backup/keine Aktion bei laufendem Spiel")

    def test_switch_to_active_or_unknown_fails(self):
        self.mgr.ensure_initial_profile()
        with self.assertRaises(bp.ProfileError):
            self.mgr.switch_to("Standard")
        with self.assertRaises(bp.ProfileError):
            self.mgr.switch_to("GibtEsNicht")

    def test_rollback_when_loading_target_fails(self):
        self.mgr.ensure_initial_profile()
        self.mgr.new_profile("Zweit")
        self.simulate_new_game("zweit")
        second = snapshot(self.live)

        real_copy = bp.copy_dir_contents
        calls = {"n": 0}

        def flaky_copy(src, dst):
            # Fehler genau beim Kopieren des Zielprofils in den Live-Ordner
            if bp._norm(dst) == bp._norm(self.live) and calls["n"] == 0:
                calls["n"] += 1
                write(self.live / "halb.sav", "halb kopiert")
                raise OSError("Datenträger voll (simuliert)")
            return real_copy(src, dst)

        with mock.patch.object(bp, "copy_dir_contents", side_effect=flaky_copy):
            with self.assertRaises(bp.ProfileError) as ctx:
                self.mgr.switch_to("Standard")
        self.assertIn("wiederhergestellt", str(ctx.exception))
        self.assertEqual(self.mgr.active, "Zweit")
        self.assertEqual(snapshot(self.live), second)


class SaveRenameDeleteTests(DummySetup):
    def test_save_current(self):
        self.mgr.ensure_initial_profile()
        changed, backup = self.mgr.save_current()
        self.assertFalse(changed, "ohne Änderung nichts tun")
        self.assertIsNone(backup)

        time.sleep(1.1)  # andere Änderungszeit erzwingen
        self.simulate_new_game("fortschritt")
        changed, backup = self.mgr.save_current()
        self.assertTrue(changed)
        self.assertEqual(snapshot(self.profiles / "Standard"), snapshot(self.live))
        self.assertNotIn("SaveData/Start-fortschritt.sav", snapshot(backup / "profil_Standard"))

    def test_rename_active_updates_config(self):
        self.mgr.ensure_initial_profile()
        self.mgr.rename_profile("Standard", "Erster Durchlauf")
        self.assertEqual(self.mgr.active, "Erster Durchlauf")
        self.assertEqual(self.mgr.list_profiles(), ["Erster Durchlauf"])
        reloaded = bp.Config(self.profiles / bp.CONFIG_FILENAME)
        reloaded.load()
        self.assertEqual(reloaded.active_profile, "Erster Durchlauf")
        # Nur Groß/Kleinschreibung ändern ist erlaubt
        self.mgr.rename_profile("Erster Durchlauf", "ERSTER Durchlauf")
        self.assertEqual(self.mgr.list_profiles(), ["ERSTER Durchlauf"])

    def test_rename_to_existing_fails(self):
        self.mgr.ensure_initial_profile()
        self.mgr.new_profile("Zweit")
        with self.assertRaises(bp.ProfileError):
            self.mgr.rename_profile("Zweit", "standard")

    def test_delete_creates_backup_and_refuses_active(self):
        self.mgr.ensure_initial_profile()
        self.mgr.new_profile("Zweit")
        with self.assertRaises(bp.ProfileError):
            self.mgr.delete_profile("Zweit")  # aktiv
        content = snapshot(self.profiles / "Standard")
        backup = self.mgr.delete_profile("Standard")
        self.assertNotIn("Standard", self.mgr.list_profiles())
        self.assertEqual(snapshot(backup / "profil_Standard"), content)

    def test_name_validation(self):
        self.mgr.ensure_initial_profile()
        for bad in ["", "   ", "a/b", "a:b", "CON", "lpt1.txt", "_intern", "x" * 41, "Punkt.", "STANDARD"]:
            with self.assertRaises(bp.ProfileError, msg=repr(bad)):
                self.mgr.validate_name(bad)
        self.assertEqual(self.mgr.validate_name(" Durchlauf 1999 "), "Durchlauf 1999")


class SafetyTests(DummySetup):
    def test_dangerous_live_dirs_are_refused(self):
        drive_root = Path(os.path.abspath(os.sep))
        for bad in [drive_root, self.profiles, self.profiles / "Standard", self.profiles.parent, Path.home()]:
            self.assertIsNotNone(bp.check_live_dir_safety(bad, self.profiles), str(bad))
        self.assertIsNone(bp.check_live_dir_safety(self.live, self.profiles))

    def test_operations_refuse_unsafe_configured_live_dir(self):
        # z. B. von Hand in config.json eingetragen
        self.config.live_dir = self.profiles.parent
        with self.assertRaises(bp.ProfileError):
            self.mgr.new_profile("Neu")

    def test_corrupt_config_is_reset_and_kept(self):
        cfg_path = self.profiles / bp.CONFIG_FILENAME
        cfg_path.write_text("{ kaputt", encoding="utf-8")
        cfg = bp.Config(cfg_path)
        cfg.load()
        self.assertIsNotNone(cfg.load_problem)
        self.assertIsNone(cfg.active_profile)
        self.assertTrue(list(self.profiles.glob("config.defekt_*.json")))


def noise(n: int, seed: int = 1) -> bytes:
    """Teils zufällige, teils wiederholte Bytes (ähnlich komprimierbar wie echte Spielstände)."""
    import random
    rnd = random.Random(seed)
    out = bytearray()
    words = [b"Master_P", b"\x00\x00\x00\x00", b"\x1d\xfb\xff\xff", b"Elizabeth", b"\x01\x00\x00\x00"]
    while len(out) < n:
        out += rnd.choice(words) if rnd.random() < 0.6 else bytes(rnd.getrandbits(8) for _ in range(rnd.randint(1, 6)))
    return bytes(out[:n])


def build_save(items, trailer=b"", level=1, money=1598, lockpicks=7, salts=160, currency=True) -> bytes:
    """Synthetischer Spielstand im selben Aufbau wie die echten .sav-Dateien.

    Das Spiel komprimiert mit zlib-Stufe 1 – daher auch hier Stufe 1.

    items: Liste von (Item-ID, Menge, Zusatzbytes)
    """
    import hashlib
    import struct
    import zlib
    body = struct.pack("<I", bp.PLAYER_STATE_MAGIC) + bytes(range(37))      # bis Offset 45 (inkl. Länge)
    body += struct.pack("<I", 2)                                             # 2 Variablen
    body += struct.pack("<IIif", 0, 0, 1, 12.5) + struct.pack("<IIif", 1, 0, 7, 99.0)
    body += struct.pack("<I", len(items))
    for item_id, amount, extra in items:
        body += struct.pack("<QiBii", item_id, amount, 1, 0, len(extra)) + extra
    body += noise(700)
    if currency:  # Währungsblock: Marker, Geld, 4x 0, Salze, Zähler, Dietriche, 0
        body += bp.CURRENCY_MARKER + struct.pack("<9i", money, 0, 0, 0, 0, salts, 2, lockpicks, 0)
    body += noise(2300, seed=5)  # wie echte Daten: teils zufällig, teils wiederholt
    raw = struct.pack("<I", len(body)) + body
    comp = zlib.compress(raw, level)
    chunk = (bp.UE3_CHUNK_TAG + struct.pack("<5I", 0x20000, len(comp), len(raw), len(comp), len(raw)) + comp)
    rest = b"Kopf\x00Master_P\x00" + bytes(range(256)) + chunk + (trailer or bytes(range(255, -1, -1)) * 3)
    return hashlib.md5(rest).digest() + rest


WEAPON = (0x0001000016600050, 1, b"\x00")


class SaveFormatTests(unittest.TestCase):
    def setUp(self):
        self.items = [(3, 1, b""), WEAPON, (276, 3, b""), (329, 6, b""), (235, 1000, b""), (346, 1, b"")]
        self.data = build_save(self.items)

    def test_reads_levels(self):
        s = bp.BioShockSave(self.data)
        self.assertEqual(s.infusion_levels(), {"health": 3, "shield": 6, "salts": 1000})

    def test_edit_keeps_everything_else(self):
        import hashlib
        s = bp.BioShockSave(self.data)
        new = s.with_infusion_levels({"health": 10, "salts": 10})
        self.assertEqual(len(new), len(self.data), "Dateigröße muss gleich bleiben")
        self.assertEqual(new[:16], hashlib.md5(new[16:]).digest(), "MD5 neu berechnet")
        end = s.comp_offset + s.comp_size
        self.assertEqual(new[16:s.comp_offset], self.data[16:s.comp_offset])
        self.assertEqual(new[end:], self.data[end:])
        again = bp.BioShockSave(new)
        self.assertEqual(again.infusion_levels(), {"health": 10, "shield": 6, "salts": 10})
        # alle übrigen Inventareinträge unverändert
        self.assertEqual([(r.item_id, r.amount) for r in again.items if r.item_id not in (276, 235)],
                         [(r.item_id, r.amount) for r in s.items if r.item_id not in (276, 235)])

    def test_missing_entry_and_range(self):
        data = build_save([(3, 1, b""), (329, 2, b"")])
        s = bp.BioShockSave(data)
        self.assertEqual(s.infusion_levels(), {"health": None, "shield": 2, "salts": None})
        with self.assertRaises(bp.SaveFormatError):
            s.with_infusion_levels({"health": 1})          # Eintrag fehlt noch
        s.with_infusion_levels({"health": 0, "shield": 3})  # 0 bei fehlendem Eintrag ist harmlos
        for bad in (-1, 11, 1000):
            with self.assertRaises(bp.SaveFormatError):
                s.with_infusion_levels({"shield": bad})

    def test_rejects_corrupt_or_foreign_files(self):
        broken = bytearray(self.data)
        broken[200] ^= 0xFF
        with self.assertRaises(bp.SaveFormatError):
            bp.BioShockSave(bytes(broken))                  # Prüfsumme passt nicht
        with self.assertRaises(bp.SaveFormatError):
            bp.BioShockSave(b"\x00" * 1000)

    def test_exact_size_deflate(self):
        import zlib
        raw = noise(9000, seed=7)
        base = len(zlib.compress(raw, 9))
        for target in list(range(base, base + 16)) + [base + 40, base + 200]:
            out = bp.deflate_to_exact_size(raw, target, b"\x78\x9c")
            self.assertEqual(len(out), target)
            d = zlib.decompressobj()
            self.assertEqual(d.decompress(out), raw)
            self.assertTrue(d.eof)
            self.assertEqual(d.unused_data, b"")
        with self.assertRaises(bp.SaveFormatError):
            bp.deflate_to_exact_size(raw, 20, b"\x78\x9c")  # kleiner geht nicht

    def test_money_and_lockpicks(self):
        s = bp.BioShockSave(self.data)
        self.assertEqual(s.resources(), {"money": 1598, "lockpicks": 7, "salts_current": 160})
        new = s.with_changes(money=99999, lockpicks=30, infusions={"health": 4})
        self.assertEqual(len(new), len(self.data))
        again = bp.BioShockSave(new)
        self.assertEqual(again.resources(), {"money": 99999, "lockpicks": 30, "salts_current": 160})
        self.assertEqual(again.infusion_levels()["health"], 4)
        for bad in ({"money": -1}, {"money": bp.MONEY_MAX + 1}, {"lockpicks": bp.LOCKPICKS_MAX + 1}):
            with self.assertRaises(bp.SaveFormatError):
                s.with_changes(**bad)

    def test_unknown_currency_block_is_never_touched(self):
        s = bp.BioShockSave(build_save(self.items, currency=False))
        self.assertEqual(s.resources(), {"money": None, "lockpicks": None, "salts_current": None})
        with self.assertRaises(bp.SaveFormatError):
            s.with_changes(money=100)
        s.with_changes(infusions={"health": 5})  # Infusionen gehen trotzdem

    def test_save_names(self):
        tmp = Path(tempfile.mkdtemp(prefix="bsi_names_"))
        try:
            for name in ["SFink_HubTHunt-0_4_15_2024_22_6_56_271.sav", "SFink_Hub01A-0_4_15_2024_22_2_31_270.bkm",
                         "DEPT_MAIN_Arrived-2_3_5_2022_16_7_24_205.bkm", "profile.bin", "kaputt.sav"]:
                write(tmp / "SaveData" / name, "x")
            saves = bp.list_saves(tmp)
            self.assertEqual([s.sequence for s in saves], [271, 270, 205])
            self.assertTrue(saves[0].is_main_game and not saves[0].is_bookmark)
            self.assertFalse(saves[2].is_main_game)
            self.assertEqual(saves[0].timestamp.year, 2024)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class WriteSaveTests(DummySetup):
    def setUp(self):
        super().setUp()
        self.save = self.live / "SaveData" / "SFink_Hub-0_4_15_2024_22_6_56_271.sav"
        self.save.write_bytes(build_save([(276, 3, b""), (329, 6, b""), (235, 6, b"")]))
        os.utime(self.save, (1_700_000_000, 1_700_000_000))
        self.mgr.ensure_initial_profile()

    def test_write_with_backup_and_same_timestamp(self):
        s = bp.BioShockSave.load(self.save)
        new = s.with_infusion_levels({"health": 4})
        original = self.save.read_bytes()
        backup = self.mgr.write_save_file("Standard", self.save, new, "Test")
        self.assertEqual(self.save.read_bytes(), new)
        self.assertEqual(int(self.save.stat().st_mtime), 1_700_000_000, "Zeitstempel bleibt erhalten")
        self.assertEqual((backup / "live" / "SaveData" / self.save.name).read_bytes(), original)
        self.assertEqual(bp.BioShockSave.load(self.save).infusion_levels()["health"], 4)
        # Gleiche Größe + gleicher Zeitstempel: „Sichern“ muss die Änderung trotzdem erkennen
        changed, _backup = self.mgr.save_current()
        self.assertTrue(changed)
        self.assertEqual((self.profiles / "Standard" / "SaveData" / self.save.name).read_bytes(), new)

    def test_write_refused_while_game_runs(self):
        new = bp.BioShockSave.load(self.save).with_infusion_levels({"health": 4})
        self.game_running = True
        with self.assertRaises(bp.GameRunningError):
            self.mgr.write_save_file("Standard", self.save, new, "Test")
        self.assertEqual(bp.BioShockSave.load(self.save).infusion_levels()["health"], 3)

    def test_write_outside_profile_refused(self):
        other = self.tmp / "fremd.sav"
        other.write_bytes(self.save.read_bytes())
        with self.assertRaises(bp.ProfileError):
            self.mgr.write_save_file("Standard", other, other.read_bytes(), "Test")


class TranslationTests(unittest.TestCase):
    """Jeder Oberflächentext braucht eine englische Übersetzung mit denselben Platzhaltern."""

    def _msgids(self):
        import ast
        tree = ast.parse(Path(bp.__file__).read_text(encoding="utf-8"))
        ids = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tr" and node.args:
                if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    ids.add(node.args[0].value)
        ids |= {bp.CLOUD_WARNING_TITLE, bp.CLOUD_WARNING_TEXT}
        ids |= {label for _k, _i, label in bp.INFUSION_STATS} | set(bp.STAT_EFFECTS.values())
        return {m for m in ids if any(ch.isalpha() for ch in m)}

    def test_every_text_has_english_translation(self):
        import re
        ids = self._msgids()
        self.assertGreater(len(ids), 200)
        self.assertEqual(sorted(ids - set(bp.EN)), [], "fehlende Übersetzungen")
        self.assertEqual(sorted(set(bp.EN) - ids), [], "ungenutzte Übersetzungen")
        ph = re.compile(r"%(?:\([a-z]+\))?[-#0 +]*\d*(?:\.\d+)?[sdifr%]")
        for de, en in bp.EN.items():
            self.assertEqual(sorted(ph.findall(de)), sorted(ph.findall(en)), de)

    def test_switching_language(self):
        try:
            bp.set_language("en")
            self.assertEqual(bp.tr("Spiel starten"), "Start game")
            self.assertEqual(bp.tr("Nicht übersetzter Text"), "Nicht übersetzter Text")
            bp.set_language("de")
            self.assertEqual(bp.tr("Spiel starten"), "Spiel starten")
        finally:
            bp.set_language("de")


class ImageTests(unittest.TestCase):
    """PNG-Funktionen für das Logo (ohne Tk)."""

    def test_png_roundtrip_and_resize(self):
        w, h = 40, 20
        px = bytearray(w * h * 4)
        for y in range(5, 15):           # rotes Rechteck auf transparentem Grund
            for x in range(10, 30):
                px[(y * w + x) * 4:(y * w + x) * 4 + 4] = b"\xc0\x30\x20\xff"
        png = bp.png_encode(w, h, px)
        w2, h2, px2 = bp.png_decode(png)
        self.assertEqual((w2, h2, bytes(px2)), (w, h, bytes(px)))
        box = bp.alpha_bbox(w, h, px)
        self.assertEqual(box, (10, 5, 30, 15))
        small = bp.resize_rgba(w, h, px, box, 10, 5)
        self.assertEqual(len(small), 10 * 5 * 4)
        self.assertEqual(bytes(small[:4]), b"\xc0\x30\x20\xff", "Farbe bleibt beim Verkleinern erhalten")

    def test_prepare_logo_uses_cache(self):
        tmp = Path(tempfile.mkdtemp(prefix="bsi_logo_"))
        try:
            w, h = 64, 36
            src = tmp / "logo.png"
            src.write_bytes(bp.png_encode(w, h, b"\x10\x20\x30\xff" * (w * h)))
            first = bp.prepare_logo(tmp / "cache", 18, source=src)
            self.assertEqual(bp.png_decode(first)[:2], (32, 18))
            self.assertEqual(len(list((tmp / "cache").glob("logo_*.png"))), 1)
            self.assertEqual(bp.prepare_logo(tmp / "cache", 18, source=src), first)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class DetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bsi_detect_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_steam_remote_and_prefers_newest(self):
        docs = self.tmp / "Docs"
        steam = self.tmp / "Steam"
        old = docs / "My Games" / "BioShock Infinite" / "SaveData" / "76561190000000000"
        write(old / "a.sav", "x")
        os.utime(old / "a.sav", (1_000_000_000, 1_000_000_000))
        remote = steam / "userdata" / "999" / "8870" / "remote"
        write(remote / "SaveData" / "b.sav", "y")
        (steam / "userdata" / "0").mkdir(parents=True)

        cands = bp.find_save_candidates(documents_dir=docs, steam_dirs=[steam])
        self.assertEqual({bp._norm(c.path) for c in cands}, {bp._norm(old), bp._norm(remote)})
        best = bp.choose_save_dir(cands)
        self.assertEqual(bp._norm(best.path), bp._norm(remote))
        self.assertTrue(bp.is_steam_cloud_dir(best.path))

    def test_nothing_found(self):
        self.assertIsNone(bp.choose_save_dir(bp.find_save_candidates(self.tmp / "leer", [])))


if __name__ == "__main__":
    unittest.main(verbosity=2)
