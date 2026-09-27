/* Shared by every page: glossary, hints, language. */
/* Glossary: key -> [en name, en text, de name, de text, pattern for auto-hints in English text, core?] */
const TERMS = {
  modding: ["Modding", "Changing a game with your own content or values.", "Modding", "Ein Spiel mit eigenen Inhalten oder Werten verändern.", "\\bmod(ding|ders?)\\b", 1],
  re: ["Reverse engineering", "Finding out how a program works by examining it from the outside, without its source code.", "Reverse Engineering", "Herausfinden, wie ein Programm funktioniert, indem man es von außen untersucht – ohne Quellcode.", "\\breverse[- ]engineer(ing|ed)?\\b", 1],
  engine: ["Engine (id Tech)", "The technical foundation a game is built on. The New Order uses id Tech 5, The New Colossus id Tech 6.", "Engine (id Tech)", "Das technische Fundament eines Spiels. The New Order nutzt id Tech 5, The New Colossus id Tech 6.", "\\bid Tech [56]\\b", 1],
  retail: ["Retail", "The normal version of the game as sold, not a developer build.", "Retail", "Die normale, verkaufte Version des Spiels, kein Entwickler-Build.", "\\b[Rr]etail\\b", 1],
  archive: ["Archive", "A large file that bundles thousands of game files, a bit like a zip file.", "Archiv", "Eine große Datei, die tausende Spieldateien bündelt, ähnlich wie eine Zip-Datei.", "\\barchives?\\b", 1],
  slot: ["Slot", "The exact space a file takes up inside an archive. A changed file has to fit into it.", "Slot", "Der genaue Platz, den eine Datei im Archiv belegt. Eine geänderte Datei muss hineinpassen.", "\\bslots?\\b", 1],
  decl: ["Decl", "Short for declaration: a text file that describes one thing in the game, such as a weapon or an enemy.", "Decl", "Kurz für Deklaration: eine Textdatei, die ein Ding im Spiel beschreibt, etwa eine Waffe oder einen Gegner.", "\\bDecls?\\b", 1],
  checksum: ["Checksum", "A short number calculated from a file. If the file changes, the number changes too.", "Prüfsumme", "Eine kurze Zahl, aus einer Datei berechnet. Ändert sich die Datei, ändert sich auch die Zahl.", "\\bchecksums?\\b", 1],
  hash: ["Hash (fingerprint)", "A fingerprint of data. The game uses it to notice when a file was changed.", "Hash (Fingerabdruck)", "Ein Fingerabdruck von Daten. Das Spiel merkt daran, ob eine Datei verändert wurde.", "\\bhash(es)?\\b", 1],
  encryption: ["Encryption", "Scrambling data so that only someone with the key can read it.", "Verschlüsselung", "Daten so verwürfeln, dass nur jemand mit dem Schlüssel sie lesen kann.", "\\b(encrypted|encryption|decrypt(ed|ion)?)\\b", 1],
  key: ["Key", "The secret value that locks and unlocks encrypted data.", "Schlüssel", "Der geheime Wert, der verschlüsselte Daten ver- und entschlüsselt.", "\\bkey derivation\\b", 1],
  idcl: ["IDCL container", "The archive format of Wolfenstein II. Each archive carries fingerprints of its own contents.", "IDCL-Container", "Das Archivformat von Wolfenstein II. Jedes Archiv trägt Fingerabdrücke seines eigenen Inhalts.", "\\bIDCL\\b", 1],
  bim: ["BIM image", "The game's own picture format, used for menus and the HUD.", "BIM-Bild", "Das eigene Bildformat des Spiels, genutzt für Menüs und das HUD.", "\\bBIMs?\\b", 1],
  vt: ["Virtual textures", "One gigantic image that holds the surfaces of the whole game world. Also called megatexture.", "Virtuelle Texturen", "Ein riesiges Bild, das die Oberflächen der ganzen Spielwelt enthält. Auch Megatexture genannt.", "\\b([Vv]irtual [Tt]extures?|[Mm]egatexture)\\b", 1],
  mesh: ["Mesh", "The 3D shape of an object, built from many small triangles.", "Mesh", "Die 3D-Form eines Objekts, aufgebaut aus vielen kleinen Dreiecken.", "\\b(md6)?mesh(es)?\\b", 1],
  cvar: ["CVar", "A console variable: a named setting such as field of view or HUD on/off.", "CVar", "Eine Konsolenvariable: eine benannte Einstellung wie Sichtfeld oder HUD an/aus.", "\\b[Cc][Vv]ars?\\b", 1],
  console: ["Developer console", "A text input inside the game for commands and settings, normally meant for developers.", "Entwicklerkonsole", "Eine Texteingabe im Spiel für Befehle und Einstellungen, eigentlich für Entwickler gedacht.", "\\b[Dd]eveloper [Cc]onsole\\b|\\bconsole\\b", 1],
  alias: ["Map alias", "A copy of an existing map under a new name, so it can be changed without touching the original.", "Karten-Alias", "Eine Kopie einer bestehenden Karte unter neuem Namen, damit man sie ändern kann, ohne das Original anzufassen.", "\\balias(es)?\\b", 1],
  verify: [".verify file", "An extra file the game writes next to a save game to check it later.", ".verify-Datei", "Eine Zusatzdatei, die das Spiel neben einen Spielstand schreibt, um ihn später zu prüfen.", "\\.verify\\b", 1],
  kiscule: ["Kiscule", "The game's scripting system for missions and scripted events.", "Kiscule", "Das Skriptsystem des Spiels für Missionen und gescriptete Ereignisse.", "\\bKiscule\\b", 1],
  compression: ["Compression", "Packing data so it takes less space; the game unpacks it while loading.", "Kompression", "Daten so packen, dass sie weniger Platz brauchen; das Spiel entpackt sie beim Laden.", "\\b(compressed|compression|DEFLATE|deflate)\\b"],
  oodle: ["Oodle / Kraken", "A commercial compression library. Wolfenstein II packs many files with its Kraken method.", "Oodle / Kraken", "Eine kommerzielle Kompressionsbibliothek. Wolfenstein II packt viele Dateien mit deren Kraken-Verfahren.", "\\b(Oodle|Kraken)\\b"],
  crc: ["CRC32", "A common kind of checksum that turns data into a 32-bit number.", "CRC32", "Eine verbreitete Art Prüfsumme, die Daten in eine 32-Bit-Zahl verwandelt.", "\\bCRC-?32\\b"],
  entity: ["Entity (.entities)", "An object placed in a level: an enemy, a door, a light, a trigger. A map's .entities file lists them all.", "Entity (.entities)", "Ein Objekt in einem Level: Gegner, Tür, Licht, Auslöser. Die .entities-Datei einer Karte listet alle auf.", "\\.entities\\b"],
  mapresources: [".mapresources", "The list of every file a map needs, so the game can load them in advance.", ".mapresources", "Die Liste aller Dateien, die eine Karte braucht, damit das Spiel sie vorab laden kann.", "\\.mapresources\\b"],
  texdb: ["TexDB", "Wolfenstein II's texture database: an index that tells the game where each texture is stored.", "TexDB", "Die Texturdatenbank von Wolfenstein II: ein Verzeichnis, wo jede Textur liegt.", "\\b[Tt]ex[Dd][Bb]\\b"],
  chunk: ["Chunk", "One numbered archive file of the game, for example chunk0.resources.", "Chunk", "Eine nummerierte Archivdatei des Spiels, zum Beispiel chunk0.resources.", "\\bchunk_?\\d+\\.(index|resources)\\b"],
  dlc: ["DLC", "Downloadable content: add-on episodes that come with their own folders and archives.", "DLC", "Zusatzinhalte zum Herunterladen: Erweiterungen mit eigenen Ordnern und Archiven.", "\\bDLCs?\\b"],
  patchprec: ["Patch precedence", "The rule for which copy wins when the same file exists in several archives.", "Patch-Vorrang", "Die Regel, welche Kopie gewinnt, wenn dieselbe Datei in mehreren Archiven liegt.", "\\bprecedence\\b"],
  loose: ["Loose files", "Game files lying directly in a folder instead of being packed into an archive.", "Lose Dateien", "Spieldateien, die direkt in einem Ordner liegen statt in einem Archiv.", "\\bloose (assets|files)\\b"],
  ghidra: ["Ghidra", "A free tool for taking programs apart and reading their machine code.", "Ghidra", "Ein kostenloses Werkzeug, um Programme zu zerlegen und ihren Maschinencode zu lesen.", "\\bGhidra\\b"],
  rtti: ["RTTI", "Type names the compiler leaves inside a program; they reveal names of the game's internal classes.", "RTTI", "Typnamen, die der Compiler im Programm hinterlässt; sie verraten Namen interner Klassen des Spiels.", "\\bRTTI\\b"],
  rva: ["RVA (address)", "A position inside the loaded program, used to point at a specific function or table.", "RVA (Adresse)", "Eine Position im geladenen Programm, um auf eine bestimmte Funktion oder Tabelle zu zeigen.", "\\bRVAs?\\b"],
  steamstub: ["SteamStub", "Steam's protection wrapper; it keeps the game's code encrypted on disk until the game starts.", "SteamStub", "Die Schutzhülle von Steam; sie hält den Programmcode auf der Festplatte verschlüsselt, bis das Spiel startet.", "\\bSteamStub\\b"],
  bc: ["BC1 / BC3", "Standard ways to compress textures in 4×4 pixel blocks that graphics cards read directly.", "BC1 / BC3", "Übliche Verfahren, Texturen in 4×4-Pixel-Blöcken zu komprimieren, die Grafikkarten direkt lesen.", "\\b(BC[1-7]H?|DXT[15])\\b"],
  ogg: ["Ogg Vorbis", "A free audio format, similar to MP3. The games store their sounds in it.", "Ogg Vorbis", "Ein freies Audioformat, ähnlich wie MP3. Die Spiele speichern ihre Töne darin.", "\\bOgg( Vorbis)?\\b"],
  jpegxr: ["JPEG XR", "An image format by Microsoft. The virtual-texture pixels use a trimmed-down variant of it.", "JPEG XR", "Ein Bildformat von Microsoft. Die Pixel der virtuellen Texturen nutzen eine abgespeckte Variante davon.", "\\bJPEG[- ]XR\\b"],
  quadtree: ["Quadtree", "A tree where each square is split into four smaller squares; used to organise huge textures.", "Quadtree", "Ein Baum, in dem jedes Quadrat in vier kleinere geteilt wird; damit werden riesige Texturen geordnet.", "\\bquad-?trees?\\b"],
  devgui: ["DevGUI", "The developers' debug menu inside the game; the code is there, but the shop version mostly hides it.", "DevGUI", "Das Debug-Menü der Entwickler im Spiel; der Code ist da, die Kaufversion versteckt es aber meist.", "\\bDevGUI\\b"],
  gore: ["Gore", "Blood and wound effects. Settings and damage Decls control how strong they are.", "Gore", "Blut- und Wundeffekte. Einstellungen und Schadens-Decls steuern, wie stark sie sind.", "\\b[Gg]ore\\b"],
  entitydef: ["entityDef", "The Decl type that describes a kind of object, such as one enemy type with its health and weapons.", "entityDef", "Der Decl-Typ, der eine Objektart beschreibt, etwa einen Gegnertyp mit Leben und Waffen.", "\\bentityDefs?\\b"],
  material: ["Material", "Describes how a surface looks: which textures it uses and how light reacts to it.", "Material", "Beschreibt, wie eine Oberfläche aussieht: welche Texturen sie nutzt und wie Licht darauf reagiert.", "\\bmaterials?\\b"],
  inherit: ["Inheritance", "A Decl can take over all values of a parent Decl and change only a few (inherit = \"…\").", "Vererbung", "Eine Decl kann alle Werte einer Eltern-Decl übernehmen und nur einige ändern (inherit = \"…\").", "\\binherit(s|ance)?\\b"],
  collision: ["Collision", "The invisible shape the game uses to decide what you can walk on or bump into.", "Kollision", "Die unsichtbare Form, mit der das Spiel entscheidet, worauf man laufen oder wogegen man stoßen kann.", "\\bcollision\\b"],
  navmesh: ["Navmesh", "A map of walkable areas that enemies use to find their way.", "Navmesh", "Eine Karte begehbarer Flächen, mit der Gegner ihren Weg finden.", "\\b(hk)?nav-?mesh(es)?\\b"],
  binary: ["Binary", "Data meant for the computer, not readable as text.", "Binär", "Daten für den Computer, nicht als Text lesbar.", "\\bbinary\\b"],
  offset: ["Offset", "A position inside a file, counted in bytes from its start.", "Offset", "Eine Position in einer Datei, in Bytes vom Anfang gezählt.", "\\boffsets?\\b"],
  header: ["Header", "The first bytes of a file that describe what follows, like a label on a box.", "Header", "Die ersten Bytes einer Datei, die beschreiben, was folgt – wie ein Etikett auf einer Kiste.", "\\bheaders?\\b"],
  payload: ["Payload", "The actual content of a file or entry, without its header.", "Nutzdaten", "Der eigentliche Inhalt einer Datei oder eines Eintrags, ohne Header.", "\\bpayloads?\\b"],
  roundtrip: ["Round trip", "Reading a file and writing it back unchanged. If every byte matches, the format is understood.", "Round-Trip", "Eine Datei lesen und unverändert zurückschreiben. Stimmt jedes Byte, ist das Format verstanden.", "\\bround-?trips?\\b"],
  endian: ["Endianness", "The order in which the bytes of a number are stored: big-endian (largest first) or little-endian (smallest first).", "Byte-Reihenfolge", "Die Reihenfolge, in der die Bytes einer Zahl gespeichert werden: Big-Endian (größtes zuerst) oder Little-Endian (kleinstes zuerst).", "\\b([Bb]ig|[Ll]ittle)[- ][Ee]ndian\\b"],
  static: ["Static analysis", "Studying a program's code without running it.", "Statische Analyse", "Den Code eines Programms untersuchen, ohne es auszuführen.", "\\b[Ss]tatic (analysis|research|finding|probe)\\b"],
  runtime: ["Runtime", "While the game is actually running. Runtime evidence is the strongest proof.", "Laufzeit", "Während das Spiel tatsächlich läuft. Belege zur Laufzeit sind der stärkste Beweis.", "\\b[Rr]untime\\b"],
  sdk: ["SDK", "Software development kit: the project's own tools and code for reading and changing game files.", "SDK", "Software Development Kit: die eigenen Werkzeuge und der Code des Projekts zum Lesen und Ändern der Spieldateien.", "\\b(wolfsdk|SDK)\\b"],
  savegame: ["Save game", "A file that stores your progress so you can continue later.", "Spielstand", "Eine Datei, die deinen Spielfortschritt speichert, damit du später weiterspielen kannst.", "\\b[Ss]ave ?games?\\b"],
};

