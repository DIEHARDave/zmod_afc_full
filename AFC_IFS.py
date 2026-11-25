# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# AFC unit for the Flashforge IFS (the AD5X's 4-slot filament system, and the
# IFS Jacker that chains up to four of them as slots 1-16).
#
# The IFS moves filament with its own firmware, driven by the printer's
# Klipper firmware: Zmod (ghzserg/zmod) or Kiln OS (djvalic/kiln-os). This
# unit hands AFC's load, unload and eject to that firmware's commands through
# the unit_load_lane / unit_unload_lane hooks, and feeds the lane sensors from
# the firmware's slot state. The firmware's slot in the toolhead is the truth
# for which lane is loaded: a change made outside AFC (printer screen,
# firmware menu, auto-refill) is mirrored into AFC.
#
# Everything firmware specific lives in a backend (ZmodBackend, KilnBackend),
# picked with `firmware: auto|zmod|kiln`.

from __future__ import annotations

import json
import os
import re
import time
import traceback
from configparser import Error as error
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from configfile import ConfigWrapper
    from gcode import GCodeCommand
    from extras.AFC_lane import AFCLane

try: from extras.AFC_utils import ERROR_STR
except:
    error_str = f"Error when trying to import AFC_utils.ERROR_STR\n{traceback.format_exc()}"
    raise error(error_str)

try: from extras.AFC_lane import AFCLaneState
except:
    error_str = ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc())
    raise error(error_str)

try: from extras.AFC_unit import afcUnit
except:
    error_str = ERROR_STR.format(import_lib="AFC_unit", trace=traceback.format_exc())
    raise error(error_str)

try: import extras.AFC_lane as afc_lane_module
except:
    error_str = ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc())
    raise error(error_str)

try: import extras.AFC_unit as afc_unit_module
except:
    error_str = ERROR_STR.format(import_lib="AFC_unit", trace=traceback.format_exc())
    raise error(error_str)

IFS_TYPE = "IFS"
MAX_SLOTS = 16          # IFS Jacker: up to four IFS units
SPOOL_WAIT = 10.0       # seconds AFC gets to fetch a spool after SET_SPOOL_ID
CONNECT_WAIT = 30.0     # seconds PREP waits for the IFS after a start
METADATA_TRIES = 10     # Zmod: seconds to wait for a print file's metadata
PREP_SAMPLE_HOLD = 5.0  # seconds one PREP presence sample serves all lanes

# One sensor per lane and no lane motor, like OpenAMS
for _type_list in (afc_lane_module.ONLY_LOAD_TYPES, afc_lane_module.EXCLUDE_TYPES,
                   afc_unit_module.SENSORLESS_UNITS):
    if IFS_TYPE not in _type_list:
        _type_list.append(IFS_TYPE)

HEX_RE = re.compile(r"^#?([0-9A-Fa-f]{6})")

# Material names Kiln's IFS_SET_MATERIAL accepts (its parse_material)
KILN_MATERIAL_RE = re.compile(r"^[A-Za-z0-9+_.-]{1,16}$")

# Written next to AFC's VarFile by Kiln's kiln_afc plugin when it replaces
# afclite: the lane spools afclite had ({"slots": {"1": 12, ...}}). Read once.
SEED_SUFFIX = ".afclite-spools.json"

SlotState = Dict[str, Any]
Material = Tuple[str, str]


def color_hex(value: Any) -> str:
    """
    Normalize a color to `#RRGGBB`.

    :param value: AFC's or the firmware's color, with or without `#`
    :return str: Upper case `#RRGGBB`, or an empty string when not a color
    """
    match = HEX_RE.match(str(value or "").strip())
    return f"#{match.group(1).upper()}" if match else ""


def spool_id(value: Any) -> Optional[int]:
    """
    Normalize a Spoolman spool ID.

    :param value: Spool ID as int, string or None
    :return Optional[int]: The ID when it is a positive integer, else None
    """
    try:
        sid = int(value)
    except (TypeError, ValueError):
        return None
    return sid if sid > 0 else None


def tool_number(tcmd: Any) -> Optional[int]:
    """
    Tool number of a tool command.

    :param tcmd: Tool command such as `T3`
    :return Optional[int]: 3 for `T3`, None when it is not a tool command
    """
    text = str(tcmd or "")
    return int(text[1:]) if text[:1] == "T" and text[1:].isdigit() else None


class IFSBackend:
    """
    What the IFS unit needs from the firmware that drives the IFS.

    `status()` returns `connected`, `busy`, `head_slot` (slot in the toolhead,
    0 for none), `head_sensor` (toolhead filament sensor, None when unknown)
    and `slots` ({slot: {present, type, color, temp}}, type None when the
    firmware keeps no material for the slot).
    """

    name = ""
    load_cmd = ""
    unload_cmd = ""
    eject_cmd = ""
    swap_cmd = ""
    empty_polls = 3          # readings before AFC hears a slot went empty
    prep_sample = 0.0        # seconds PREP samples the slots
    afc_runout = True        # AFC's runout path from the slot sensor
    keep_temp = False        # put the nozzle target back after an idle move
    calibration_hint = ""
    # A runout waits for the strand's end to pass the toolhead sensor
    # (the IFS can't pull back a tail that has left its slot)
    waits_for_tail = False

    def __init__(self, unit: afcIFS):
        """
        Keep a reference to the unit the backend serves.

        :param unit: IFS unit
        """
        self.unit = unit

    def connect(self) -> None:
        """
        Look up the firmware's objects at klippy:connect.
        """

    def status(self, eventtime: float) -> Dict[str, Any]:
        """
        Read the firmware's IFS state.

        :param eventtime: Reactor event time
        :return Dict[str, Any]: Normalized state, empty when unreadable
        """
        return {}

    def push_material(self, lane: AFCLane, slot: int, mtype: str,
                      color: str) -> Optional[Material]:
        """
        Write AFC's material and color to the firmware's slot.

        :param lane: Lane the material belongs to
        :param slot: IFS slot
        :param mtype: Material type
        :param color: Color as `#RRGGBB` or empty
        :return Optional[Material]: (type, color) the firmware will report, None
                                    when nothing was written
        """
        return None

    def early_poll(self, st: Dict[str, Any], eventtime: float) -> None:
        """
        Poll work that runs even while the firmware is busy.

        :param st: Current status
        :param eventtime: Reactor event time
        """

    def late_poll(self, st: Dict[str, Any], eventtime: float) -> None:
        """
        Poll work that runs after the lane sensors are updated.

        :param st: Current status
        :param eventtime: Reactor event time
        """

    def fold_unload(self, cur_lane: AFCLane, next_lane: str) -> bool:
        """
        Whether the next load does this unload as part of one change.

        :param cur_lane: Lane being unloaded
        :param next_lane: Name of the lane AFC loads next
        :return bool: True when the unload is left to the next load
        """
        return False

    def load(self, lane: AFCLane, swap: bool) -> Tuple[bool, str]:
        """
        Load a lane into the toolhead.

        :param lane: Lane to load
        :param swap: True when the previous lane's unload was folded into this load
        :return Tuple[bool, str]: Success and error message
        """
        return self.unit.run_cmd(self.unit.load_cmd, lane, keep_temp=self.unit.keep_temp)

    def can_eject_in_print(self) -> bool:
        """
        Whether an idle lane can be ejected beside a running print, without
        holding up the print's G-code.

        :return bool: True when `eject_in_print` is available
        """
        return False

    def follow_runout(self, old_slot: int, change: AFCLane) -> None:
        """
        After an infinite-spool change, point the firmware's tools for the
        empty slot at the new lane's slot.

        :param old_slot: Slot that ran out
        :param change: Lane the print changed to
        """

    def backup_slots(self, slot: int) -> List[int]:
        """
        Slots with the same filament as a slot, best first: same material and
        color in AFC's lanes.

        :param slot: Slot that ran out
        :return List[int]: Backup slots
        """
        unit = self.unit
        lane = unit.lane_for_slot(slot)
        if lane is None or not lane.material or not lane.color:
            return []
        want = (str(lane.material).lower(), str(lane.color).lower())
        return [unit.slot_of(other) for other in unit.lanes.values()
                if other is not lane
                and (str(other.material).lower(), str(other.color).lower()) == want]

    def stand_down_refill(self) -> None:
        """
        Keep the firmware's own runout refill from acting while AFC changes lanes.
        """

    def eject_in_print(self, slot: int) -> Tuple[bool, str]:
        """
        Eject an idle slot from a reactor callback while a print runs.

        :param slot: IFS slot to eject
        :return Tuple[bool, str]: Success and error message
        """
        return False, "not supported by this firmware"

    def eject(self, lane: AFCLane) -> Tuple[bool, str]:
        """
        Retract a lane's filament to its IFS slot.

        :param lane: Lane to eject
        :return Tuple[bool, str]: Success and error message
        """
        return self.unit.run_cmd(self.unit.eject_cmd, lane, keep_temp=self.unit.keep_temp)


