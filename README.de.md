# Wolfenstein Modloader

[🇬🇧 English](README.md) · 🇩🇪 Deutsch

Community-Dokumentation und Reverse-Engineering-Forschung zum Modden von:

- **Wolfenstein: The New Order** (id Tech 5)
- **Wolfenstein II: The New Colossus** (id Tech 6)

> [!NOTE]
> Dieses Repository veröffentlicht die Dokumentation und das **Wolfenstein-SDK**
> in vier Programmiersprachen. Mods, Spieldateien und große
> Forschungsartefakte sind nicht Teil der öffentlichen Veröffentlichung.

## SDK

Das SDK macht aus den dokumentierten Formaten Code. Jede Sprachanbindung
implementiert dieselbe [sprachneutrale Spezifikation](spec/SPEC.md) und wird
gegen dieselben [gemeinsamen Test-Vektoren](spec/vectors/) getestet.

| Sprache | Paket | Installation | Doku |
|---|---|---|---|
| Python (Referenz + CLI) | `wolfenstein-sdk` auf PyPI | `pip install wolfenstein-sdk` | [sdk/python](sdk/python/README.md) |
| TypeScript / Node.js | `wolfenstein-sdk` auf npm | `npm install wolfenstein-sdk` | [sdk/typescript](sdk/typescript/README.md) |
| Rust | `wolfenstein-sdk` auf crates.io | `cargo add wolfenstein-sdk` | [sdk/rust](sdk/rust/README.md) |
| Go | `github.com/LopeKinz/wolfenstein-modloader/sdk/go` | `go get github.com/LopeKinz/wolfenstein-modloader/sdk/go` | [sdk/go](sdk/go/README.md) |

Umfang (Archive von *The New Order*; das Decl-Format beider Spiele):

| Bereich | Funktionen |
|---|---|
| Archive | `master.index`, `chunkN.index` (bytegenau erhalten), Raw-DEFLATE mit Sync-Flush, Schreiben in den bestehenden Slot, geordneter Neuaufbau, Prüfung der Invarianten |
| Decls | Parser/Editor für beide Dialekte, der nur den geänderten Wert ersetzt; Punkt-Pfade; Zahlen behalten ihr Originalformat |
| Mods | `mod.json` ([Schema](spec/mod.schema.json)), Schichtung nach Priorität, Konfliktberichte |
| Bilder | BIM-Header, Dekodieren von RGBA8/A8/BC1/BC3, RGBA8-Kodierung, PNG lesen/schreiben |
| Audio | `bsnf`-Deskriptoren, Offsets in den Streamed-Containern, Längen von Ogg-Streams |
| Spielstände | `MD5_BlockChecksum`-Header: berechnen, prüfen, reparieren |

Das Python-Paket enthält zusätzlich das Kommandozeilenwerkzeug `wolfsdk` mit
einem protokollierenden Patcher (`apply` / `revert`):

```bash
pip install wolfenstein-sdk
wolfsdk -g "C:/Spiele/Wolfenstein The New Order" paths damage:damage/tungsten/mg60
wolfsdk -g "C:/Spiele/Wolfenstein The New Order" plan mods/schnelle_schrotflinte   # Probelauf mit Diff
wolfsdk -g "C:/Spiele/Wolfenstein The New Order" apply mods/schnelle_schrotflinte
wolfsdk -g "C:/Spiele/Wolfenstein The New Order" revert                            # alles rückgängig
```

Eine Mod ist eine `mod.json`:

```json
{
  "name": "Schnelle Schrotflinte",
  "game": "tno",
  "priority": 10,
  "assets": {
    "weapon:weapon/shotgun_base": {
      "set": {
        "edit.firingIntervals.firingIntervals[0]": 120,
        "edit.validAmmoClips.item[0].clipSize": 40
      }
    }
  }
}
```

