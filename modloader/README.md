# WolfSDK 0.3.0

Mod loader and modding studio for **Wolfenstein: The New Order** (id Tech 5)
and **Wolfenstein II: The New Colossus** (id Tech 6). Windows, Steam versions
of both games. Free, MIT license (see [LICENSE](LICENSE)).

Two packages:

- **WolfSDK Loader** (`WolfSDK-Loader-0.3.0.zip`) for players: tick mods,
  apply them, start the game, revert to the original.
- **WolfSDK Studio** (`WolfSDK-Studio-0.3.0.zip`) for modders: the same
  loader plus the Studio, which browses the games' assets and saves your
  changes as mods.

Website and Discord: https://lopekinz.github.io/wolfenstein-modloader/

## For players: WolfSDK Loader

You do **not need Python**. The package brings its own (Python 3.11 from
python.org). The loader window is a local page in its own app window of
Microsoft Edge, which comes with Windows (else Chrome, else your default
browser); nothing of it goes online.

1. **Download:** `WolfSDK-Loader-0.3.0.zip`. Modders take
   `WolfSDK-Studio-0.3.0.zip` instead: the same loader plus the Studio.
2. **Extract:** right-click the ZIP → *Extract All…*, into a normal folder,
   e.g. `Documents\WolfSDK`. Do not start it straight from inside the ZIP
   (then `WolfSDK.cmd` only tells you to extract the ZIP first), and do not
   put it in `C:\Program Files`: the loader saves its settings in its own
   folder.
3. **Start:** double-click `WolfSDK.cmd`. A black window flashes briefly,
   then the loader opens in its own window. It finds the games through Steam
   by itself. If it does not, use *Choose folder…* in the game menu at the
   top left. If Windows warns you on
   the first start ("Windows protected your PC"), click *More info → Run
   anyway*. If the loader does not start, the black window usually stays open
   with the error message; the same text is then in
   `%LOCALAPPDATA%\wolfsdk\start.log`.
4. **Mods:** tick them on the *Mods* page, click *Apply mods*, then *Start
   game*. The game must be closed while mods are applied or reverted. Later
   a double-click on `Spielen.cmd` ("play") is enough: it applies the ticked
   mods if needed and starts the game.
5. **Back to the original:** *Revert* restores the game files bit for bit
   from the loader's backups. As a last resort, in Steam: Properties →
   Installed Files → *Verify integrity of game files*.

### What the loader does

- **Mods:** applies the ticked mods into the game's archives and reverts them
  again from its backups, for both games. Each game has its own mods, its own
  backups and its own *Revert*.
- **Cheats & QoL:** a searchable catalogue of the engine's own cvars, with
  the game's descriptions: all 3,116 for *The New Colossus* and all 3,186 for
  *The New Order* (its 31 measured tweaks first). Changes are saved at once.
  For *The New Order* they are passed when the game starts and
  change no file. For *The New Colossus*, the cvars the retail console
  accepts are passed at start and the rest go into the game's `default.cfg`;
  *Revert* removes them again.
- **Updates:** when a new WolfSDK version is out on GitHub, the loader says
  so at the top right, with the release notes and a download link. It asks
  GitHub once per start (at most every 6 hours) for the list of releases and
  sends nothing else; *Info* → *Check for updates on start* turns it off.
- **Presets** (*The New Colossus*, on *Cheats & QoL*): *Ultra+* (17 cvars),
  *Clean Image* (11) and *Photo Mode* (3, screenshot quality only).
- **Custom map** (*The New Colossus*): on the *Overview* page under *Quick
  actions*, *Install map…* takes a finished map file (`chunk_22.resources`)
  and sets it up as its own DLC folder `dlc\dlc_0`; *Remove map* takes it out
  again. The loader cannot build a map; the file has to come from someone
  who built it.
- **The New Order:** the console opens with **Ctrl+^** (Ctrl + the key left
  of 1). Cheat cvars are also sent as `+toggle`, so the engine's cheat reset
  at startup does not wipe them. F1–F4 get default binds (god mode, ammo,
  free camera, HUD).

Mods for *The New Colossus* that come with it:

- **Sandbox Tools:** F6 toggles god mode, Notarget and infinite ammo; F7
  kills all enemies. Bind the keys once in the game's console (binds are
  saved): `bind F6 "resourceExec snapdemo.cfg -s"` and
  `bind F7 "resourceExec snapedit.cfg -s"`.
- **Dev Menu Map Loading:** the developer menu's map entries load maps again
  (each entry runs `devmap`), and 126 developer start points are restored.
  The menu also lists *WolfSDK: Mod test map*, a copy of c2v1 with 11
  weapons and 8 enemies for trying out mods. The map file itself is **not**
  in this package: that entry only works when the test map is installed in
  `dlc\dlc_0`.
- **Hidden Menu Options:** turns on menu options the retail game hides: the
  main menu entry to the developer menu, the SAS menu, the hardest
  difficulty *Mein Leben!* without finishing the game first, and the photo
  mode setting.