class ZmodBackend(IFSBackend):
    """
    Zmod: zmod_ifs and zmod_color drive the IFS.

    Mid-print changes run Zmod's own `_A_CHANGE_FILAMENT` (z-lift, flush,
    temperature and fan restore) with the slot AFC chose written into Zmod's
    tool map (FILE_CONFIG). Zmod's virtual_sdcard routes a print's T<n> lines
    to AFC (`_AFC_IFS_SYNC`, then `T<n>`) and its head sensor runout calls
    `_AFC_IFS_HEAD_RUNOUT`.
    """

    name = "zmod"
    load_cmd = "INSERT_PRUTOK_IFS PRUTOK={slot} TRASH=1"
    # Back the filament out of the extruder into the tube, then unlock the IFS
    unload_cmd = "IFS_REMOVE_CURRENT_PRUTOK NEED_TRASH=1\nIFS_F18"
    # Full retract to the slot so the spool can come off
    eject_cmd = "REMOVE_PRUTOK_IFS PRUTOK={slot}"
    swap_cmd = ("_A_CHANGE_FILAMENT CHANNEL={tool} RESTORE_POSITION={restore}"
                " RESTORE_TEMP={restore}")
    # The slot flag stays set for a while after a spool is pulled
    empty_polls = 10
    # The IFS reads slots empty for seconds after a start
    prep_sample = 8.0
    # Runout comes from the head sensor, see cmd_AFC_IFS_HEAD_RUNOUT
    afc_runout = False
    # Zmod heats for the slot's material and leaves the nozzle hot
    keep_temp = True

    def __init__(self, unit: afcIFS):
        """
        Set up Zmod state.

        :param unit: IFS unit
        """
        super().__init__(unit)
        self.color: Any = None
        self.zifs: Any = None
        self.sdcard: Any = None
        self.save_variables: Any = None
        self.atomic_write: Optional[Callable[[str, str], None]] = None
        self.file_config: Optional[str] = None
        self.map_mtime: Optional[float] = None
        self.map_agreed: Optional[List[int]] = None
        self.printing = False
        self.seeded = False
        self.runout_tool: Optional[int] = None
        self.slot_count = 0

    def connect(self) -> None:
        """
        Look up Zmod's objects and register the commands Zmod calls.
        """
        unit = self.unit
        printer = unit.printer
        self.color = printer.lookup_object("zmod_color")
        self.zifs = printer.lookup_object("zmod_ifs", None)
        self.sdcard = printer.lookup_object("virtual_sdcard", None)
        self.save_variables = printer.lookup_object("save_variables", None)
        try:
            import extras.zmod_color as zmod_color
            self.atomic_write = zmod_color.atomic_write
            self.file_config = zmod_color.FILE_CONFIG
        except Exception:
            error_str = (f"[AFC_IFS {unit.name}]: Zmod's zmod_color module not found\n"
                         f"{traceback.format_exc()}")
            raise error(error_str)
        unit.gcode.register_command("_AFC_IFS_SYNC", self.cmd_AFC_IFS_SYNC,
                                    desc=self.cmd_AFC_IFS_SYNC_help)
        unit.gcode.register_command("_AFC_IFS_HEAD_RUNOUT", self.cmd_AFC_IFS_HEAD_RUNOUT,
                                    desc=self.cmd_AFC_IFS_HEAD_RUNOUT_help)
        self.count_preloaded_tool()
        patch_metadata_retry()

    def drop_runout_macro(self) -> None:
        """
        Remove Zmod's SET_RUNOUT macro, its AFC panel stub, which blocks AFC's command.
        """
        gcode = self.unit.gcode
        if "SET_RUNOUT" not in getattr(gcode, "mux_commands", {}):
            gcode.register_command("SET_RUNOUT", None)

    def count_preloaded_tool(self) -> None:
        """
        Count Zmod's first tool load as AFC's.

        Zmod loads the first tool before the print starts and virtual_sdcard
        skips the T<n> of a loaded tool, so AFC would count changes from -1.
        """
        afc = self.unit.afc
        if getattr(afc, "_ifs_count_wrapped", False):
            return
        orig = afc._finish_print_start

        def finish(*args, **kwargs):
            orig(*args, **kwargs)
            if afc.current is not None and afc.current_toolchange == -1:
                afc.current_toolchange = 0
        afc._finish_print_start = finish
        afc._ifs_count_wrapped = True

    def status(self, eventtime: float) -> Dict[str, Any]:
        """
        Read the IFS state from zmod_color.

        :param eventtime: Reactor event time
        :return Dict[str, Any]: Normalized state, empty when unreadable
        """
        try:
            st = self.color.get_status(eventtime)
        except Exception:
            self.unit.logger.debug(f"AFC_IFS: zmod_color.get_status failed\n"
                                   f"{traceback.format_exc()}")
            return {}
        # zmod_ifs holds the IFS link; zmod_color.ifs is set only after the color dialog
        if self.zifs is not None:
            connected = bool(getattr(self.zifs, "ifs", False))
        else:
            connected = bool(st.get("ifs"))
        slots: Dict[int, SlotState] = {}
        for entry in st.get("slots") or []:
            try:
                slot = int(entry["ID"])
            except (KeyError, TypeError, ValueError):
                continue
            mat = entry.get("Material") or ""
            slots[slot] = {"present": bool(entry.get("hasFilament")),
                           "type": "" if mat in ("", "?") else mat,
                           "color": color_hex(entry.get("HEX")),
                           "temp": None}
        head = bool(st.get("extruder_sensor"))
        channel = int(st.get("channel") or 0)
        # FFMInfo's channel stays set after an unload: only trust it with the sensor
        return {"connected": connected, "busy": False, "slots": slots,
                "head_slot": channel if head else 0, "head_sensor": head,
                "channel": channel}

    def zmod_type(self, mat: str) -> Optional[str]:
        """
        Zmod material type for an AFC material.

        :param mat: AFC material such as `PLA+`
        :return Optional[str]: Zmod type such as `PLA`, None when Zmod has no match
        """
        types = list(self.color.valid_types[:-1])
        mat = (mat or "").upper()
        if mat in types:
            return mat
        return next((t for t in sorted(types, key=len, reverse=True) if t and t in mat), None)

    def push_material(self, lane: AFCLane, slot: int, mtype: str,
                      color: str) -> Optional[Material]:
        """
        Set the Zmod slot's type and color.

        :param lane: Lane the material belongs to
        :param slot: IFS slot
        :param mtype: Material type
        :param color: Color as `#RRGGBB` or empty
        :return Optional[Material]: (type, color) Zmod will report
        """
        current = self.unit.last_status.get("slots", {}).get(slot, {})
        zmat = self.zmod_type(mtype) or current.get("type") or "PLA"
        zhex = color or current.get("color") or "#FFFFFF"
        unit = self.unit

        def run(eventtime):
            try:
                code, resp = self.color.set_printer_data_detail(slot, zmat, zhex)
            except Exception as e:
                unit.logger.info(f"AFC_IFS: slot {slot} {zmat} {zhex}: {e}")
                return
            if code != 200:
                unit.logger.info(f"AFC_IFS: slot {slot} {zmat} {zhex}: {resp}")
        unit.reactor.register_callback(run)
        return zmat, zhex

    def late_poll(self, st: Dict[str, Any], eventtime: float) -> None:
        """
        Reset the tool map after a print, sync it and take over the built-in AFC's spools.

        :param st: Current status
        :param eventtime: Reactor event time
        """
        unit = self.unit
        state = unit.printer.lookup_object("print_stats").get_status(eventtime)["state"]
        printing = state in ("printing", "paused")
        if (unit.idle_map
            and self.printing
            and not printing):
            self.reset_map()
        self.printing = printing
        if unit.sync_map:
            self.sync_map()
        self.seed_spools()
        self.save_slot_count(st)

    def save_slot_count(self, st: Dict[str, Any]) -> None:
        """
        Save the IFS's slot count as `afc_lanes` for Zmod's fix_config.sh,
        which creates lane5-laneN for an IFS Jacker at the next boot, and say
        once when the IFS has more slots than AFC has lanes.

        The count is the Jacker's `channel_count` from the IFS status, else
        the slots zmod_color reports.

        :param st: Current status
        """
        count = 0
        ifs_data = getattr(self.zifs, "ifs_data", None)
        match = re.search(r"channel_count:\s*(\d+)", getattr(ifs_data, "lastResponseRaw", "") or "")
        if match:
            count = int(match.group(1))
        else:
            count = len(st.get("slots") or {})
        count = max(0, min(MAX_SLOTS, count))
        if (not count
            or count == self.slot_count):
            return
        self.slot_count = count
        unit = self.unit
        if (self.save_variables is not None
            and self.save_variables.allVariables.get("afc_lanes") != count):
            unit.run_later(f"SAVE_VARIABLE VARIABLE=afc_lanes VALUE={count}")
        missing = [s for s in range(1, count + 1) if unit.lane_for_slot(s) is None]
        if missing:
            first, last = missing[0], missing[-1]
            lanes = f"lane{first}" if first == last else f"lane{first}-lane{last}"
            unit.logger.info(f"AFC_IFS: the IFS has {count} slots, AFC has no lane for slot"
                             f" {', '.join(str(s) for s in missing)}. Reboot the printer: Zmod"
                             f" adds {lanes} at start.")

    def read_map(self) -> Tuple[Optional[float], Optional[List[int]]]:
        """
        Read Zmod's tool map, T(n) -> slot.

        :return Tuple[Optional[float], Optional[List[int]]]: mtime and map, Nones when unreadable
        """
        try:
            mtime = os.stat(self.file_config).st_mtime
            with open(self.file_config, "r") as f:
                return mtime, [int(x) for x in json.load(f)]
        except Exception:
            return None, None

    def write_map(self, fmap: List[int]) -> None:
        """
        Write Zmod's tool map and remember it as agreed.

        :param fmap: Tool map, T(n) -> slot
        """
        self.atomic_write(self.file_config, json.dumps(fmap, indent=2))
        self.map_mtime, self.map_agreed = self.read_map()

    def afc_map(self, length: int) -> List[Optional[int]]:
        """
        Zmod tool map from AFC's: each T(n) to its lane's slot.

        :param length: Number of tools
        :return List[Optional[int]]: Slots, None for a T(n) on no IFS lane
        """
        unit = self.unit
        out: List[Optional[int]] = []
        for tool in range(length):
            lane = unit.lanes.get(unit.afc.tool_cmds.get(f"T{tool}", ""))
            out.append(unit.slot_of(lane) if lane is not None else None)
        return out

    def sync_map(self, run: Optional[Callable[[str], None]] = None) -> None:
        """
        Keep Zmod's tool map and AFC's in step, both ways.

        :param run: Runs a G-code line; the timer's run_script when None
        """
        unit = self.unit
        if run is None and unit.gcode.get_mutex().test():
            # G-code is busy (print start): a run_script would queue behind it.
            # _AFC_IFS_SYNC or the next poll does it.
            return
        mtime, fmap = self.read_map()
        if fmap is None:
            return
        tools = [t for t in (tool_number(k) for k in unit.afc.tool_cmds) if t is not None]
        length = max([len(fmap)] + [t + 1 for t in tools])
        merged = [a if a is not None else (fmap[t] if t < len(fmap) else None)
                  for t, a in enumerate(self.afc_map(length))]
        if (mtime != self.map_mtime
            or self.map_agreed is None):
            # The file changed (color dialog, ANALOG_PRUTOK), or first look
            self.map_mtime = mtime
            self.map_agreed = list(fmap)
            self.apply_file_map(fmap, run or unit.gcode.run_script)
        elif merged != fmap:
            # Changed in AFC: SET_MAP, panels, infinite spool
            count = max(len(unit.slots_by_lane), 1)
            self.write_map([m if m is not None else (t % count) + 1
                            for t, m in enumerate(merged)])

    def reset_map(self) -> None:
        """
        Put Zmod's tool map back to T(n) on slot n+1 after a print. The next
        sync_map takes the file as new, so AFC's lane maps follow it.
        """
        count = max(len(self.unit.slots_by_lane), 1)
        _, fmap = self.read_map()
        new = [(t % count) + 1 for t in range(max(count, len(fmap or [])))]
        if fmap != new:
            self.unit.logger.info(f"AFC_IFS: print ended, tool map back to {new}")
            self.write_map(new)
        self.map_agreed = None

    def apply_file_map(self, fmap: List[int], run: Callable[[str], None]) -> None:
        """
        Map AFC's tools like Zmod's tool map.

        :param fmap: Zmod tool map, T(n) -> slot
        :param run: Runs a G-code line
        """
        unit = self.unit
        if not getattr(unit.afc, "enable_multiple_mapping", False):
            unit.logger.info("AFC_IFS: sync_map needs [AFC] enable_multiple_mapping: True")
            return
        cmds = []
        for tool, slot in enumerate(fmap):
            lane = unit.lane_for_slot(slot)
            tcmd = f"T{tool}"
            if (lane is None
                or unit.afc.tool_cmds.get(tcmd) == lane.name):
                continue
            if tcmd in unit.afc.tool_cmds:
                cmds.append(f"SET_MAP LANE={lane.name} MAP={tcmd}")
            else:
                cmds.append(f"AFC_ADD_MAPPING LANE={lane.name} MAPPING={tcmd}")
        if cmds:
            unit.logger.info(f"AFC_IFS: following Zmod's tool map: {', '.join(cmds)}")
        for cmd in cmds:
            try:
                run(cmd)
            except Exception as e:
                unit.logger.info(f"AFC_IFS: {cmd} failed: {e}")

    cmd_AFC_IFS_SYNC_help = "AFC_IFS: carry Zmod's tool map (FILE_CONFIG) into AFC now"

    def cmd_AFC_IFS_SYNC(self, gcmd: GCodeCommand) -> None:
        """
        Carry Zmod's tool map into AFC now. Zmod's virtual_sdcard runs this
        before a print's T<n> lines and the first load.

        Usage
        -------
        `_AFC_IFS_SYNC`

        Example
        -------
        ```
        _AFC_IFS_SYNC
        ```
        """
        if (self.unit.prepped()
            and self.unit.sync_map):
            self.sync_map(self.unit.gcode.run_script_from_command)

    def set_file_tool(self, tool: int, slot: int) -> None:
        """
        Point a tool at a slot in Zmod's tool map, which Zmod's change reads.

        :param tool: Tool number
        :param slot: IFS slot
        """
        _, fmap = self.read_map()
        fmap = list(fmap or [])
        count = max(len(self.unit.slots_by_lane), 1)
        while len(fmap) <= tool:
            fmap.append((len(fmap) % count) + 1)
        if fmap[tool] != slot:
            fmap[tool] = slot
            self.atomic_write(self.file_config, json.dumps(fmap, indent=2))
        self.map_mtime, self.map_agreed = self.read_map()

    def seed_spools(self) -> None:
        """
        Once, give AFC's lanes the spools Zmod's built-in AFC had.
        """
        unit = self.unit
        if (self.seeded
            or getattr(unit.afc, "spoolman", None) is None
            or self.save_variables is None):
            return
        self.seeded = True
        variables = self.save_variables.allVariables
        if variables.get("afc_full_seeded", 0):
            return
        scripts = []
        for lane in unit.lanes.values():
            sid = spool_id(variables.get(f"afc_spool_lane{unit.slot_of(lane)}"))
            if (sid is not None
                and spool_id(lane.spool_id) is None
                and lane.prep_state):
                unit.logger.info(f"AFC_IFS: {lane.name} keeps spool {sid} from Zmod's AFC")
                scripts.append(f"SET_SPOOL_ID LANE={lane.name} SPOOL_ID={sid}")
        scripts.append("SAVE_VARIABLE VARIABLE=afc_full_seeded VALUE=1")
        unit.run_later(*scripts)

    def tool_for(self, lane: AFCLane) -> int:
        """
        Print tool a lane is loaded for.

        :param lane: Lane being loaded
        :return int: Tool number
        """
        if self.runout_tool is not None:
            return self.runout_tool
        tool = tool_number(getattr(lane, "current_map", ""))
        if tool is not None:
            return tool
        for tcmd in lane.map or []:
            tool = tool_number(tcmd)
            if tool is not None:
                return tool
        return self.unit.slot_of(lane) - 1

    def fold_unload(self, cur_lane: AFCLane, next_lane: str) -> bool:
        """
        Zmod's change and load take the old filament out themselves.

        :param cur_lane: Lane being unloaded
        :param next_lane: Name of the lane AFC loads next
        :return bool: True during an AFC tool change
        """
        return bool(self.unit.afc.in_toolchange)

    def load(self, lane: AFCLane, swap: bool) -> Tuple[bool, str]:
        """
        Load with Zmod's load outside a print, with Zmod's change in a print.

        :param lane: Lane to load
        :param swap: Unused, Zmod's change always takes the old filament out
        :return Tuple[bool, str]: Success and error message
        """
        unit = self.unit
        if not unit.afc.function.in_print():
            return unit.run_cmd(unit.load_cmd, lane, keep_temp=unit.keep_temp)
        tool = self.tool_for(lane)
        self.set_file_tool(tool, unit.slot_of(lane))
        # A print's T<n> restores position and temperature; the first load
        # at print start doesn't
        restore = 1 if getattr(self.sdcard, "change_filament", False) else 0
        return unit.run_cmd(unit.swap_cmd, lane, tool=tool, restore=restore)

    def eject(self, lane: AFCLane) -> Tuple[bool, str]:
        """
        Eject a lane. Zmod's REMOVE_PRUTOK_IFS always unloads the head first,
        so a lane that isn't in the head is retracted on its own: clamp,
        retract the tube length, release, stop. No heat, no toolhead moves,
        the filament in the head stays (as Kiln's IFS_EJECT of an idle lane).

        :param lane: Lane to eject
        :return Tuple[bool, str]: Success and error message
        """
        unit = self.unit
        slot = unit.slot_of(lane)
        head = unit.status().get("head_slot") or 0
        if (head == slot
                or self.zifs is None
                or unit.eject_cmd != self.eject_cmd):   # eject_cmd set in the config
            return unit.run_cmd(unit.eject_cmd, lane, keep_temp=unit.keep_temp)
        length, speed = 1000, 1200
        try:
            config = self.zifs.get_prutok_config(slot)
            length = int(config.get("filament_tube_length", length))
            speed = int(config.get("filament_ifs_speed", speed))
        except Exception:
            unit.logger.debug(f"AFC_IFS: Zmod slot {slot} config unreadable, eject uses"
                              f" {length} mm at {speed}\n{traceback.format_exc()}")
        return unit.run_cmd("IFS_F24 PRUTOK={slot}\n"
                            "IFS_F11 PRUTOK={slot} LEN={length} SPEED={speed}\n"
                            "IFS_F39 PRUTOK={slot}\n"
                            "IFS_F112",
                            lane, length=length, speed=speed)

    cmd_AFC_IFS_HEAD_RUNOUT_help = "AFC_IFS: filament ran out at the head sensor"

    def cmd_AFC_IFS_HEAD_RUNOUT(self, gcmd: GCodeCommand) -> None:
        """
        Filament ran out at the head sensor. Zmod's ANALOG_PRUTOK calls this
        after pausing and parking.

        The IFS slot flag stays set until the slot is unloaded, so this is
        where AFC hears of the runout. AFC's own infinite spool would resume
        from the park position, so the change runs through AFC and Zmod's
        RESUME restores position and temperature.

        Usage
        -------
        `_AFC_IFS_HEAD_RUNOUT`

        Example
        -------
        ```
        _AFC_IFS_HEAD_RUNOUT
        ```
        """
        unit = self.unit
        afc = unit.afc
        lane = afc.lanes.get(afc.current) if afc.current else None
        if lane is None:
            # The poll may have taken the lane off the head first: the lane
            # that left, else Zmod's last slot (FFMInfo's channel is not reset)
            lane = afc.lanes.get(unit.left_head) if unit.left_head else None
        if lane is None:
            channel = self.status(unit.reactor.monotonic()).get("channel") or 0
            lane = unit.lane_for_slot(channel)
        if (lane is None
            or lane.unit_obj is not unit):
            gcmd.respond_info("AFC_IFS: head runout, no IFS lane in the toolhead")
            return
        change = afc.lanes.get(lane.runout_lane) if lane.runout_lane else None
        ready = (change is not None
                 and change is not lane
                 and change.prep_state
                 and change.status == AFCLaneState.LOADED)
        lane.status = AFCLaneState.NONE
        if not ready:
            unit.lane_not_ready(lane, gcmd=gcmd)
            if change is None:
                why = "runout lane is not set"
            else:
                why = f"runout lane {change.name} is not ready"
            afc.error.AFC_error(f"Runout triggered for lane {lane.name} and {why}. Load the"
                                " next spool into the toolhead, then hit resume to continue.",
                                pause=False, gcmd=gcmd)
            return
        unit.logger.info(f"Infinite Spool triggered for {lane.name} -> {change.name}")
        tool = getattr(self.sdcard, "print_channel", None)
        runout_tool = tool if isinstance(tool, int) else self.tool_for(lane)
        self.runout_tool = runout_tool
        # Not INFINITE_RUNOUT: on one extruder that only makes AFC eject the
        # empty slot, which Zmod takes out itself
        try:
            afc.CHANGE_TOOL(change, restore_pos=False, gcmd=gcmd)
        finally:
            self.runout_tool = None
        if afc.error_state:
            return
        # The empty lane's tools move to the spare, as Zmod's ANALOG_PRUTOK does
        tools = [str(m) for m in (lane.map or []) if tool_number(m) is not None]
        for tcmd in tools:
            unit.gcode.run_script_from_command(f"SET_MAP LANE={change.name} MAP={tcmd}")
        _, fmap = self.read_map()
        if fmap:
            slot = unit.slot_of(change)
            moved = [tool_number(t) for t in tools] or [runout_tool]
            new = [slot if t in moved else s for t, s in enumerate(fmap)]
            if new != fmap:
                self.atomic_write(self.file_config, json.dumps(new, indent=2))
        self.map_mtime, self.map_agreed = self.read_map()
        unit.logger.info(f"AFC_IFS: {', '.join(tools) or f'T{runout_tool}'} now on {change.name}")
        unit.lane_not_ready(lane, gcmd=gcmd)
        unit.gcode.run_script_from_command("RESUME")


