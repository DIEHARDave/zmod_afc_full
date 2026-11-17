# Zmod AFC Full

A [Zmod](https://github.com/ghzserg/zmod) plugin for the Flashforge AD5X. It
swaps Zmod's built-in AFC panel emulation (`zmod_afc`) for the real
[AFC-Klipper-Add-On](https://github.com/AFCProject/AFC-Klipper-Add-On), with
the IFS working as an AFC unit. AFC then owns tool changes, lane mapping,
runout and infinite spool, and Spoolman lanes. Zmod still moves the filament
(its IFS loads, unloads, cuts, purges and heats).

It works on stock Zmod 1.7 and doesn't change any of Zmod's files. The changes
it needs are made at runtime while Klipper runs (see
[How it works](#how-it-works)).

**Status: test release.** Everything here was hardware-tested on an AD5X as
part of the earlier Zmod test branch (multi-color prints, first load, runout
and infinite spool, jam pause and resume, Spoolman, reboots). The plugin
version has passed the emulator suite and is being tested on hardware now.

## Requirements

- An AD5X on Zmod 1.7.5 or newer.
- The native screen turned off (`DISPLAY_OFF`). Full AFC only starts without
  it. HelixScreen, Fluidd and Mainsail all work.
- `zmod_afcplus` must be off. The installer turns it off for you.

## Install

1. Add this to `mod_data/user.moonraker.conf`:

   ```ini
   [update_manager zmod_afc_full]
   type: git_repo
   channel: dev
   path: /root/printer_data/config/mod_data/plugins/zmod_afc_full
   origin: https://github.com/DIEHARDave/zmod_afc_full.git
   is_system_service: False
   primary_branch: main
   ```

2. Restart Moonraker, then in the Fluidd/Mainsail console run:

   ```
   ENABLE_PLUGIN name=zmod_afc_full
   ```

Klipper restarts twice. The first start writes the AFC config, and the second
loads AFC. About 15 s later AFC checks every lane (PREP). The AFC panel should
then list `lane1`–`lane4`, with the loaded slots shown as "LOCKED AND LOADED".

Updates come through Fluidd/Mainsail's update manager. Restart Klipper after
an update.

## Remove

```
DISABLE_PLUGIN name=zmod_afc_full
```

After Klipper restarts, Zmod's built-in AFC panel is back. AFC's saved lane
state (`mod_data/AFC/AFC.var*`) is kept for a later reinstall. Turn
`zmod_afcplus` back on if you used it: `ENABLE_PLUGIN name=zmod_afcplus`.

## Settings

To change an AFC option, add the same section with your option to
`mod_data/user.cfg`. Yours wins, because `user.cfg` loads after plugins. The
defaults are in [ad5x_afc.cfg](ad5x_afc.cfg).

`[zmod_afc_full]` has one option, `restart_on_change` (default `True`). It
restarts Klipper by itself when the AFC config has to change (see below).

## IFS Jacker (more than 4 slots)

With ninjamida's `ifs_jacker` plugin enabled, AFC gets a lane for every Jacker
slot (8, 12 or 16):

1. Start the printer with the Jacker connected.
2. After about 30 s, AFC.log says `the IFS has 8 slots, AFC has no lane for
   slot 5, 6, 7, 8. Reboot the printer`. Restart Klipper once.
3. After that, the AFC panel shows `lane5`–`lane8` (T4–T7).

This was tested in an emulator with 2 IFS units, not on a real Jacker yet.

## Another AFC unit instead of the IFS (experimental)

AFC can also drive its own unit in place of the IFS: a BoxTurtle (AFC-Lite or
AFC-Pro board), NightOwl, QuattroBox, HTLF, ViViD and so on. AFC moves the
filament itself. `AFC_FF.py` turns the AD5X's analog head sensor into AFC's
toolhead sensor, and Zmod's macros cut (`_CUT_PRUTOK`), park and purge in the
chute (`_GOTO_TRASH`, `_SBROS_TRASH_DAVIM`) and wipe (`_CLEAR_REZINA`).

1. Connect the unit's board over USB. Flash it with Klipper firmware built from
   the Klipper version Zmod runs.
2. Create `mod_data/afc_unit.cfg` with the unit: its `[mcu]`
   (`serial: /dev/serial/by-id/usb-Klipper_...`) and its unit, hub and lane
   sections. Take them from AFC's `templates/` and `config/mcu/`, for example
   `AFC_Turtle_1.cfg` with `AFC_Lite.cfg`.
3. Restart Klipper. With that file present the plugin loads
   [ad5x_afc_ff.cfg](ad5x_afc_ff.cfg) and your unit instead of the IFS config.
   Delete the file and restart to go back to the IFS.
4. Measure `tool_stn` and `tool_stn_unload` (head sensor to nozzle). Set them
   under `[AFC_extruder extruder]` in `mod_data/user.cfg`.

So far this has only been tested in the emulator with a simulated BoxTurtle,
not with a real unit.

## Moving from the test branch

If your Zmod is on the `feat/full-afc` test branch (DIEHARDave/z_ad5x), the
plugin stays off until Zmod is back on ghzserg's branch:

1. In the console, run `SAVE_ZMOD_DATA AFC=1`.
2. Over SSH, switch Zmod back:

   ```sh
   chroot /usr/data/.mod/.zmod sh -c "cd /usr/data/zmod/zmod && \
     git remote set-url origin https://github.com/ghzserg/z_ad5x.git && \
     git fetch origin +1.7:refs/remotes/origin/1.7 && \
     git checkout -B 1.7 origin/1.7"
   ```

3. Reboot, then install the plugin as above.

Your lanes, spools and runout lanes carry over, because AFC keeps them in the
same `mod_data/AFC/AFC.var`.

## How it works

At every Klipper start, `zmod_afc_full.py`:

- Links AFC's modules (`AFC/extras`, a copy of AFC-Klipper-Add-On at the
  commit in `AFC/AFC_COMMIT`), `AFC_IFS.py` and `AFC_FF.py` into
  `klippy/extras`, next to its own link.
- Writes `mod_data/AFC/zmod_afc_full_active.cfg`, which the plugin's cfg
  includes. It holds the IFS config plus any Jacker lanes, the unit config, or
  nothing while the native screen is on. When that file changes, Klipper
  restarts once.

While AFC is loaded, it changes Zmod at runtime:

- **zmod_afc** stands down. Its commands are removed before AFC registers its
  own, and its panel objects are never created.
- **zmod_color** stops writing `lane_data`, because AFC writes it.
- **Print tool changes.** Zmod's `virtual_sdcard` turns each `T<n>` line into
  `_A_CHANGE_FILAMENT`. The plugin sends that call to AFC's `T<n>`, and
  `AFC_IFS` then runs Zmod's own `_A_CHANGE_FILAMENT` for the swap. If a pause
  comes before or during the change, the `T` line stays unread, so the change
  runs again after `RESUME`.
- **zmod_ifs.** `SET_CURRENT_PRUTOK` (the first load at print start) goes
  through AFC's `T<n>`. `ANALOG_PRUTOK` (head-sensor runout) goes to `AFC_IFS`,
  which runs the infinite spool or pauses with an AFC message. IFS autoinsert
  waits for the G-code lock, so it can't collide with a load.

## Known limitations

- Cutting filament mid-print can trigger Zmod's IFS jam pause instead of a
  runout. That comes from Zmod's IFS motion-sensor logic.
- `lane_data` follows the idle lane map (lane N = T(N-1)).
- A Klipper firmware update that replaces `klippy/extras` removes the plugin's
  link. Run `ENABLE_PLUGIN name=zmod_afc_full` again.

## Reporting problems

Include `/usr/data/logs/AFC.log`, `klippy.log`, the time it happened and what
you did.

## License

GPLv3 ([LICENSE](LICENSE)), the same as AFC-Klipper-Add-On, which is vendored
in `AFC/` unchanged.