- **Photo Mode:** F8 freezes time, turns on a free camera and hides the HUD
  and menus; F8 again returns. Bind it once in the console:
  `bind F8 "resourceExec default_snap.cfg -s"`.

### Confirmed in game, and what is not yet

**Confirmed in game:**

- Applying and reverting mods with backups.
- Installing a custom map.
- Sandbox Tools: god mode and infinite ammo.
- Dev Menu Map Loading: the menu runs `devmap` and loads the map.
- *WolfSDK: Mod test map* loads.

**Not yet confirmed in game:**

- The 126 restored developer start points.
- Sandbox Tools: Notarget and F7 (kill all enemies).
- Hidden Menu Options, the presets and the Photo Mode mod.
- *The New Colossus*: cvars written into `default.cfg`.
- *The New Order*: the console with Ctrl+^, cheat cvars kept via `+toggle`,
  and the F1–F4 binds; the cvars beyond the 31 measured tweaks are sent the
  same way but were not tried one by one.
- Most other shipped mods are only checked structurally. Unless a mod's
  description says otherwise, treat it as untested in game.

### Limits, honestly

- **Steam version only.** Microsoft Store/Game Pass locks the game files, it
  does not work there. GOG and Epic are untested.
- **German, censored version:** it has different textures. Skins made from
  the international version probably do not apply there. This is unverified.
- **Custom maps** need the DLC *The Freedom Chronicles*, episode 2: the
  custom map takes the place of the map c2v1 and is loaded in the game
  through that episode. If mods are currently applied, the loader usually
  asks for a *Revert* first before installing or removing a map.
- **No default F-key binds in *The New Colossus*:** a key bound there cannot
  reach `toggle` or `noclip`. Use the Sandbox Tools mod instead.
- **Disk space:** about 1 GB free on the game's drive for the backups. With
  *The New Order* it can be more depending on the mods, up to about 3 GB. If
  there is not enough space, the loader stops before it writes any mods (if
  mods were already applied, they are reverted afterwards).
- **Back up your saves first:** copy the folder
  `%USERPROFILE%\Saved Games\MachineGames` once. Whether old saves load
  cleanly with and without every mod has not been checked for every mod.
- **The game must be closed** while the loader applies or reverts mods. After
  a game update, *Revert* first.
- **Do not tick two skins for the same weapon** at the same time. The loader
  only refuses this by itself when both change the same textures.
- **So far tested on one PC only.** If something does not work, a report
  with the text from the loader window or from
  `%LOCALAPPDATA%\wolfsdk\start.log` helps.

### Support

WolfSDK is free and stays free. If it helps you, you can
[buy the developer a coffee](https://buymeacoffee.com/swscc5yr7rs). Nothing
is locked behind it.

<!-- Studio part: tools/build_release.py packs up to here into the loader ZIP, up to the next marker into the Studio ZIP. -->

## For modders: WolfSDK Studio

`WolfSDK-Studio-0.3.0.zip` is the loader from above plus the Studio.
Extract and start it the same way. The Studio needs *Wolfenstein II: The New
Colossus*; it finds *The New Order* by itself if that is installed too.

1. **Start:** in the loader, page *Studio* → *Open Studio*. The Studio is a
   local page (served on `127.0.0.1` by the loader, nothing goes online) and
   opens in its own full-screen window: Edge or Chrome, otherwise your
   default browser. It keeps running while its window is open, also when
   the loader window is closed. Without the loader:
   `python\python.exe -m wolfsdk studio` in the extracted folder (Ctrl+C
   quits).
2. **Browse:** maps, videos, audio, textures and texts of both games.
   Cutscenes play with their sound in sync with the video. A 3D viewer shows
   the 17,315 models of *The New Colossus*, plays their animations and
   exports them as GLB, glTF or OBJ.
3. **Replace:** BC1 textures, texts and decls, and for *The New Colossus*
   also sounds (16-bit PCM WAV) and videos (Bink 2). Each replacement is
   saved as a mod in the `mods` folder. It then shows up on the loader's *Mods* page:
   tick it, *Apply mods*, *Start game*. Applying mods closes the Studio,
   because it keeps the game files open.

New in 0.3.0:

- **Animations** in the model viewer: pick one of a model's clips and play it.
- **Faster models:** textures load as they are needed, material maps in the
  background.
- **Wolfenstein: Youngblood** (preview, read only, if installed): texts,
  scripts, videos, textures and models with animations. No maps, no export
  and no replacing yet.
- **Sounds and videos** (*The New Colossus*) can be replaced now; *Revert*
  restores the original sound packs and video files.

**Not yet tried in game:**

- **Replaced sounds and videos.**
- **Normal maps (BC5):** replace them like any other texture. The PNG must
  be exactly the texture's size, with the green channel in DirectX
  convention.
- **Script and mission editor** (*Scripts*): 3,010 Kiscule scripts as node
  graphs, all 205 node types. Change parameters and connections, insert
  nodes, add and remove mission objectives. Edits accumulate over several
  saves into one mod.

**Planned:** a map editor, map import from Blender (glTF), the models of
*The New Order*, maps of *Youngblood*, and adding new assets instead of only
replacing existing ones.
