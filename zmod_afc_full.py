# Zmod AFC Full: the real AFC-Klipper-Add-On on a stock Zmod AD5X.
#
# Copyright (C) 2026 DIEHARDave
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# Zmod (ghzserg) ships its own AFC panel emulation, zmod_afc. This plugin
# replaces it with AFCProject's AFC-Klipper-Add-On (vendored in AFC/extras)
# and drives the IFS as an AFC unit (AFC_IFS.py), or another AFC unit such as
# a BoxTurtle in place of the IFS (AFC_FF.py). Zmod's own files stay
# untouched; what has to change in Zmod is changed at runtime:
#
# - zmod_afc stands down: its commands (SET_MAP, SET_SPOOL_ID, ...) are
#   unregistered before AFC registers its own, and its klippy:ready handler
#   (AFC panel objects) never runs. zmod_afc.enabled stays True so plugins
#   that check it (FF_NFC) send SET_SPOOL_ID, which AFC now handles.
# - zmod_color stops writing lane_data (AFC writes it).
# - The print's T<n>: Zmod's virtual_sdcard runs
#   _A_CHANGE_FILAMENT CHANNEL=n RESTORE_POSITION=1 for each T line. That
#   call is routed to AFC's T<n>; AFC_IFS runs Zmod's own _A_CHANGE_FILAMENT
#   for the actual swap. A pause raised before or during the change leaves the
#   T line unread (virtual_sdcard keeps the line when the command fails), so
#   the change runs again after RESUME.
# - zmod_ifs: SET_CURRENT_PRUTOK (first load at print start) goes through
#   AFC's T<n>; ANALOG_PRUTOK (head-sensor runout) goes to AFC_IFS (infinite
#   spool or AFC pause); IFS autoinsert waits for the G-code mutex.
#
# At every Klipper start this module also links the AFC modules next to
# itself in klippy/extras (so a plugin update that adds a module needs no
# reinstall) and writes mod_data/AFC/zmod_afc_full_active.cfg, which the
# plugin's cfg includes: the IFS config (plus IFS Jacker lanes), the unit
# config when mod_data/afc_unit.cfg exists, or nothing while the native screen
# is on. When that file changes, Klipper restarts once to load it.

import json
import logging
import os
import sys

ZMOD_AFC_COMMANDS = ('SET_COLOR', 'SET_MATERIAL', 'SET_MAP', 'SET_WEIGHT', 'CHANGE_TOOL',
                     'TOOL_UNLOAD', 'LANE_UNLOAD', 'AFC_RESET', 'RESET_FAILURE',
                     'AFC_CLEAR_MESSAGE', 'SET_SPOOL_ID')
OWN_MODULES = ('AFC_IFS.py', 'AFC_FF.py')
ACTIVE_NAME = 'zmod_afc_full_active.cfg'
MAX_LANES = 16
# Where the full-AFC test branch of Zmod (DIEHARDave/z_ad5x feat/full-afc) kept AFC
OLD_ZMOD_AFC = ('/usr/data/zmod/zmod/AFC/', '/usr/data/zmod/zmod/.shell/AFC_')