def patch_metadata_retry() -> None:
    """
    Retry AFC's print file metadata request for Zmod.

    Zmod writes 3mf_print/*.gcode seconds before the print starts and
    Moonraker answers 404 until it has indexed the file. AFC would log an
    error and count 0 changes. The request runs in AFC's background thread,
    so sleeping does not block the reactor; the last try is AFC's own.
    """
    try:
        from urllib.parse import quote, urljoin
        import extras.AFC_utils as afc_utils
        cls = afc_utils.AFC_moonraker
    except Exception:
        return
    if getattr(cls, "_ifs_metadata_retry", False):
        return
    orig = cls._get_file_metadata_sync

    def sync(self, filename, callback):
        url = urljoin(self.host, f"{self.FILENAME_PATH}{quote(filename)}")
        for _ in range(METADATA_TRIES):
            resp = self._get_results(url, print_error=False)
            if resp is not None:
                self.reactor.register_async_callback(lambda et, resp=resp: callback(resp))
                return
            time.sleep(1.0)
        return orig(self, filename, callback)
    cls._get_file_metadata_sync = sync
    cls._ifs_metadata_retry = True


class KilnBackend(IFSBackend):
    """
    Kiln OS: klipper/extras/ifs.py (printer.ifs, IFS_* commands) drives the IFS.

    Kiln keeps slot materials in ifs_materials and slot spools in its
    spoolman plugin's spoolman_slots. Kiln's IFS commands heat for the
    slot's material and put the old target back.
    """

    name = "kiln"
    load_cmd = "IFS_LOAD SLOT={slot}"
    unload_cmd = "IFS_UNLOAD SLOT={slot}"
    eject_cmd = "IFS_EJECT SLOT={slot}"
    # Kiln's cut, unload, load and flush in one
    swap_cmd = "IFS_SWAP CHANNEL={slot}"
    calibration_hint = " (Kiln: IFS_TUNE)"
    waits_for_tail = True

    def __init__(self, unit: afcIFS):
        """
        Set up Kiln state.

        :param unit: IFS unit
        """
        super().__init__(unit)
        self.ifs: Any = None
        self.head_sensor: Any = None
        self.materials: Any = None
        self.spool_slots: Any = None
        self.tool_map: Optional[Dict[Any, int]] = None
        self.spool_synced: Dict[int, Optional[int]] = {}
        self.spool_wait: Dict[int, Tuple[Optional[int], float]] = {}
        self.spool_pushed: Dict[int, float] = {}
        self.seed_checked = False

    def connect(self) -> None:
        """
        Look up Kiln's objects and follow its loads.
        """
        unit = self.unit
        printer = unit.printer
        self.ifs = printer.lookup_object(unit.ifs_name, None)
        if self.ifs is None:
            error_str = f"[AFC_IFS {unit.name}]: Kiln's [{unit.ifs_name}] object not found"
            raise error(error_str)
        self.head_sensor = printer.lookup_object(unit.head_sensor_name, None)
        self.materials = printer.lookup_object(unit.materials_name, None)
        self.spool_slots = printer.lookup_object(unit.spool_slots_name, None)
        printer.register_event_handler("ifs:loaded", self.on_ifs_loaded)

    def status(self, eventtime: float) -> Dict[str, Any]:
        """
        Read the IFS state from printer.ifs, ifs_materials and the head sensor.

        :param eventtime: Reactor event time
        :return Dict[str, Any]: Normalized state, empty when unreadable
        """
        try:
            st = self.ifs.get_status(eventtime)
        except Exception:
            self.unit.logger.debug(f"AFC_IFS: ifs.get_status failed\n{traceback.format_exc()}")
            return {}
        materials = {}
        if self.materials is not None:
            try:
                materials = self.materials.get_status(eventtime).get("slots") or {}
            except Exception:
                materials = {}
        loaded = st.get("loaded_channels") or []
        slots: Dict[int, SlotState] = {}
        for slot in self.unit.slots_by_lane.values():
            entry = materials.get(str(slot))
            if entry is None:
                slots[slot] = {"present": slot in loaded, "type": None, "color": "",
                               "temp": None}
            else:
                slots[slot] = {"present": slot in loaded, "type": entry.get("type") or "",
                               "color": color_hex(entry.get("color")),
                               "temp": entry.get("temp")}
        head = None
        if self.head_sensor is not None:
            try:
                head = bool(self.head_sensor.get_status(eventtime).get("filament_detected"))
            except Exception:
                head = None
        return {"connected": bool(st.get("connected")), "busy": bool(st.get("busy")),
                "slots": slots, "head_slot": int(st.get("channel") or 0),
                "head_sensor": head, "tool_map": st.get("tool_map")}

    def push_material(self, lane: AFCLane, slot: int, mtype: str,
                      color: str) -> Optional[Material]:
        """
        Set the Kiln slot's material with IFS_SET_MATERIAL.

        :param lane: Lane the material belongs to
        :param slot: IFS slot
        :param mtype: Material type
        :param color: Color as `#RRGGBB` or empty
        :return Optional[Material]: (type, color) Kiln will report, None when nothing was sent
        """
        current = self.unit.last_status.get("slots", {}).get(slot, {})
        args = [f"SLOT={slot}"]
        typed = bool(mtype and KILN_MATERIAL_RE.match(mtype))
        if typed:
            args.append(f"TYPE={mtype}")
        if color:
            # Bare hex: Klipper takes '#' as the start of a comment
            args.append(f"COLOR={color[1:]}")
        if len(args) == 1:
            return None
        if lane.extruder_temp:
            args.append(f"TEMP={int(float(lane.extruder_temp))}")
        self.unit.run_later(f"IFS_SET_MATERIAL {' '.join(args)}")
        return (mtype if typed else (current.get("type") or ""),
                color or current.get("color") or "")

    def early_poll(self, st: Dict[str, Any], eventtime: float) -> None:
        """
        Follow Kiln's tool map, also while Kiln is busy.

        :param st: Current status
        :param eventtime: Reactor event time
        """
        if self.unit.sync_map:
            self.sync_tool_map(st.get("tool_map"))

    def late_poll(self, st: Dict[str, Any], eventtime: float) -> None:
        """
        Keep lane spools and Kiln's spoolman slots in step.

        :param st: Current status
        :param eventtime: Reactor event time
        """
        if self.unit.sync_spools:
            self.sync_spools(eventtime)

    def fold_unload(self, cur_lane: AFCLane, next_lane: str) -> bool:
        """
        In a print, IFS_SWAP does the unload and the next load in one.

        :param cur_lane: Lane being unloaded
        :param next_lane: Name of the lane AFC loads next
        :return bool: True when use_swap is on and a print runs
        """
        return bool(self.unit.use_swap
                    and self.unit.afc.function.in_print())

    def load(self, lane: AFCLane, swap: bool) -> Tuple[bool, str]:
        """
        Load with IFS_SWAP after a folded unload, else IFS_LOAD.

        :param lane: Lane to load
        :param swap: True when the previous lane's unload was folded into this load
        :return Tuple[bool, str]: Success and error message
        """
        unit = self.unit
        return unit.run_cmd(unit.swap_cmd if swap else unit.load_cmd, lane,
                            keep_temp=unit.keep_temp)

    def can_eject_in_print(self) -> bool:
        """
        A serial-mode IFS ejects an idle slot over its own link, without
        toolhead moves. MCU mode (IFS Jacker) drives manual_steppers, which
        queue behind the print's moves.

        :return bool: True on a serial-mode IFS
        """
        ifs = self.ifs
        return (ifs is not None
                and getattr(ifs, "is_mcu_mode", True) is False
                and callable(getattr(ifs, "_run", None))
                and callable(getattr(ifs, "_eject", None)))

    def eject_in_print(self, slot: int) -> Tuple[bool, str]:
        """
        Run IFS_EJECT's idle-slot path (`_eject` under Kiln's IFS mutex)
        without waiting for the print's queued moves, so the print keeps
        going. A tool change that comes meanwhile waits on the mutex.

        :param slot: IFS slot to eject
        :return Tuple[bool, str]: Success and error message
        """
        ifs = self.ifs
        if slot == getattr(ifs, "channel", 0):
            return False, f"slot {slot} is in the toolhead"
        try:
            ifs._run(ifs._eject, slot, op="eject", target=slot, wait=False)
        except Exception as e:
            return False, str(e)
        return True, ""

    def follow_runout(self, old_slot: int, change: AFCLane) -> None:
        """
        Kiln's tool map: the tools on the empty slot go to the new lane's slot
        (as Kiln's auto-refill does), so it agrees with AFC's swapped mapping.

        :param old_slot: Slot that ran out
        :param change: Lane the print changed to
        """
        unit = self.unit
        new_slot = unit.slot_of(change)
        tool_map = getattr(self.ifs, "tool_map", None)
        if isinstance(tool_map, dict):
            pairs = [(int(t), s) for t, s in tool_map.items()]
        elif isinstance(tool_map, (list, tuple)):
            # Kiln keeps a list: tool n -> slot
            pairs = list(enumerate(tool_map))
        else:
            return
        tools = sorted(t for t, s in pairs if s == old_slot)
        for tool in tools:
            cmd = f"IFS_MAP_TOOL TOOL={tool} SLOT={new_slot}"
            try:
                unit.gcode.run_script_from_command(cmd)
            except Exception as e:
                unit.logger.info(f"AFC_IFS: {cmd} failed: {e}")
        if tools:
            unit.logger.info(f"AFC_IFS: Kiln tools {', '.join(f'T{t}' for t in tools)}"
                             f" now on slot {new_slot}")

    def backup_slots(self, slot: int) -> List[int]:
        """
        Kiln's auto-refill candidates for a slot, by its MATCH= setting (exact,
        similar or any), nearest color first: the same choice Kiln's refill
        makes for a slot that just ran out. printer.ifs.refill_backups lists
        only slots that still have filament, so it is no use here. Without
        Kiln's matcher, AFC's lanes.

        :param slot: Slot that ran out
        :return List[int]: Backup slots
        """
        ifs = self.ifs
        candidates = getattr(ifs, "_refill_candidates", None)
        if callable(candidates):
            try:
                present = ifs.get_status(self.unit.reactor.monotonic()).get(
                    "loaded_channels") or []
                return [int(s) for s in candidates(slot, list(present))]
            except Exception:
                self.unit.logger.debug(f"AFC_IFS: Kiln refill match failed\n"
                                       f"{traceback.format_exc()}")
        return super().backup_slots(slot)

    def stand_down_refill(self) -> None:
        """
        auto_runout: AFC_IFS changes lanes on a runout, so Kiln's auto-refill
        (an IFS_SWAP as soon as the slot reads empty) is skipped. Its setting
        and backups list stay as they are.
        """
        ifs = self.ifs
        check = getattr(ifs, "_check_refill", None)
        if not callable(check) or getattr(check, "afc_ifs_skip", False):
            return

        def skip(eventtime):
            return None
        skip.afc_ifs_skip = True
        ifs._check_refill = skip
        if getattr(ifs, "auto_refill", False):
            self.unit.logger.info("AFC_IFS: Kiln's auto-refill is on; with auto_runout the"
                                  " runout change is AFC's, Kiln's own swap is skipped")

    def on_ifs_loaded(self, slot: Optional[int]) -> None:
        """
        Kiln's `ifs:loaded` event, after every Kiln load, unload, swap and
        IFS_SET_CHANNEL. Ours are accounted for by AFC itself.

        :param slot: Slot now in the toolhead, None or 0 for none
        """
        unit = self.unit
        if (unit.busy
            or not unit.prepped()):
            return
        unit.reactor.register_callback(lambda e: unit.sync_tool(slot or 0, now=True))

    def sync_tool_map(self, tool_map: Any) -> None:
        """
        Replay a change of Kiln's tool map into AFC with SET_MAP.

        Kiln maps tools to slots itself (color matching at print start, the
        screen, IFS_MAP_TOOL) and may give two tools one slot.

        :param tool_map: Kiln's ifs.tool_map, {tool: slot}
        """
        unit = self.unit
        if (not isinstance(tool_map, dict)
            or tool_map == self.tool_map):
            return
        self.tool_map = dict(tool_map)
        if not getattr(unit.afc, "enable_multiple_mapping", False):
            unit.logger.info("AFC_IFS: sync_map needs [AFC] enable_multiple_mapping: True")
            return
        moves = []
        for tool, slot in sorted(tool_map.items()):
            tcmd = f"T{tool}"
            lane = unit.lane_for_slot(slot)
            if (lane is None
                or tcmd not in unit.afc.tool_cmds
                or unit.afc.tool_cmds[tcmd] == lane.name):
                continue
            moves.append(f"SET_MAP LANE={lane.name} MAP={tcmd}")
        if not moves:
            return
        unit.logger.info(f"AFC_IFS: following Kiln's tool map: {', '.join(moves)}")
        for cmd in moves:
            try:
                unit.gcode.run_script(cmd)
            except Exception as e:
                unit.logger.info(f"AFC_IFS: {cmd} failed: {e}")

    def sync_spools(self, eventtime: float) -> None:
        """
        Keep lane spools and Kiln's spoolman slots in step.

        Both sides keep a spool per slot and set Moonraker's active spool on
        a load. AFC wins the first look (except right after the switch from
        afclite, see take_seed); after that a change on either side is
        copied to the other.

        :param eventtime: Reactor event time
        """
        unit = self.unit
        if getattr(unit.afc, "spoolman", None) is None:
            return
        kiln_slots = {}
        if self.spool_slots is not None:
            try:
                kiln_slots = self.spool_slots.get_status(eventtime).get("slots") or {}
            except Exception:
                return
        if not self.seed_checked:
            self.seed_checked = True
            self.take_seed(kiln_slots, eventtime)
        if self.spool_slots is None:
            return
        for lane in unit.lanes.values():
            slot = unit.slot_of(lane)
            if str(slot) in kiln_slots:
                self.sync_spool(lane, slot, spool_id(kiln_slots.get(str(slot))), eventtime)

    def sync_spool(self, lane: AFCLane, slot: int, kiln: Optional[int],
                   eventtime: float) -> None:
        """
        Keep one lane's spool and its Kiln spoolman slot in step.

        :param lane: Lane
        :param slot: IFS slot
        :param kiln: Spool ID in Kiln's spoolman slot
        :param eventtime: Reactor event time
        """
        afc = spool_id(lane.spool_id)
        wait = self.spool_wait.get(slot)
        if wait is not None:
            # AFC fetches the spool from Spoolman after SET_SPOOL_ID returns
            sid, deadline = wait
            if (afc != sid
                and eventtime < deadline):
                return
            del self.spool_wait[slot]
            if afc != sid:
                self.unit.logger.info(f"AFC_IFS: AFC did not take spool {sid} for {lane.name};"
                                      f" Kiln's spoolman slot {slot} follows AFC")
            self.spool_synced[slot] = sid
        pushed = self.spool_pushed.get(slot)
        if pushed is not None:
            # Our SPOOLMAN_SET_SLOT may not have run yet
            if (kiln != self.spool_synced.get(slot)
                and eventtime < pushed):
                return
            del self.spool_pushed[slot]
        if slot not in self.spool_synced:
            self.spool_synced[slot] = afc
            if kiln != afc:
                self.push_spool(slot, afc, eventtime)
        elif kiln != self.spool_synced[slot]:
            # Set on Kiln's side: SPOOLMAN_SET_SLOT, the screen
            self.spool_synced[slot] = kiln
            if afc != kiln:
                self.apply_spool(lane, slot, kiln, eventtime)
        elif afc != self.spool_synced[slot]:
            # Set on AFC's side: SET_SPOOL_ID, the panels, NFC, runout
            self.spool_synced[slot] = afc
            self.push_spool(slot, afc, eventtime)

    def take_seed(self, kiln_slots: Dict[str, Any], eventtime: float) -> None:
        """
        First start after kiln_afc replaced afclite: a lane without a spool
        takes Kiln's spoolman slot spool, else afclite's.

        :param kiln_slots: Kiln's spoolman slots, {"1": spool ID}
        :param eventtime: Reactor event time
        """
        unit = self.unit
        path = f"{getattr(unit.afc, 'VarFile', '') or ''}{SEED_SUFFIX}"
        try:
            with open(path) as f:
                seed = json.load(f).get("slots") or {}
        except FileNotFoundError:
            return
        except (OSError, ValueError, AttributeError):
            seed = {}
        for lane in unit.lanes.values():
            slot = unit.slot_of(lane)
            if spool_id(lane.spool_id) is not None:
                continue
            kiln = spool_id(kiln_slots.get(str(slot)))
            sid = kiln or spool_id(seed.get(str(slot)))
            if sid is None:
                continue
            unit.logger.info(f"AFC_IFS: {lane.name} keeps spool {sid} from afclite")
            self.apply_spool(lane, slot, sid, eventtime)
            if str(slot) in kiln_slots:
                self.spool_synced[slot] = sid
                if kiln != sid:
                    self.push_spool(slot, sid, eventtime)
        try:
            os.remove(path)
        except OSError:
            pass

    def push_spool(self, slot: int, sid: Optional[int], eventtime: float) -> None:
        """
        Set Kiln's spoolman slot to AFC's spool.

        :param slot: IFS slot
        :param sid: Spool ID, None to clear
        :param eventtime: Reactor event time
        """
        self.spool_pushed[slot] = eventtime + SPOOL_WAIT
        self.unit.run_later(f"SPOOLMAN_SET_SLOT SLOT={slot} SPOOL={sid or ''}")

    def apply_spool(self, lane: AFCLane, slot: int, sid: Optional[int],
                    eventtime: float) -> None:
        """
        Set the lane's spool to Kiln's, taking it off any other lane first.

        :param lane: Lane
        :param slot: IFS slot
        :param sid: Spool ID, None to clear
        :param eventtime: Reactor event time
        """
        scripts = []
        if sid is not None:
            for other in self.unit.afc.lanes.values():
                if (other is not lane
                    and spool_id(other.spool_id) == sid):
                    scripts.append(f"SET_SPOOL_ID LANE={other.name} SPOOL_ID=")
        scripts.append(f"SET_SPOOL_ID LANE={lane.name} SPOOL_ID={sid or ''}")
        self.spool_wait[slot] = (sid, eventtime + SPOOL_WAIT)
        self.unit.run_later(*scripts)