`apply` schreibt in die vorhandenen Archiv-Slots (Offsets und `csize` bleiben
unverändert). Passt eine Änderung nicht hinein, wird nur das betroffene Archiv
in Originalreihenfolge neu aufgebaut. Jede Änderung landet im Journal
`base/_wolfsdk_backup/journal.json`; `revert` stellt die Dateien bytegenau
wieder her. Nach einem Spiel-Update zuerst `revert` ausführen.

Noch nicht abgedeckt: der Oodle-komprimierte `IDCL`-Container von *The New
Colossus*, Pixeldaten der virtuellen Texturen und BC1/BC3-Kompression.

Veröffentlichung: Ein Tag `vX.Y.Z` startet
[`sdk-release.yml`](.github/workflows/sdk-release.yml) und veröffentlicht alle
vier Pakete. `python scripts/sync_version.py X.Y.Z` setzt die Version überall.

## Dokumentation

Die vollständige (englische) Dokumentation steht im
**[Wolfenstein Modloader Wiki](https://github.com/LopeKinz/wolfenstein-modloader/wiki)**.

Empfohlene Einstiegspunkte:

- [Modding Guide](https://github.com/LopeKinz/wolfenstein-modloader/wiki/Modding-Guide)
- [File Formats](https://github.com/LopeKinz/wolfenstein-modloader/wiki/File-Formats)
- [Modding Limits](https://github.com/LopeKinz/wolfenstein-modloader/wiki/Modding-Limits)
- [The New Order Modding Surface](https://github.com/LopeKinz/wolfenstein-modloader/wiki/TNO-Modding-Surface)
- [The New Colossus Modding Surface](https://github.com/LopeKinz/wolfenstein-modloader/wiki/TNC-Modding-Surface)
- [Reverse Engineering Research](https://github.com/LopeKinz/wolfenstein-modloader/wiki/Reverse-Engineering-Research)
- [Known Contradictions](https://github.com/LopeKinz/wolfenstein-modloader/wiki/Known-Contradictions)

## Belegstandards

Die Dokumentation unterscheidet bewusst zwischen:

- **Gemessen** — zur Laufzeit, auf der Festplatte oder an echten Spieldaten bestätigt.
- **Abgeleitet** — durch Binäranalyse oder strukturelle Belege gestützt, im
  laufenden Spiel aber noch nicht bestätigt.
- **Vermutet** — eine plausible Spur, die noch überprüft werden muss.

Negative Ergebnisse, gescheiterte Ansätze und offene Fragen bleiben erhalten,
damit Leser gesichertes Verhalten von laufender Forschung unterscheiden können.

## Aktueller Umfang

Das Wiki behandelt Archiv- und Ressourcenformate, Decls, Audio, UI-Assets,
Karten, Spielstände, den Entwicklermodus, Ladepfade, Patch-Vorrang, Texturen,
Kiscule-Skripting, Forschung zu eigenen Karten sowie ausführliche
Reverse-Engineering-Berichte.

Große generierte Artefakte aus dem Forschungsarbeitsbereich sind über ein
[Artefakt-Verzeichnis](https://github.com/LopeKinz/wolfenstein-modloader/wiki/Research-Artifacts)
erfasst, statt im Wiki-Repository dupliziert zu werden.

## Unterstützen

WolfSDK ist kostenlos und bleibt es. Wenn es dir hilft, kannst du dem Entwickler [einen Kaffee spendieren](https://buymeacoffee.com/swscc5yr7rs). Dafür wird nichts freigeschaltet.

## Haftungsausschluss

Dies ist ein unabhängiges Community-Projekt. Es steht in keiner Verbindung zu
MachineGames, Bethesda Softworks, ZeniMax Media oder Microsoft und wird von
diesen nicht unterstützt. Wolfenstein und die Namen der genannten Spiele sind
Marken ihrer jeweiligen Inhaber.

Verbreite keine urheberrechtlich geschützten Spieldateien. Nutze Dokumentation
und Werkzeuge nur mit Spieldateien, auf die du rechtmäßig zugreifen darfst.

Der Quellcode des SDK steht unter der [MIT-Lizenz](LICENSE).