class ZmodAfcFull:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.name = config.get_name()
        self.restart = config.getboolean('restart_on_change', True)
        self.lang = 'en'
        self.zifs = self.color = self.zafc = self.sdcard = None
        self.orig_change = None
        self.routing = False        # inside our AFC T<n> for a print's T line

        start_cfg = self.printer.get_start_args().get('config_file', '/usr/data/config/printer.cfg')
        self.mod_data = os.path.join(os.path.dirname(os.path.abspath(start_cfg)), 'mod_data')
        self.active_cfg = os.path.join(self.mod_data, 'AFC', ACTIVE_NAME)
        self.unit_cfg = os.path.join(self.mod_data, 'afc_unit.cfg')

        here = os.path.abspath(__file__)
        self.extras_dir = os.path.dirname(here)
        self.plugin_dir = os.path.dirname(os.path.realpath(here))
        if self.plugin_dir == self.extras_dir:
            raise config.error("[%s]: zmod_afc_full.py must be a link into the plugin folder;"
                               " install it with ENABLE_PLUGIN name=zmod_afc_full" % self.name)

        save_variables = self.printer.load_object(config, 'save_variables')
        self.variables = dict(getattr(save_variables, 'allVariables', {}) or {})
        self.printer.register_event_handler('klippy:connect', self._connect)
        self.printer.register_event_handler('klippy:ready', self._ready)
        if config.has_section('zmod_afc') and hasattr(self.printer.load_object(config, 'zmod_afc'), 'full'):
            # Zmod's full-AFC test branch runs AFC itself: leave everything to it
            self.loaded = False
            self.mode, self.reason = 'off', (
                "Zmod is on the full-AFC test branch; switch it back to ghzserg's 1.7"
                " branch first (README: Moving from the test branch)")
            self.changed = self._write_active(self._active_text())
            return
        self._link_modules(config)
        self.loaded = config.has_section('AFC')
        self.mode, self.reason = self._wanted_mode(config)
        self.changed = self._write_active(self._active_text())
        if self.loaded:
            self._patch_zmod(config)

    # ---- install-time work done at every start ------------------------------

    def _link_modules(self, config):
        """Link AFC/extras/*.py, AFC_IFS.py and AFC_FF.py into klippy/extras."""
        afc_dir = os.path.join(self.plugin_dir, 'AFC', 'extras')
        if not os.path.isdir(afc_dir):
            raise config.error("[%s]: %s is missing; reinstall the plugin" % (self.name, afc_dir))
        sources = [os.path.join(afc_dir, f) for f in sorted(os.listdir(afc_dir))
                   if f.endswith('.py') and f != '__init__.py']
        sources += [os.path.join(self.plugin_dir, f) for f in OWN_MODULES]
        problems = []
        for src in sources:
            dst = os.path.join(self.extras_dir, os.path.basename(src))
            try:
                if os.path.islink(dst):
                    if os.path.realpath(dst) == os.path.realpath(src):
                        continue
                    cur = os.readlink(dst)
                    if (os.path.exists(dst) and not cur.startswith(OLD_ZMOD_AFC)
                            and 'zmod_afc_full' not in cur):
                        problems.append("%s links to %s" % (dst, cur))
                        continue
                    os.remove(dst)
                elif os.path.exists(dst):
                    problems.append("%s is a file, not this plugin's link" % dst)
                    continue
                os.symlink(src, dst)
                logging.info("zmod_afc_full: linked %s", dst)
            except OSError as e:
                problems.append("%s: %s" % (dst, e))
        # AFC dropped a module: remove our dangling link
        for f in os.listdir(self.extras_dir):
            dst = os.path.join(self.extras_dir, f)
            if (os.path.islink(dst) and not os.path.exists(dst)
                    and os.readlink(dst).startswith(self.plugin_dir + os.sep)):
                try:
                    os.remove(dst)
                except OSError:
                    pass
        if problems:
            raise config.error("[%s]: can't link the AFC modules into %s:\n  %s\n"
                               "Remove the other AFC install first." % (
                                   self.name, self.extras_dir, "\n  ".join(problems)))

    def _wanted_mode(self, config):
        """'ifs', 'unit' or 'off' (with the reason)."""
        if self.variables.get('display_off', 0) != 1:
            return 'off', "the native screen is on (full AFC needs DISPLAY_OFF)"
        if config.has_section('zmod_afcplus'):
            return 'off', "zmod_afcplus is enabled (DISABLE_PLUGIN name=zmod_afcplus)"
        if not config.has_section('zmod_afc'):
            return 'off', "this Zmod has no [zmod_afc] (AD5X with Zmod 1.7.5 or newer needed)"
        if not config.has_section('zmod_ifs'):
            return 'off', "no [zmod_ifs] in the config (AD5X only)"
        if os.path.exists(self.unit_cfg):
            return 'unit', "mod_data/afc_unit.cfg"
        return 'ifs', "IFS"

    def _lanes(self):
        """IFS Jacker: lanes 5..N. AFC_IFS saves the slot count it sees as afc_lanes."""
        try:
            n = int(self.variables.get('afc_lanes', 4))
        except (TypeError, ValueError):
            n = 4
        n = min(n, MAX_LANES)
        plugins_cfg = os.path.join(self.mod_data, 'plugins.cfg')
        try:
            with open(plugins_cfg) as f:
                jacker = any(l.startswith('[include plugins/ifs_jacker/') for l in f)
        except OSError:
            jacker = False
        return n if jacker and n > 4 else 4

    def _active_text(self):
        head = ("# Written by zmod_afc_full.py at every Klipper start. Don't edit: put your\n"
                "# changes in mod_data/user.cfg, they win.\n")
        if self.mode == 'off':
            return head + "# Full AFC is off: %s\n" % self.reason
        if self.mode == 'unit':
            return head + "[include %s]\n[include %s]\n" % (
                os.path.join(self.plugin_dir, 'ad5x_afc_ff.cfg'), self.unit_cfg)
        text = head + "[include %s]\n" % os.path.join(self.plugin_dir, 'ad5x_afc.cfg')
        n = self._lanes()
        if n > 4:
            text += ("\n# IFS Jacker: slots 5-%d (afc_lanes = %d in variables.cfg)\n"
                     "[zmod_ifs]\ncolor_limit: %d\n" % (n, n, n))
            for i in range(5, n + 1):
                text += "\n[AFC_lane lane%d]\nunit: IFS:%d\nmap: T%d\n" % (i, i, i - 1)
        return text

    def _write_active(self, text):
        """Write the active cfg; True when its content changed."""
        try:
            with open(self.active_cfg) as f:
                if f.read() == text:
                    return False
        except OSError:
            pass
        try:
            os.makedirs(os.path.dirname(self.active_cfg), exist_ok=True)
            tmp = self.active_cfg + '.tmp'
            with open(tmp, 'w') as f:
                f.write(text)
            os.replace(tmp, self.active_cfg)
            with open(self.active_cfg) as f:
                return f.read() == text
        except OSError:
            logging.exception("zmod_afc_full: can't write %s", self.active_cfg)
            return False

    # ---- runtime changes to Zmod ---------------------------------------------

    def _patch_zmod(self, config):
        self.zafc = self.printer.load_object(config, 'zmod_afc')
        for cmd in ZMOD_AFC_COMMANDS:
            self.gcode.register_command(cmd, None)
        handlers = self.printer.event_handlers.get('klippy:ready', [])
        self.printer.event_handlers['klippy:ready'] = [
            h for h in handlers if getattr(h, '__self__', None) is not self.zafc]
        self.zafc.full = True
        self.zafc.enabled = True

        self.color = self.printer.load_object(config, 'zmod_color')
        if hasattr(self.color, '_publish_lanes'):
            self.color._publish_lanes = lambda slots: None

        self.zifs = self.printer.load_object(config, 'zmod_ifs')
        self.orig_set_current = self.zifs.cmd_SET_CURRENT_PRUTOK
        self.orig_analog = self.zifs.cmd_ANALOG_PRUTOK
        for cmd, func in (('SET_CURRENT_PRUTOK', self.cmd_SET_CURRENT_PRUTOK),
                          ('ANALOG_PRUTOK', self.cmd_ANALOG_PRUTOK)):
            self.gcode.register_command(cmd, None)
            self.gcode.register_command(cmd, func)
        self.zifs._safe_run_script = self._safe_run_script
        self.file_config = getattr(sys.modules.get(type(self.zifs).__module__), 'FILE_CONFIG',
                                   os.path.join(self.mod_data, 'file.json'))

    def _connect(self):
        if not self.loaded:
            return
        self.sdcard = self.printer.lookup_object('virtual_sdcard', None)
        macro = self.printer.lookup_object('gcode_macro _A_CHANGE_FILAMENT', None)
        if macro is None:
            logging.error("zmod_afc_full: no _A_CHANGE_FILAMENT macro, print T<n> stay with Zmod")
            return
        self.orig_change = macro.cmd
        self.gcode.register_command('_A_CHANGE_FILAMENT', None)
        self.gcode.register_command('_A_CHANGE_FILAMENT', self.cmd_A_CHANGE_FILAMENT,
                                    desc=getattr(macro, 'cmd_desc', None))

    def _ready(self):
        if self.zifs is not None:
            self.lang = getattr(self.zifs, 'lang', 'en')
        if self.mode == 'off':
            msg = "Zmod AFC Full: full AFC is off: %s" % self.reason
            logging.info(msg)
            if self.loaded or self.changed:
                self.gcode.respond_info(msg)
        if not self.changed:
            return
        logging.info("zmod_afc_full: %s changed (mode %s)", self.active_cfg, self.mode)
        if self.restart and self.variables.get('display_off', 0) == 1:
            self.gcode.respond_info("Zmod AFC Full: AFC config switched to '%s', restarting Klipper"
                                    % self.mode)
            self.reactor.register_callback(lambda e: self.gcode.run_script("RESTART"),
                                           self.reactor.monotonic() + 1.)
        else:
            self.gcode.respond_info("Zmod AFC Full: AFC config switched to '%s', restart Klipper"
                                    % self.mode)

    def get_status(self, eventtime):
        return {'mode': self.mode, 'reason': self.reason, 'loaded': self.loaded,
                'restart_pending': self.changed}

    # ---- the commands -------------------------------------------------------

    def _ru(self, ru, en):
        return ru if self.lang == 'ru' else en

    def cmd_A_CHANGE_FILAMENT(self, gcmd):
        """A print's T<n> line (virtual_sdcard) -> AFC's T<n>; all else to Zmod's macro."""
        sd = self.sdcard
        if (self.routing or sd is None or not getattr(sd, 'change_filament', False)
                or gcmd.get_int('RESTORE_POSITION', 0) != 1):
            return self.orig_change(gcmd)
        channel = gcmd.get_int('CHANNEL')
        if sd.must_pause_work:
            # Paused during the M400 before the change: keep the T line, change after RESUME
            raise gcmd.error(self._ru("Пауза перед сменой T%d: смена будет после RESUME" % channel,
                                      "Paused before T%d: the change runs after RESUME" % channel))
        self.routing = True
        try:
            self.gcode.run_script_from_command("_AFC_IFS_SYNC")
            self.gcode.run_script_from_command("T%d" % channel)
        finally:
            self.routing = False
        sd.change_filament = False
        if sd.must_pause_work:
            # AFC paused (it doesn't raise): keep the T line, the change repeats after RESUME
            raise gcmd.error(self._ru("AFC встал на паузу при смене T%d: смена повторится после RESUME"
                                      % channel,
                                      "AFC paused during T%d: the change runs again after RESUME"
                                      % channel))

    def _tool_in_head(self, tool):
        """The filament of tool T<n> (slot per Zmod's tool map) is in the extruder."""
        z = self.zifs
        if not z.get_extruder_sensor():
            return False
        slot = z.get_current_channel_from_config()
        if not z.get_port(slot):
            return False
        try:
            with open(self.file_config) as f:
                return json.load(f)[tool] == slot
        except Exception:
            return False

    def cmd_SET_CURRENT_PRUTOK(self, gcmd):
        """Print start: load the first tool with AFC's T<n> (a loaded lane doesn't move)."""
        z = self.zifs
        if not z.ifs:
            return self.orig_set_current(gcmd)
        cur = 99
        if z.get_extruder_sensor():
            slot = z.get_current_channel_from_config()
            if z.get_port(slot):
                try:
                    with open(self.file_config) as f:
                        cur = json.load(f).index(slot)
                except Exception:
                    cur = 98
        channel = gcmd.get_int('CHANNEL', cur)
        if channel != cur:
            afc = self.printer.lookup_object('AFC', None)
            if afc is not None and not all(getattr(l, '_afc_prep_done', True)
                                           for l in afc.lanes.values()):
                # PREP runs ~15 s after start; T<n> before it would leave AFC in a tool change
                raise gcmd.error(self._ru("AFC ещё запускается (PREP), повторите печать через несколько секунд",
                                          "AFC is still starting (PREP), start the print again in a few seconds"))
            self.gcode.run_script_from_command("_AFC_IFS_SYNC")
            self.gcode.run_script_from_command("T%d" % channel)
            # AFC only reports a load error outside a print: don't start with the wrong filament
            if not self._tool_in_head(channel):
                raise gcmd.error(self._ru("Не удалось загрузить T%d, печать отменена" % channel,
                                          "Failed to load T%d, print not started" % channel))
        else:
            z.print_str(self._ru("Указываю активный пруток T%d" % cur,
                                 "Setting active filament T%d" % cur))
        self.gcode.run_script_from_command("SDCARD_SET_CHANNEL CHANNEL=%d" % channel)
        z.print_str(self._ru("Включаю IFS", "Enable IFS"))
        self.gcode.run_script_from_command("SDCARD_ENABLE_FFM ENABLE=1")

    def cmd_ANALOG_PRUTOK(self, gcmd):
        """Head-sensor runout: the IFS slot flag stays set until unload, so only this
        sees the end of the filament. The pause is already set; AFC_IFS runs the
        infinite spool (runout_lane) or pauses with an AFC message."""
        if not self.zifs.ifs:
            return self.orig_analog(gcmd)
        self.gcode.run_script_from_command("_PRINT_HEAD INFO=0")
        self.gcode.run_script_from_command("_AFC_IFS_HEAD_RUNOUT")

    def _safe_run_script(self, script):
        # From a reactor callback (sensor thread): run_script takes the G-code mutex,
        # so autoinsert (_IFS_AUTOINSERT) can't run during a load/unload/change and
        # share the IFS command slot with it ("Timeout waiting for response")
        try:
            self.gcode.run_script(script)
        except self.gcode.error:
            pass


def load_config(config):
    return ZmodAfcFull(config)