BACKENDS = {"zmod": ZmodBackend, "kiln": KilnBackend}


class afcIFS(afcUnit):
    def __init__(self, config: ConfigWrapper):
        """
        Read the unit's options. Options with a firmware default are filled in
        when the backend is picked.

        :param config: Configuration of the `[AFC_IFS <name>]` section
        """
        super().__init__(config)
        self.type = config.get("type", IFS_TYPE)
        self.stepperless_drive = True
        self.firmware = config.get("firmware", "auto").lower()
        if self.firmware not in ("auto",) + tuple(BACKENDS):
            error_str = f"[AFC_IFS {self.name}]: firmware must be auto, {', '.join(BACKENDS)}"
            raise error(error_str)
        self.poll_interval = config.getfloat("poll_interval", 1.0, minval=0.2)
        # Readings a slot must hold before AFC hears of a change
        self.stable_polls = config.getint("stable_polls", 3, minval=1)
        self.empty_polls: Optional[int] = config.getint("empty_polls", None, minval=1)
        self.prep_sample: Optional[float] = config.getfloat("prep_sample", None, minval=0.0)
        self.afc_runout: Optional[bool] = config.getboolean("afc_runout", None)
        self.keep_temp: Optional[bool] = config.getboolean("keep_temp", None)
        self.load_cmd: Optional[str] = config.get("load_cmd", None)
        self.unload_cmd: Optional[str] = config.get("unload_cmd", None)
        self.eject_cmd: Optional[str] = config.get("eject_cmd", None)
        self.swap_cmd: Optional[str] = config.get("swap_cmd", None)
        # The firmware's IFS commands heat for the slot's material
        self.afc_heats = config.getboolean("afc_heats", False)
        self.sync_materials = config.getboolean("sync_materials", True)
        # follow_tool_map is the older KILN_AFC name
        self.sync_map = config.getboolean("sync_map",
                                          config.getboolean("follow_tool_map", True))
        # Zmod: tool map back to T(n) on slot n+1 when a print ends
        self.idle_map = config.getboolean("idle_map", True)
        # Kiln: mid-print changes as one IFS_SWAP
        self.use_swap = config.getboolean("use_swap", True)
        # LANE_UNLOAD of an idle lane during a print, beside the print
        self.print_eject = config.getboolean("print_eject", True)
        # Kiln: on a slot runout, print the rest of the strand and change
        # when its end passes the toolhead sensor
        self.tail_runout = config.getboolean("tail_runout", True)
        # A runout with no ready runout lane picks a loaded lane with the same
        # filament (Kiln: its auto-refill matches, MATCH=exact|similar|any)
        self.auto_runout = config.getboolean("auto_runout", True)
        self.ifs_name = config.get("ifs", "ifs")
        self.head_sensor_name = config.get("head_sensor", "adc_filament_sensor toolhead")
        self.materials_name = config.get("materials", "ifs_materials")
        self.sync_spools = config.getboolean("sync_spools", True)
        self.spool_slots_name = config.get("spool_slots", "spoolman_slots")

        self.backend: Optional[IFSBackend] = None
        self.slots_by_lane: Dict[str, int] = {}
        self.last_status: Dict[str, Any] = {}
        self.busy = False
        self.left_head: Optional[str] = None
        self.seen: Dict[str, bool] = {}
        self.pending: Dict[str, Tuple[bool, int]] = {}
        self.head: Optional[Tuple[int, int]] = None
        self.await_empty: Set[str] = set()
        self.print_ejecting: Set[str] = set()
        self.tail_lane: Optional[str] = None
        self.head_runout_hooked = False
        self.prep_seen: Optional[Tuple[float, Dict[int, bool]]] = None
        self.synced: Dict[int, Tuple[Material, Material]] = {}
        self.swap_from: Optional[str] = None
        self.runout_dropped = False
        self.logo = "<span class=success--text>IFS ready</span>\n"
        self.logo_error = "<span class=error--text>IFS not ready</span>\n"
        # Before AFC_spool's handler, which registers in its klippy:connect
        self.printer.register_event_handler("afc_stepper:register_macros",
                                            self.on_register_macros)
        self.prepare_varfile(config)

    def prepare_varfile(self, config: ConfigWrapper) -> None:
        """
        Create AFC.var.unit, which PREP needs. install-afc.sh creates it; a
        firmware that ships AFC without the installer has none.

        :param config: Configuration of the unit section
        """
        try:
            var = config.getsection("AFC").get("VarFile", None, note_valid=False)
        except Exception:
            var = None
        if not var:
            return
        var = os.path.expanduser(var)
        try:
            os.makedirs(os.path.dirname(var), exist_ok=True)
            if not os.path.exists(f"{var}.unit"):
                with open(f"{var}.unit", "w") as f:
                    f.write("{}")
        except OSError as e:
            self.logger.info(f"AFC_IFS: {var}: {e}")

    def get_backend(self) -> IFSBackend:
        """
        Pick the firmware backend once and fill in its option defaults.

        :return IFSBackend: Backend for the printer's firmware
        """
        if self.backend is not None:
            return self.backend
        kind = self.firmware
        if kind == "auto":
            if self.printer.lookup_object("zmod_color", None) is not None:
                kind = "zmod"
            elif self.printer.lookup_object(self.ifs_name, None) is not None:
                kind = "kiln"
            else:
                error_str = (f"[AFC_IFS {self.name}]: no IFS firmware found (Zmod's"
                             f" [zmod_color] or Kiln's [{self.ifs_name}]); set firmware:")
                raise error(error_str)
        backend = BACKENDS[kind](self)
        for attr in ("load_cmd", "unload_cmd", "eject_cmd", "swap_cmd", "empty_polls",
                     "prep_sample", "afc_runout", "keep_temp"):
            if getattr(self, attr) is None:
                setattr(self, attr, getattr(backend, attr))
        self.backend = backend
        return backend

    def on_register_macros(self, lane: AFCLane) -> None:
        """
        On Zmod, drop its SET_RUNOUT macro before AFC registers its command.

        :param lane: Lane whose macros are being registered
        """
        backend = self.get_backend()
        if (isinstance(backend, ZmodBackend)
            and not self.runout_dropped):
            self.runout_dropped = True
            backend.drop_runout_macro()

    def handle_connect(self) -> None:
        """
        Connect the backend and take the lanes' slots from `unit: <name>:<slot>`.
        """
        super().handle_connect()
        backend = self.get_backend()
        backend.connect()
        if not self.afc_heats:
            self.skip_afc_preheat()
        for lane in self.lanes.values():
            slot = getattr(lane, "index", 0)
            if not 1 <= slot <= MAX_SLOTS:
                error_str = (f"[{lane.fullname}]: unit must be {self.name}:<slot 1-{MAX_SLOTS}>,"
                             f" e.g. unit: {self.name}:1")
                raise error(error_str)
            self.slots_by_lane[lane.name] = slot
            # The poll drives the lane sensor; the IFS has no hub sensor
            lane.prep_state = False
            lane._load_state = False
            lane.loaded_to_hub = False

    def skip_afc_preheat(self) -> None:
        """
        Skip AFC's heat-and-wait before IFS loads and unloads, which the
        firmware's commands do themselves.
        """
        afc = self.afc
        if getattr(afc, "_ifs_preheat_wrapped", False):
            return
        orig = afc._check_extruder_temp

        def check(cur_lane, *args, **kwargs):
            unit = getattr(cur_lane, "unit_obj", None)
            if (isinstance(unit, afcIFS)
                and not unit.afc_heats):
                return False
            return orig(cur_lane, *args, **kwargs)
        afc._check_extruder_temp = check
        afc._ifs_preheat_wrapped = True

    def handle_ready(self) -> None:
        """
        Start the poll.
        """
        super().handle_ready()
        if self.print_eject:
            self.wrap_lane_unload()
        if getattr(self.backend, "waits_for_tail", False):
            self.gcode.register_command("_AFC_IFS_TAIL_RUNOUT", self.cmd_AFC_IFS_TAIL_RUNOUT,
                                        desc=self.cmd_AFC_IFS_TAIL_RUNOUT_help)
            if self.tail_runout:
                self.hook_head_runout()
            if self.auto_runout:
                self.backend.stand_down_refill()
        self.reactor.register_timer(self.poll_safe, self.reactor.monotonic() + self.poll_interval)

    def wrap_lane_unload(self) -> None:
        """
        Put a handler in front of AFC's LANE_UNLOAD for this unit's lanes. AFC
        refuses every eject while printing; the handler ejects an idle lane
        beside the print when the backend can, and leaves the rest to AFC.
        """
        mux = getattr(self.gcode, "mux_commands", {}).get("LANE_UNLOAD")
        if mux is None:
            return
        handlers = mux[1]
        for lane in self.lanes.values():
            orig = handlers.get(lane.name)
            if orig is None or getattr(orig, "afc_ifs_print_eject", False):
                continue

            def handler(gcmd, lane=lane, orig=orig):
                if not self.take_print_eject(lane):
                    return orig(gcmd)
                self.start_print_eject(lane, gcmd)
            handler.afc_ifs_print_eject = True
            handlers[lane.name] = handler

    def hook_head_runout(self) -> None:
        """
        Kiln: put a handler in front of the toolhead sensor's runout event, so a
        slot runout can wait for the strand's end to reach the head.
        """
        backend = self.backend
        if not getattr(backend, "waits_for_tail", False):
            return
        sensor = getattr(backend, "head_sensor", None)
        helper = getattr(sensor, "runout_helper", None)
        if (helper is None
            or not callable(getattr(helper, "_runout_event_handler", None))):
            self.logger.info(f"AFC_IFS: no runout helper on {self.head_sensor_name}; a slot"
                             " runout changes lanes at once")
            return
        if getattr(helper, "afc_ifs_tail", False):
            self.head_runout_hooked = True
            return
        orig = helper._runout_event_handler

        def handler(eventtime):
            lane = self.take_tail_runout()
            if lane is None:
                return orig(eventtime)
            self.head_runout(helper, lane)
        helper._runout_event_handler = handler
        helper.afc_ifs_tail = True
        self.head_runout_hooked = True

    def handle_runout(self, lane: AFCLane, gcmd: Any = "callback") -> bool:
        """
        AFC's unit hook for a slot runout of the lane in the toolhead. With the
        head hook in place and the strand still in the head, the runout waits
        for the head sensor: the IFS can't pull back a tail that has left its
        slot, so the change cut and retract would fail.

        Without the head hook, or with the head already empty, the runout runs
        at once, still through `_AFC_IFS_TAIL_RUNOUT` so auto_runout picks the
        lane.

        :param lane: Lane whose slot ran empty
        :return bool: True when this unit handles the runout
        """
        if not getattr(self.backend, "waits_for_tail", False):
            return False
        change = self.runout_target(lane)
        then = f"changes to {change.name}" if change is not None else "pauses"
        if (self.head_runout_hooked
            and self.status().get("head_sensor") is not False):
            self.tail_lane = lane.name
            self.gcode.respond_info(f"AFC_IFS: {lane.name} ran out; printing the rest of the"
                                    f" strand, the print {then} when its end passes the"
                                    " toolhead sensor")
            self.logger.info(f"AFC_IFS: {lane.name} slot runout, waiting for the toolhead"
                             " sensor")
            return True
        self.gcode.respond_info(f"AFC_IFS: {lane.name} ran out; the print {then}")

        def run(eventtime):
            try:
                self.afc.error.pause_resume.send_pause_command()
                self.gcode.run_script(f"_AFC_IFS_TAIL_RUNOUT LANE={lane.name}")
            except Exception:
                self.logger.error(f"AFC_IFS: runout of {lane.name} failed\n"
                                  f"{traceback.format_exc()}")
        self.reactor.register_callback(run)
        return True

    def runout_target(self, lane: AFCLane) -> Optional[AFCLane]:
        """
        Lane a runout changes to: the lane's runout lane when it is ready, else
        (auto_runout) a loaded lane of this unit with the same filament, as the
        backend matches it. A runout lane that isn't ready is still returned
        when nothing matches, so AFC reports it.

        :param lane: Lane that ran out
        :return Optional[AFCLane]: Lane to change to, None to pause
        """
        def ready(cand: Optional[AFCLane]) -> bool:
            return (cand is not None
                    and cand is not lane
                    and bool(cand.prep_state)
                    and cand.status == AFCLaneState.LOADED)
        explicit = self.afc.lanes.get(lane.runout_lane) if lane.runout_lane else None
        if ready(explicit):
            return explicit
        if self.auto_runout and self.backend is not None:
            for slot in self.backend.backup_slots(self.slot_of(lane)):
                cand = self.lane_for_slot(slot)
                if ready(cand):
                    return cand
        return explicit

    def take_tail_runout(self) -> Optional[AFCLane]:
        """
        The lane a head runout belongs to: the one whose slot ran out, still in
        the toolhead, in a print. Clears the wait.

        :return Optional[AFCLane]: Lane, or None to let the sensor's own handling run
        """
        name, self.tail_lane = self.tail_lane, None
        lane = self.lanes.get(name) if name else None
        if (lane is None
            or not self.afc.function.in_print()
            or self.afc.function.get_current_lane() != lane.name):
            return None
        return lane

    def head_runout(self, helper: Any, lane: AFCLane) -> None:
        """
        Reactor callback for the head sensor's runout: pause at once (as
        Klipper's runout handling does from an event) and run AFC's runout
        for the lane as G-code.

        :param helper: The toolhead sensor's RunoutHelper
        :param lane: Lane whose strand just left the head sensor
        """
        try:
            self.logger.info(f"AFC_IFS: end of {lane.name} passed the toolhead sensor")
            self.afc.error.pause_resume.send_pause_command()
            self.gcode.run_script(f"_AFC_IFS_TAIL_RUNOUT LANE={lane.name}")
        except Exception:
            self.logger.error(f"AFC_IFS: runout of {lane.name} failed\n"
                              f"{traceback.format_exc()}")
        finally:
            helper.min_event_systime = self.reactor.monotonic() + helper.event_delay

    cmd_AFC_IFS_TAIL_RUNOUT_help = "AFC_IFS: a runout strand's end passed the toolhead sensor"

    def cmd_AFC_IFS_TAIL_RUNOUT(self, gcmd: GCodeCommand) -> None:
        """
        AFC's runout for a lane once its strand has left the toolhead sensor:
        infinite spool to its runout lane, else AFC's runout pause. Run by the
        head sensor hook.

        Usage
        -------
        `_AFC_IFS_TAIL_RUNOUT LANE=<lane>`

        Example
        -------
        ```
        _AFC_IFS_TAIL_RUNOUT LANE=lane1
        ```
        """
        lane = self.lanes.get(gcmd.get("LANE"))
        if lane is None:
            raise gcmd.error(f"AFC_IFS: {gcmd.get('LANE')} is not an {self.name} lane")
        change = self.runout_target(lane)
        if change is None:
            lane._perform_pause_runout(gcmd=gcmd)
            return
        if change.name != lane.runout_lane:
            self.logger.info(f"AFC_IFS: auto runout {lane.name} -> {change.name} (same"
                             " filament)")
        # AFC's infinite spool changes to lane.runout_lane; the lane's own
        # setting is put back after
        saved = lane.runout_lane
        lane.runout_lane = change.name
        try:
            lane._perform_infinite_runout(gcmd=gcmd)
        finally:
            lane.runout_lane = saved
        if (change is not None
            and not self.afc.error_state
            and self.afc.function.get_current_lane() == change.name):
            self.backend.follow_runout(self.slot_of(lane), change)

    def take_print_eject(self, lane: AFCLane) -> bool:
        """
        Whether LANE_UNLOAD of a lane is ours to run beside a print.

        :param lane: Lane to eject
        :return bool: True while printing, for an idle lane, when the backend can
        """
        extruder = lane.extruder_obj
        return bool(self.afc.function.is_printing()
                    and self.backend is not None
                    and self.backend.can_eject_in_print()
                    and not lane.tool_loaded
                    and (extruder is None or extruder.lane_loaded != lane.name)
                    and self.afc.function.get_current_lane() != lane.name)

    def start_print_eject(self, lane: AFCLane, gcmd: Any = "callback") -> None:
        """
        Start an eject beside the print and return at once, so the print's
        G-code keeps running. Refused while the IFS or AFC is busy.

        :param lane: Idle lane to eject
        :param gcmd: The LANE_UNLOAD command
        """
        slot = self.slot_of(lane)
        if lane.name in self.print_ejecting:
            self.gcode.respond_info(f"AFC_IFS: {lane.name} is already being ejected")
            return
        if (self.busy
            or self.status().get("busy")
            or self.afc.current_state not in ("Idle", None)):
            self.afc.error.AFC_error(f"IFS is busy with a tool change; eject {lane.name}"
                                     " again in a moment", pause=False, gcmd=gcmd)
            return
        tools = [t for t in (lane.map if isinstance(lane.map, list) else [lane.map]) if t]
        if tools:
            self.gcode.respond_info(
                f"AFC_IFS: {lane.name} is still mapped to {', '.join(tools)}; a change to"
                f" {'it' if len(tools) == 1 else 'them'} later in this print pauses on an"
                " empty lane")
        self.print_ejecting.add(lane.name)
        self.await_empty.add(lane.name)
        self.lane_unloading(lane, gcmd=gcmd)
        lane.status = AFCLaneState.EJECTING
        self.afc.save_vars()
        self.gcode.respond_info(f"AFC_IFS: ejecting {lane.name} (IFS slot {slot}) beside"
                                " the print")
        self.reactor.register_callback(lambda eventtime: self.finish_print_eject(lane, slot))

    def finish_print_eject(self, lane: AFCLane, slot: int) -> None:
        """
        Reactor callback: run the eject, then do LANE_UNLOAD's lane bookkeeping
        (AFC's own state is left alone, a print is running).

        :param lane: Lane being ejected
        :param slot: Its IFS slot
        """
        try:
            ok, err = self.backend.eject_in_print(slot)
            self.pending.clear()
            if not ok:
                self.await_empty.discard(lane.name)
                lane.status = AFCLaneState.LOADED if lane.prep_state else AFCLaneState.NONE
                self.afc.save_vars()
                self.afc.error.AFC_error(f"IFS eject of {lane.name} failed: {err}",
                                         pause=False, gcmd="callback")
                return
            lane.loaded_to_hub = False
            lane.status = AFCLaneState.NONE
            self.afc.spool.set_spoolID(lane, None, gcmd="callback")
            self.afc.save_vars()
            self.lane_unloaded(lane, gcmd="callback")
            self.logger.info(f"LANE {lane.name} eject done (beside the print)")
            self.gcode.respond_info(f"AFC_IFS: {lane.name} - pull the filament out of IFS"
                                    f" slot {slot}")
        except Exception:
            self.logger.error(f"AFC_IFS: eject of {lane.name} failed\n"
                              f"{traceback.format_exc()}")
        finally:
            self.print_ejecting.discard(lane.name)

    def slot_of(self, lane: AFCLane) -> int:
        """
        IFS slot of a lane.

        :param lane: Lane of this unit
        :return int: Slot 1-16
        """
        return self.slots_by_lane[lane.name]

    def lane_for_slot(self, slot: int) -> Optional[AFCLane]:
        """
        Lane of an IFS slot.

        :param slot: IFS slot
        :return Optional[AFCLane]: Lane, None when no lane uses the slot
        """
        for name, lane_slot in self.slots_by_lane.items():
            if lane_slot == slot:
                return self.lanes.get(name)
        return None

    def prepped(self) -> bool:
        """
        Whether PREP has run for all lanes.

        :return bool: True once every lane is prepped
        """
        return (bool(self.lanes)
                and all(getattr(lane, "_afc_prep_done", False) for lane in self.lanes.values()))

    def status(self, eventtime: Optional[float] = None) -> Dict[str, Any]:
        """
        Read the backend's state and keep the last good one.

        :param eventtime: Reactor event time, now when None
        :return Dict[str, Any]: Normalized state, empty when unreadable
        """
        if eventtime is None:
            eventtime = self.reactor.monotonic()
        st = self.backend.status(eventtime)
        if st:
            self.last_status = st
        return st

    def run_later(self, *scripts: str) -> None:
        """
        Run G-code lines from a reactor callback, logging failures.

        :param scripts: G-code lines
        """
        def run(eventtime):
            for script in scripts:
                self.logger.debug(f"AFC_IFS: {script}")
                try:
                    self.gcode.run_script(script)
                except Exception as e:
                    self.logger.info(f"AFC_IFS: {script} failed: {e}")
        self.reactor.register_callback(run)

    def set_presence(self, lane: AFCLane, present: bool) -> None:
        """
        Set the lane's slot sensor.

        :param lane: Lane
        :param present: Whether the slot has filament
        """
        lane.prep_state = present
        lane.loaded_to_hub = present
        lane._load_state = False

    def send_lane_data(self, lane: AFCLane) -> None:
        """
        Publish the lane to Moonraker once AFC's client exists (from PREP on).

        :param lane: Lane
        """
        if getattr(self.afc, "moonraker", None) is not None:
            lane.send_lane_data()

    def poll_safe(self, eventtime: float) -> float:
        """
        Poll timer. An exception in a reactor timer shuts Klipper down, so log it.

        :param eventtime: Reactor event time
        :return float: Next wake time
        """
        try:
            return self.poll(eventtime)
        except Exception:
            self.logger.error(f"AFC_IFS: poll failed\n{traceback.format_exc()}")
            return eventtime + max(self.poll_interval, 5.0)

    def poll(self, eventtime: float) -> float:
        """
        Mirror the firmware's slots, toolhead and materials into AFC.

        :param eventtime: Reactor event time
        :return float: Next wake time
        """
        wake = eventtime + self.poll_interval
        if (self.tail_lane is not None
            and not self.afc.function.in_print()):
            self.tail_lane = None
        if (self.busy
            or self.backend is None
            or not self.prepped()
            or self.afc.current_state not in ("Idle", None)):
            return wake
        st = self.status(eventtime)
        if not st.get("connected"):
            return wake
        self.backend.early_poll(st, eventtime)
        if st.get("busy"):
            return wake
        head = st.get("head_sensor")
        if head is not None:
            for lane in self.lanes.values():
                if (lane.extruder_obj is not None
                    and lane.extruder_obj.tool_start is None):
                    lane.extruder_obj.tool_start_state = head
        slots = st.get("slots") or {}
        for lane in self.lanes.values():
            present = bool(slots.get(self.slot_of(lane), {}).get("present"))
            self.poll_lane(lane, present, eventtime)
        self.sync_tool(st.get("head_slot") or 0)
        self.backend.late_poll(st, eventtime)
        if self.sync_materials:
            self.sync_slot_materials(st)
        return wake

    def poll_lane(self, lane: AFCLane, present: bool, eventtime: float) -> None:
        """
        Debounce a slot reading and hand a change to AFC.

        :param lane: Lane
        :param present: Whether the slot reads filament now
        :param eventtime: Reactor event time
        """
        seen = self.seen.get(lane.name)
        if seen is None:
            self.seen[lane.name] = present
            self.set_presence(lane, present)
            return
        if not present:
            self.await_empty.discard(lane.name)
        if present == seen:
            self.pending.pop(lane.name, None)
            if (present
                and not lane.load_state
                and not lane.tool_loaded
                and lane.name not in self.await_empty
                and lane.status != AFCLaneState.EJECTING):
                # Pulled and put back faster than empty_polls: an insert for AFC
                self.logger.info(f"AFC_IFS: {lane.name} has filament again")
                self.set_presence(lane, True)
                self.load_runout(lane, eventtime, True)
            return
        value, count = self.pending.get(lane.name, (present, 0))
        count = count + 1 if value == present else 1
        if count < (self.stable_polls if present else self.empty_polls):
            self.pending[lane.name] = (present, count)
            return
        self.pending.pop(lane.name, None)
        self.seen[lane.name] = present
        self.set_presence(lane, present)
        if (lane.status == AFCLaneState.EJECTING
            and not present):
            lane.status = AFCLaneState.NONE
        self.load_runout(lane, eventtime, present)

    def load_runout(self, lane: AFCLane, eventtime: float, present: bool) -> None:
        """
        Run AFC's insert or runout logic for a slot change.

        :param lane: Lane
        :param eventtime: Reactor event time
        :param present: Whether the slot has filament
        """
        try:
            lane.handle_load_runout(eventtime, present)
        except Exception:
            self.logger.error(f"AFC_IFS: {lane.name} sensor change failed\n"
                              f"{traceback.format_exc()}")

    def sync_tool(self, slot: int, now: bool = False, gcmd: Any = "callback") -> None:
        """
        Make AFC's toolhead lane match the firmware's slot in the head.

        :param slot: Slot in the toolhead, 0 for none
        :param now: Apply without waiting for stable_polls readings
        """
        if (self.busy
            or self.afc.current_state not in ("Idle", None)):
            return
        if not now:
            last, count = self.head or (slot, 0)
            count = count + 1 if last == slot else 1
            self.head = (slot, count)
            if count < self.stable_polls:
                return
        want = self.lane_for_slot(slot) if slot else None
        have = None
        for lane in self.lanes.values():
            if (lane.tool_loaded
                or (lane.extruder_obj is not None
                    and lane.extruder_obj.lane_loaded == lane.name)):
                have = lane
                break
        if want is have:
            return
        if have is not None:
            self.logger.info(f"AFC_IFS: {have.name} left the toolhead outside AFC")
            self.left_head = have.name
            have.set_tool_unloaded(normal_toolchange=True, gcmd=gcmd)
            if have.prep_state:
                have.status = AFCLaneState.LOADED
        if want is not None:
            self.logger.info(f"AFC_IFS: {want.name} is in the toolhead"
                             f" ({self.backend.name} slot {slot})")
            self.left_head = None
            want.loaded_to_hub = True
            want.set_tool_loaded(normal_toolchange=True, gcmd=gcmd)
        self.afc.save_vars()

    def apply_slot(self, lane: AFCLane, entry: SlotState) -> None:
        """
        Copy the firmware slot's material, color and temperature to the lane.

        :param lane: Lane
        :param entry: Normalized slot state
        """
        lane.material = entry.get("type") or ""
        if entry.get("color"):
            lane.color = entry["color"]
        if entry.get("temp"):
            lane.extruder_temp = entry["temp"]
        if (lane.spool_id is None
            and not lane.weight):
            # AFC's default for a spool without Spoolman; panels draw 0 g as empty
            lane.weight = 1000
        self.send_lane_data(lane)

    def push_slot(self, lane: AFCLane, slot: int, afc: Material, fw: Material) -> None:
        """
        Write AFC's material to the firmware slot and remember the agreed pair.

        :param lane: Lane
        :param slot: IFS slot
        :param afc: AFC's (type, color)
        :param fw: Firmware's current (type, color)
        """
        reported = self.backend.push_material(lane, slot, afc[0], afc[1])
        self.synced[slot] = (reported if reported is not None else fw, afc)

    def sync_slot_materials(self, st: Dict[str, Any]) -> None:
        """
        Keep lane materials and colors and the firmware's slots in step.

        Each slot keeps the (firmware, AFC) pair both sides agreed on, since
        the firmware's material is not always AFC's (Zmod: PLA+ is PLA). A
        spool set on an empty lane (an NFC scan before loading) goes to the
        firmware too and survives the insert.

        :param st: Current status
        """
        slots = st.get("slots") or {}
        changed = False
        for lane in self.lanes.values():
            slot = self.slot_of(lane)
            entry = slots.get(slot)
            if (entry is None
                or entry.get("type") is None):
                self.synced.pop(slot, None)
                continue
            fw = (entry.get("type") or "", entry.get("color") or "")
            afc = (lane.material or "", color_hex(lane.color))
            if lane.prep_state:
                changed = self.sync_loaded_slot(lane, slot, entry, fw, afc) or changed
            else:
                changed = self.sync_empty_slot(lane, slot, entry, fw, afc) or changed
        if changed:
            self.afc.save_vars()

    def sync_loaded_slot(self, lane: AFCLane, slot: int, entry: SlotState, fw: Material,
                         afc: Material) -> bool:
        """
        Sync the material of a slot with filament.

        :param lane: Lane
        :param slot: IFS slot
        :param entry: Normalized slot state
        :param fw: Firmware's (type, color)
        :param afc: AFC's (type, color)
        :return bool: True when the lane changed
        """
        agreed = self.synced.get(slot)
        if (agreed is None
            or fw != agreed[0]
            or afc == ("", "")):
            # First look, set on the firmware's side (screen, menu, NFC), or
            # AFC cleared the lane: the firmware wins
            changed = afc != fw
            if changed:
                self.apply_slot(lane, entry)
            self.synced[slot] = (fw, (lane.material or "", color_hex(lane.color)))
            return changed
        if afc != agreed[1]:
            # Set on AFC's side: SET_COLOR, SET_MATERIAL, SET_SPOOL_ID
            self.push_slot(lane, slot, afc, fw)
        return False

    def sync_empty_slot(self, lane: AFCLane, slot: int, entry: SlotState, fw: Material,
                        afc: Material) -> bool:
        """
        Sync the material of an empty slot, where a spool can be set before loading.

        :param lane: Lane
        :param slot: IFS slot
        :param entry: Normalized slot state
        :param fw: Firmware's (type, color)
        :param afc: AFC's (type, color)
        :return bool: True when the lane changed
        """
        agreed = self.synced.get(slot)
        if agreed is None:
            if (afc != ("", "")
                and afc != fw):
                # AFC kept a spool for the empty lane over a restart
                self.push_slot(lane, slot, afc, fw)
            else:
                self.synced[slot] = (fw, afc)
            return False
        if fw != agreed[0]:
            # The firmware's menu set the empty slot
            changed = (afc != fw
                       and fw != ("", ""))
            if changed:
                self.apply_slot(lane, entry)
            self.synced[slot] = (fw, (lane.material or "", color_hex(lane.color)))
            return changed
        if afc != agreed[1]:
            if afc == ("", ""):
                # AFC cleared the spool after a runout: leave the slot alone
                self.synced[slot] = (agreed[0], afc)
            else:
                self.push_slot(lane, slot, afc, fw)
        return False

    def system_Test(self, cur_lane: AFCLane, delay: float, assignTcmd: bool,
                    enable_movement: bool, gcmd: Any = "callback") -> bool:
        """
        PREP for an IFS lane: read the slot and the toolhead from the firmware.

        :param cur_lane: Lane to test
        :param delay: Unused, the IFS has no lane motor
        :param assignTcmd: Whether to assign the lane's tool command
        :param enable_movement: Unused, the IFS has no lane motor
        :param gcmd: The PREP command
        :return bool: True when the IFS answered
        """
        st = self.status()
        until = self.reactor.monotonic() + CONNECT_WAIT
        while (not st.get("connected")
               and self.reactor.monotonic() < until):
            self.reactor.pause(self.reactor.monotonic() + 0.5)
            st = self.status()
        succeeded = bool(st.get("connected"))
        slot = self.slot_of(cur_lane)
        if succeeded:
            msg = self.prep_lane(cur_lane, slot, st, gcmd)
        else:
            msg = "<span class=error--text>IFS NOT CONNECTED</span>"
        if assignTcmd:
            self.afc.function.TcmdAssign(cur_lane)
        if isinstance(cur_lane.map, tuple):
            # A config `map:` is getlist()'s tuple; SET_MAP then fails on tuple + list
            cur_lane.map = list(cur_lane.map)
        entry = (st.get("slots") or {}).get(slot)
        if (succeeded
            and self.sync_materials
            and cur_lane.prep_state
            and entry is not None
            and entry.get("type") is not None):
            self.apply_slot(cur_lane, entry)
            self.synced[slot] = ((entry.get("type") or "", entry.get("color") or ""),
                                 (cur_lane.material or "", color_hex(cur_lane.color)))
        # AFC cleared lane_data when PREP started; write this lane back
        self.send_lane_data(cur_lane)
        cur_lane.do_enable(False)
        self.logger.info(f"{cur_lane.name} tool cmd: {cur_lane.map_to_string():3} {msg}")
        cur_lane.set_afc_prep_done()
        return succeeded

    def prep_lane(self, cur_lane: AFCLane, slot: int, st: Dict[str, Any],
                  gcmd: Any = "callback") -> str:
        """
        Set a lane's presence and toolhead state for PREP.

        :param cur_lane: Lane
        :param slot: IFS slot
        :param st: Current status
        :param gcmd: The PREP command
        :return str: PREP message for the lane
        """
        sample = self.prep_presence()
        present = sample.get(slot, bool((st.get("slots") or {}).get(slot, {}).get("present")))
        self.seen[cur_lane.name] = present
        self.set_presence(cur_lane, present)
        in_head = (st.get("head_slot") or 0) == slot
        if (in_head
            and not cur_lane.tool_loaded):
            cur_lane.set_tool_loaded(gcmd=gcmd)
        elif (cur_lane.tool_loaded
              and not in_head):
            cur_lane.set_tool_unloaded(gcmd=gcmd)
        if not (present or in_head):
            if not cur_lane.remember_spool:
                self.afc.spool.clear_values(cur_lane)
            self.lane_not_ready(cur_lane, gcmd=gcmd)
            return "EMPTY READY FOR SPOOL"
        self.lane_loaded(cur_lane, gcmd=gcmd)
        cur_lane.status = AFCLaneState.LOADED
        msg = "<span class=success--text>LOCKED AND LOADED</span>"
        if in_head:
            cur_lane.loaded_to_hub = True
            cur_lane.status = AFCLaneState.TOOLED
            self.afc.spool.set_active_spool(cur_lane.spool_id)
            self.lane_tool_loaded(cur_lane, gcmd=gcmd)
            msg += "<span class=primary--text> in ToolHead</span>"
            self.printer.send_event("afc:tool_loaded", cur_lane)
        return msg

    def prep_presence(self) -> Dict[int, bool]:
        """
        Sample the slots for prep_sample seconds; filament in any reading
        counts. One sample serves all lanes of a PREP.

        :return Dict[int, bool]: Slot -> whether it had filament
        """
        now = self.reactor.monotonic()
        if (self.prep_seen is not None
            and now < self.prep_seen[0]):
            return self.prep_seen[1]
        seen: Dict[int, bool] = {}
        until = now + self.prep_sample
        while True:
            for slot, entry in (self.status().get("slots") or {}).items():
                seen[slot] = seen.get(slot, False) or bool(entry.get("present"))
            if self.reactor.monotonic() >= until:
                break
            self.reactor.pause(self.reactor.monotonic() + 0.5)
        self.prep_seen = (self.reactor.monotonic() + PREP_SAMPLE_HOLD, seen)
        return seen

    def run_cmd(self, template: str, lane: AFCLane, keep_temp: bool = False,
                **kwargs: Any) -> Tuple[bool, str]:
        """
        Run a firmware IFS command for a lane.

        :param template: Command with `{slot}`, `{lane}` and kwargs fields
        :param lane: Lane
        :param keep_temp: Put the nozzle target back afterwards outside a print
        :param kwargs: More template fields
        :return Tuple[bool, str]: Success and error message
        """
        cmd = template.format(slot=self.slot_of(lane), lane=lane.name, **kwargs)
        self.logger.debug(f"AFC_IFS: {cmd}")
        self.busy = True
        target = None
        if keep_temp:
            target = self.printer.lookup_object("extruder").get_heater().target_temp
        try:
            self.gcode.run_script_from_command(cmd)
            return True, ""
        except Exception as e:
            return False, str(e)
        finally:
            if (target is not None
                and not self.afc.function.in_print()):
                self.gcode.run_script_from_command(f"M104 S{int(target)}")
            self.busy = False
            # Read slots and the head from scratch after our move
            self.pending.clear()
            self.head = None

    def unit_load_lane(self, cur_lane: AFCLane, cur_extruder: Any,
                       gcmd: Any = "callback") -> bool:
        """
        Load a lane into the toolhead through the firmware (load_sequence's hook).

        :param cur_lane: Lane to load
        :param cur_extruder: Extruder object, unused
        :param gcmd: The command the load runs under
        :return bool: True when the firmware reports the lane's slot in the head
        """
        swap = self.swap_from is not None
        self.swap_from = None
        slot = self.slot_of(cur_lane)
        ok, err = self.backend.load(cur_lane, swap)
        head = self.status().get("head_slot") or 0
        if (ok
            and head != slot):
            ok, err = False, f"{self.backend.name} reports slot {head or 'none'} in the toolhead"
        if not ok:
            self.afc.error.handle_lane_failure(cur_lane,
                                               f"IFS load of {cur_lane.name} failed: {err}",
                                               pause=self.afc.function.in_print(), gcmd=gcmd)
            self.sync_tool(head, now=True, gcmd=gcmd)
            return False
        cur_lane.loaded_to_hub = True
        cur_lane.status = AFCLaneState.TOOL_LOADED
        self.afc.save_vars()
        return True

    def unit_unload_lane(self, cur_lane: AFCLane, cur_extruder: Any,
                         gcmd: Any = "callback") -> bool:
        """
        Unload a lane from the toolhead (unload_sequence's hook), or leave the
        unload to the next load when the firmware does both in one change.

        :param cur_lane: Lane to unload
        :param cur_extruder: Extruder object, unused
        :param gcmd: The command the unload runs under
        :return bool: True when the lane left the toolhead
        """
        nxt = self.afc.next_lane_load
        if (nxt in self.slots_by_lane
            and nxt != cur_lane.name
            and self.backend.fold_unload(cur_lane, nxt)):
            self.swap_from = cur_lane.name
        else:
            self.swap_from = None
            ok, err = self.run_cmd(self.unload_cmd, cur_lane, keep_temp=self.keep_temp)
            if (ok
                and (self.status().get("head_slot") or 0) == self.slot_of(cur_lane)):
                ok, err = False, "the lane is still in the toolhead"
            if not ok:
                self.afc.error.handle_lane_failure(cur_lane,
                                                   f"IFS unload of {cur_lane.name} failed: {err}",
                                                   pause=self.afc.function.in_print(),
                                                   gcmd=gcmd)
                return False
        if self.afc.post_unload_macro is not None:
            self.gcode.run_script_from_command(self.afc.post_unload_macro)
        cur_lane.set_tool_unloaded(normal_toolchange=True, gcmd=gcmd)
        cur_lane.status = AFCLaneState.LOADED if cur_lane.prep_state else AFCLaneState.NONE
        self.afc.save_vars()
        return True

    def eject_lane(self, lane: AFCLane, gcmd: Any = "callback") -> None:
        """
        Retract a lane's filament to its IFS slot.

        :param lane: Lane to eject
        :param gcmd: The command the eject runs under
        """
        slot = self.slot_of(lane)
        st = self.status()
        if (st.get("connected")
            and not (st.get("slots") or {}).get(slot, {}).get("present")):
            # A runout: nothing left in the slot to pull back. A lane still in
            # the nozzle (its unload folded into the next IFS_SWAP) is taken
            # out by that swap; ejecting here would unload it twice.
            self.logger.info(f"AFC_IFS: {lane.name} - IFS slot {slot} is empty, nothing to"
                             " eject")
            return
        self.await_empty.add(lane.name)
        ok, err = self.backend.eject(lane)
        if not ok:
            self.afc.error.AFC_error(f"IFS eject of {lane.name} failed: {err}", pause=False,
                                     gcmd=gcmd)
            return
        self.gcode.respond_info(f"AFC_IFS: {lane.name} - pull the filament out of IFS slot"
                                f" {self.slot_of(lane)}")

    def lane_unload(self, cur_lane: AFCLane, gcmd: Any = "callback") -> bool:
        """
        LANE_UNLOAD: eject the lane.

        :param cur_lane: Lane to unload
        :param gcmd: The LANE_UNLOAD command
        :return bool: Always True
        """
        self.eject_lane(cur_lane, gcmd=gcmd)
        return True

    def prep_load(self, lane: AFCLane, gcmd: Any = "callback") -> None:
        """
        The IFS loads from its slot itself.

        :param lane: Lane
        """

    def prep_post_load(self, lane: AFCLane, gcmd: Any = "callback") -> None:
        """
        The IFS loads from its slot itself.

        :param lane: Lane
        """

    def lane_move(self, cur_lane: AFCLane, distance: float, speed_mode: Any) -> None:
        """
        IFS lanes have no AFC lane motor.

        :param cur_lane: Lane
        :param distance: Requested distance
        :param speed_mode: Requested speed mode
        """
        self.logger.info("LANE_MOVE is not available on IFS lanes")

    def get_lane_reset_command(self, lane: AFCLane, dis: float) -> None:
        """
        IFS lanes have no lane reset move.

        :param lane: Lane
        :param dis: Distance
        """
        return None

    def calibration_lane_message(self) -> str:
        """
        Message for AFC's calibration commands.

        :return str: The IFS needs no AFC calibration
        """
        hint = self.backend.calibration_hint if self.backend is not None else ""
        return f"The IFS needs no AFC calibration{hint}."

    def calibrate_lane(self, cur_lane: AFCLane, tol: float,
                       gcmd: Any = "callback") -> Tuple[bool, str, int]:
        """
        No lane calibration on the IFS.

        :param cur_lane: Lane
        :param tol: Tolerance
        :return Tuple[bool, str, int]: Failure and the calibration message
        """
        return False, self.calibration_lane_message(), 0

    def calibrate_bowden(self, cur_lane: AFCLane, dis: float,
                         tol: float, gcmd: Any = "callback") -> Tuple[bool, str, int]:
        """
        No bowden calibration on the IFS.

        :param cur_lane: Lane
        :param dis: Distance
        :param tol: Tolerance
        :return Tuple[bool, str, int]: Failure and the calibration message
        """
        return False, self.calibration_lane_message(), 0

    def calibrate_hub(self, cur_lane: AFCLane, tol: float,
                      gcmd: Any = "callback") -> Tuple[bool, str, int]:
        """
        No hub calibration on the IFS.

        :param cur_lane: Lane
        :param tol: Tolerance
        :return Tuple[bool, str, int]: Failure and the calibration message
        """
        return False, self.calibration_lane_message(), 0

    def calibrate_td1(self, cur_lane: AFCLane, dis: float, tol: float,
                      gcmd: Any = "callback") -> Tuple[bool, str, int]:
        """
        No TD-1 on the IFS.

        :param cur_lane: Lane
        :param dis: Distance
        :param tol: Tolerance
        :return Tuple[bool, str, int]: Failure and a message
        """
        return False, "TD-1 is not supported on IFS lanes", 0

    def select_lane(self, lane: AFCLane, sel_dir: bool = False,
                    disable_selector: bool = False, *,
                    gcmd: Any = "callback") -> Tuple[bool, int]:
        """
        The IFS selects its slot itself.

        :param lane: Lane
        :param sel_dir: Unused
        :param disable_selector: Unused
        :return Tuple[bool, int]: Success
        """
        return True, 0

    def return_to_home(self, prep: bool = False, *, gcmd: Any = "callback") -> bool:
        """
        The IFS has no selector to home.

        :param prep: Unused
        :return bool: Always True
        """
        return True

    def check_runout(self, cur_lane: AFCLane) -> bool:
        """
        AFC's runout path from the slot sensor, for the lane in the toolhead while printing.

        :param cur_lane: Lane whose slot went empty
        :return bool: True when AFC should handle the runout
        """
        return (bool(self.afc_runout)
                and cur_lane.name == self.afc.function.get_current_lane()
                and self.afc.function.is_printing()
                and cur_lane.status not in (AFCLaneState.EJECTING, AFCLaneState.CALIBRATING))


def load_config_prefix(config: ConfigWrapper) -> afcIFS:
    """
    Klipper entry point for `[AFC_IFS <name>]`.

    :param config: Configuration of the section
    :return afcIFS: The unit
    """
    return afcIFS(config)