const WIKI_REMOTE = "https://github.com/LopeKinz/wolfenstein-modloader/wiki/";
const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
const hints = s => esc(s).replace(/\{(\w+)\|([^}]+)\}/g, (_, k, label) => TERMS[k] ? `<span class="t" data-t="${k}">${label}</span>` : label);
const both = (en, de, fn = esc) => `<span class="en">${fn(en)}</span><span class="de">${fn(de)}</span>`;

/* ---------- auto-hints: mark the first use of each term in technical text ---------- */
function autoHint(root) {
  const skip = "code, pre, a, h1, h2, h3, h4, h5, h6, .t, script, style, summary, th";
  const found = new Set();
  for (const [key, t] of Object.entries(TERMS)) {
    if (!t[4]) continue;
    const re = new RegExp(t[4]);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: n => n.parentElement.closest(skip) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
    });
    let node;
    while ((node = walker.nextNode())) {
      const m = re.exec(node.data);
      if (!m) continue;
      const span = document.createElement("span");
      span.className = "t";
      span.dataset.t = key;
      const rest = node.splitText(m.index);
      rest.splitText(m[0].length);
      span.textContent = rest.data;
      rest.replaceWith(span);
      found.add(key);
      break;
    }
  }
  return found;
}

/* ---------- hint tooltips ---------- */
const tip = document.getElementById("tip");
let current = null, shownAt = 0;
function lang() { return document.body.dataset.lang === "de" ? 1 : 0; }
function show(el) {
  const t = TERMS[el.dataset.t];
  if (!t) return;
  if (current && current !== el) current.setAttribute("aria-expanded", "false");
  if (current !== el || !tip.classList.contains("on")) shownAt = performance.now();
  current = el;
  el.setAttribute("aria-expanded", "true");
  tip.innerHTML = `<strong>${esc(t[lang() * 2])}</strong>${esc(t[lang() * 2 + 1])}`;
  tip.classList.add("on");
  const r = el.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
  let x = Math.min(Math.max(12, r.left), innerWidth - w - 12);
  let y = r.bottom + 10;
  if (y + h > innerHeight - 8) y = r.top - h - 10;
  tip.style.left = x + "px";
  tip.style.top = y + "px";
}
function hide() {
  if (current) current.setAttribute("aria-expanded", "false");
  current = null;
  tip.classList.remove("on");
}
function wire(root = document) {
  root.querySelectorAll(".t:not([tabindex])").forEach(el => {
    el.tabIndex = 0;
    el.setAttribute("role", "button");
    el.setAttribute("aria-expanded", "false");
    el.setAttribute("aria-describedby", "tip");
  });
}
document.addEventListener("mouseover", e => { const t = e.target.closest(".t"); if (t) show(t); });
document.addEventListener("mouseout", e => { const t = e.target.closest(".t"); if (t && !t.contains(e.relatedTarget) && document.activeElement !== t) hide(); });
document.addEventListener("focusin", e => { const t = e.target.closest(".t"); t ? show(t) : hide(); });
document.addEventListener("click", e => {
  const t = e.target.closest(".t");
  // a tap fires hover/focus first; only a later second tap closes the hint
  if (t) { e.preventDefault(); current === t && tip.classList.contains("on") && performance.now() - shownAt > 400 ? hide() : show(t); }
  else hide();
});
document.addEventListener("keydown", e => { if (e.key === "Escape") hide(); });
addEventListener("scroll", hide, { passive: true });

/* ---------- language ---------- */
function setLang(l) {
  document.body.dataset.lang = l;
  document.documentElement.lang = l;
  document.querySelectorAll(".lang button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.set === l)));
  hide();
  try { localStorage.setItem("lang", l); } catch (e) {}
}
document.querySelectorAll(".lang button").forEach(b => b.addEventListener("click", () => setLang(b.dataset.set)));
let initial = "en";
try { initial = localStorage.getItem("lang") || ((navigator.language || "").toLowerCase().startsWith("de") ? "de" : "en"); } catch (e) {}
setLang(initial);


const Site = { TERMS, esc, both, hints, wire, autoHint, setLang, hide };
document.querySelectorAll("[data-autohint]").forEach(autoHint);
wire(document);
