#!/usr/bin/env python3
"""Build the website's wiki section (docs/wiki/) from the GitHub wiki.

    git clone https://github.com/LopeKinz/wolfenstein-modloader.wiki.git /tmp/wiki
    pip install markdown
    python scripts/build_site.py --wiki /tmp/wiki

Every wiki page becomes docs/wiki/<Page>.html with
  * a plain-language summary (EN + DE) from docs/wiki/summaries.json, and
  * the full technical page rendered from Markdown, with glossary hints
    added in the browser by assets/site.js.
Pages without a summary are still published; they show a short notice.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import sys

import markdown

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "https://github.com/LopeKinz/wolfenstein-modloader"
WIKI_REMOTE = REPO + "/wiki/"
RAW_REMOTE = "https://raw.githubusercontent.com/wiki/LopeKinz/wolfenstein-modloader/"

CATEGORIES = [
    ("start", "Start here", "Einstieg",
     "Guides and overviews: read these first.", "Anleitungen und Überblicke: die solltest du zuerst lesen.",
     ["Home", "Modding-Guide", "File-Formats", "Modding-Limits", "Engine-Modding-Surface", "Known-Contradictions"]),
    ("tno", "The New Order", "The New Order",
     "Part 1 (2014, id Tech 5): maps, textures and what can be changed.", "Teil 1 (2014, id Tech 5): Karten, Texturen und was sich ändern lässt.",
     ["TNO-Modding-Surface", "TNO-Maps", "TNO-Virtual-Textures", "Virtual-Texture-Planes", "UI-Assets"]),
    ("tnc", "The New Colossus", "The New Colossus",
     "Part 2 (2017, id Tech 6): settings, encryption, DLC and the developer mode.", "Teil 2 (2017, id Tech 6): Einstellungen, Verschlüsselung, DLC und der Entwicklermodus.",
     ["TNC-Modding-Surface", "TNC-Verified-Findings-2026-09-23", "TNC-CVar-Validation", "TNC-CVar-Catalog",
      "TNC-Texture-Database", "Developer-Mode", "Cfile-Encryption", "DLC-Overrides"]),
    ("systems", "Game systems", "Spielsysteme",
     "How sound, loading, save games and scripts work in both games.", "Wie Ton, Laden, Spielstände und Skripte in beiden Spielen funktionieren.",
     ["Audio", "Streamed-Audio-Containers", "Asset-Loading-Paths", "Patch-Precedence", "Save-Games-and-Profiles",
      "Kiscule-Scripting", "Ghidra-Analysis"]),
    ("maps", "Custom maps and missions", "Eigene Karten und Missionen",
     "The work towards a new playable level in Part 2.", "Die Arbeit an einem neuen spielbaren Level in Teil 2.",
     ["TNC-Custom-Map-Status", "TNC-C3V1-Custom-Map-Prototype", "TNC-DLC-C02-Runtime-Test", "Kitmap-Kit-Survey",
      "Kitmap-Map-Skeleton", "Kitmap-Design-Report", "Mission-Anatomy", "Mission-Kiscule-Scripting", "Mission-Text"]),
    ("reports", "Research reports", "Forschungsberichte",
     "Detailed results of single investigations.", "Ausführliche Ergebnisse einzelner Untersuchungen.", []),
    ("briefs", "Research briefs", "Forschungsaufträge",
     "The questions each investigation set out to answer.", "Die Fragen, die jede Untersuchung beantworten sollte.", []),
    ("status", "Status and records", "Stand und Protokolle",
     "Hand-over notes, indexes and the list of research artifacts.", "Übergabenotizen, Verzeichnisse und die Liste der Forschungsartefakte.",
     ["Reverse-Engineering-Research", "Research-Status-2026-09-21", "Research-Orchestrator-Feedback", "Research-Artifacts"]),
]

STATUS = {
    "solved": ("Solved", "Gelöst"),
    "partial": ("Partly solved", "Teilweise gelöst"),
    "open": ("Open", "Offen"),
    "reference": ("Reference", "Nachschlagewerk"),
}


def e(s: str) -> str:
    return html.escape(s, quote=True)


def both(en: str, de: str) -> str:
    return f'<span class="en">{en}</span><span class="de">{de}</span>'


def hints(text: str) -> str:
    """Escape text and turn {key|label} into glossary hint spans."""
    out = e(text)
    return re.sub(r"\{(\w+)\|([^}]+)\}", lambda m: f'<span class="t" data-t="{m.group(1)}">{m.group(2)}</span>', out)


def github_slug(value: str, separator: str = "-") -> str:
    value = re.sub(r"<[^>]+>", "", value).strip().lower()
    value = re.sub(r"[^\w\- ]", "", value, flags=re.UNICODE)
    return value.replace(" ", separator)


FENCE = re.compile(r"^\s*(```|~~~)")
LIST = re.compile(r"^(\s*)([-*+]|\d+[.)])\s")


def preprocess(md: str) -> str:
    """Bridge GitHub-flavoured habits that python-markdown reads differently."""
    out, in_fence, prev = [], False, ""
    for line in md.split("\n"):
        if FENCE.match(line):
            if not in_fence and prev.strip() and not LIST.match(prev):
                out.append("")
            in_fence = not in_fence
            out.append(line)
            prev = line
            continue
        if in_fence:
            out.append(line)
            prev = line
            continue
        m = LIST.match(line)
        if m:
            indent = m.group(1)
            if indent:  # GitHub nests with 2 spaces, python-markdown needs 4
                line = " " * (len(indent.expandtabs(4)) * 2) + line[len(indent):]
            elif prev.strip() and not LIST.match(prev) and not prev.startswith((" ", "\t", ">")):
                out.append("")
        elif line.startswith("|") and prev.strip() and not prev.startswith("|"):
            out.append("")
        out.append(line)
        prev = line
    return "\n".join(out)


def rewrite_links(body: str, pages: set) -> str:
    def fix(m):
        href = html.unescape(m.group(1))
        if re.match(r"^[a-z]+:", href) or href.startswith("#"):
            return m.group(0)
        path, _, anchor = href.partition("#")
        name = path[:-3] if path.endswith(".md") else path
        if name in pages:
            return f'href="{e(name)}.html{("#" + e(anchor)) if anchor else ""}"'
        if path.startswith(("reference/", "assets/")):
            return f'href="{e(RAW_REMOTE + path)}"'
        return f'href="{e(WIKI_REMOTE + href)}"'
    return re.sub(r'href="([^"]*)"', fix, body)


def alerts(body: str) -> str:
    return re.sub(r"<blockquote>\s*<p>\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*",
                  lambda m: f'<blockquote class="alert {m.group(1).lower()}"><p>', body)


USER_PATH = re.compile(r"(Users[\\/]+)(?!\.\.\.)([A-Za-z0-9_.-]+)")
USER_NAMES: set = set()  # filled from every page before rendering
ACCOUNT_IDS: set = set()


def find_user_names(texts) -> None:
    for t in texts:
        USER_NAMES.update(m.group(2) for m in USER_PATH.finditer(t) if m.group(2).lower() not in ("public", "default"))
        ACCOUNT_IDS.update(re.findall(r"userdata[\\/]+(\d+)", t))


def redact(text: str) -> str:
    """Remove personal data that leaked into research notes (local user names, machine names, Steam IDs)."""
    text = USER_PATH.sub(r"\1‹user›", text)
    for name in USER_NAMES:
        text = re.sub(r"\b" + re.escape(name) + r"(?=\b|[A-Z])[A-Za-z0-9]*", "‹user›", text, flags=re.I)
    text = re.sub(r"\b7656119\d{10}\b", "‹steam-id›", text)
    text = re.sub(r"(userdata[\\/]+)\d+", r"\1‹account-id›", text)
    text = re.sub(r"\bSTEAM_\d:\d:\d+\b", "‹steam-id›", text)
    text = re.sub(r"\[U:1:\d+\]", "‹account-id›", text)
    for acc in ACCOUNT_IDS:
        text = re.sub(r"\b" + acc + r"\b", "‹account-id›", text)
    return text


def render(md_text: str, pages: set):
    md_text = redact(md_text)
    lines = md_text.strip("\n").split("\n")
    title = ""
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        lines = lines[1:]
    conv = markdown.Markdown(
        extensions=["tables", "fenced_code", "sane_lists", "toc"],
        extension_configs={"toc": {"slugify": github_slug, "toc_depth": "2-3"}},
    )
    body = conv.convert(preprocess("\n".join(lines)))
    body = rewrite_links(body, pages)
    body = alerts(body)
    body = re.sub(r"<table>", '<div class="table-wrap"><table>', body)
    body = body.replace("</table>", "</table></div>")
    toc = [(t["id"], t["name"]) for t in conv.toc_tokens if t["level"] == 2]
    if not toc:
        toc = [(c["id"], c["name"]) for t in conv.toc_tokens for c in t.get("children", []) if c["level"] == 2]
    return title, body, toc


def inline_md(text: str) -> str:
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", e(text))


def page_title(name: str, title: str) -> str:
    if not title or title.lower() in ("identity", "identität"):
        for prefix, label in (("Research-Brief-", "Research brief: "), ("Research-Report-", "Research report: ")):
            if name.startswith(prefix):
                return label + name[len(prefix):].replace("-", " ")
        return name.replace("-", " ")
    return title


HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} – Wolfenstein Modloader</title>
<meta name="description" content="{desc}">
<meta name="theme-color" content="#2d3134">
<link rel="icon" href="../assets/favicon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Big+Shoulders+Stencil+Display:wght@600;800;900&family=Spectral:ital,wght@0,400;0,600;1,400&display=swap" rel="stylesheet">
<link rel="stylesheet" href="../assets/site.css">
</head>
<body data-lang="en">
<a class="skip" href="#main"><span class="en">Skip to content</span><span class="de">Zum Inhalt springen</span></a>
<header class="mast">
  <div class="wrap">
    <a class="mark" href="../index.html">Wolfenstein Modloader</a>
    <nav aria-label="Sections">
      <a href="../index.html#roadmap">Roadmap</a>
      <a href="index.html">Wiki</a>
      <a href="glossary.html"><span class="en">Words explained</span><span class="de">Begriffe</span></a>
      <a href="{repo}">GitHub</a>
    </nav>
    <div class="lang" role="group" aria-label="Language">
      <button type="button" data-set="en" aria-pressed="true">EN</button>
      <button type="button" data-set="de" aria-pressed="false">DE</button>
    </div>
  </div>
</header>
<main id="main">
"""

FOOT = """</main>
<footer>
  <div class="wrap">
    <p class="en">An independent community project, not affiliated with or endorsed by MachineGames, Bethesda Softworks, ZeniMax Media or Microsoft.</p>
    <p class="de">Ein unabhängiges Community-Projekt ohne Verbindung zu MachineGames, Bethesda Softworks, ZeniMax Media oder Microsoft.</p>
    <p><a href="{repo}/wiki">{repo_short}/wiki</a></p>
  </div>
</footer>
<div id="tip" role="tooltip"></div>
<script src="../assets/site.js"></script>
{extra}
</body>
</html>
"""


def head(title: str, desc: str) -> str:
    return HEAD.format(title=e(title), desc=e(desc), repo=REPO)


def foot(extra: str = "") -> str:
    return FOOT.format(repo=REPO, repo_short=REPO.replace("https://", ""), extra=extra)


def stamp(status: str) -> str:
    if status not in STATUS:
        return ""
    en, de = STATUS[status]
    return f'<span class="stamp s-{status}">{both(en, de)}</span>'


def summary_block(s: dict | None) -> str:
    if not s:
        return ('<section class="plainbox"><h2>' + both("In plain words", "In einfachen Worten") + "</h2>"
                + "<p>" + both("A plain-language summary of this page is still being written.",
                               "Eine Zusammenfassung in einfachen Worten wird für diese Seite noch geschrieben.") + "</p></section>")
    parts = []
    for lang in ("en", "de"):
        d = s[lang]
        pts = "".join(f"<li>{hints(p)}</li>" for p in d["points"])
        parts.append(f'<div class="{lang}"><p class="lede">{hints(d["summary"])}</p><ul>{pts}</ul></div>')
    return ('<section class="plainbox"><h2>' + both("In plain words", "In einfachen Worten") + "</h2>"
            + "".join(parts) + "</section>")


def build(wiki: str, out: str, summaries_path: str) -> int:
    names = sorted(f[:-3] for f in os.listdir(wiki) if f.endswith(".md") and not f.startswith("_"))
    pages = set(names)
    summaries = {}
    if os.path.exists(summaries_path):
        with open(summaries_path, encoding="utf-8") as f:
            summaries = json.load(f)

    cat_of = {}
    for key, *_rest, members in CATEGORIES:
        for n in members:
            cat_of[n] = key
    for n in names:
        if n not in cat_of:
            cat_of[n] = "briefs" if n.startswith("Research-Brief-") else "reports"

    sources = {}
    for n in names:
        with open(os.path.join(wiki, n + ".md"), encoding="utf-8") as f:
            sources[n] = f.read()
    find_user_names(sources.values())

    rendered = {}
    for n in names:
        title, body, toc = render(sources[n], pages)
        rendered[n] = (page_title(n, title), body, toc)

    order = {}
    for key, *_rest, members in CATEGORIES:
        listed = [n for n in members if n in pages] + sorted(n for n in names if cat_of[n] == key and n not in members)
        order[key] = listed

    os.makedirs(out, exist_ok=True)
    for f in os.listdir(out):
        if f.endswith(".html"):
            os.remove(os.path.join(out, f))

    cat_info = {c[0]: c for c in CATEGORIES}
    for n in names:
        title, body, toc = rendered[n]
        s = summaries.get(n)
        cat = cat_info[cat_of[n]]
        seq = order[cat[0]]
        i = seq.index(n)
        prev_n = seq[i - 1] if i > 0 else None
        next_n = seq[i + 1] if i + 1 < len(seq) else None
        desc = s["en"]["oneLine"] if s else title
        toc_html = "".join(f'<li><a href="#{e(a)}">{inline_md(html.unescape(t))}</a></li>' for a, t in toc)
        nav = []
        if prev_n:
            nav.append(f'<a class="prev" href="{e(prev_n)}.html"><small>{both("Previous", "Zurück")}</small>{inline_md(rendered[prev_n][0])}</a>')
        if next_n:
            nav.append(f'<a class="next" href="{e(next_n)}.html"><small>{both("Next", "Weiter")}</small>{inline_md(rendered[next_n][0])}</a>')
        doc = [
            head(title, desc),
            '<div class="pagehead"><div class="wrap">',
            f'<p class="crumbs"><a href="index.html">Wiki</a> / <a href="index.html#{cat[0]}">{both(e(cat[1]), e(cat[2]))}</a></p>',
            f'<h1>{inline_md(title)}</h1>',
            f'<p class="meta">{stamp(s["status"]) if s else ""}',
            f'<a href="{e(WIKI_REMOTE + n)}">{both("Original page in the GitHub wiki", "Originalseite im GitHub-Wiki")}</a></p>',
            "</div></div>",
            '<div class="wrap docgrid">',
            '<aside class="toc">' + (f'<h2>{both("On this page", "Auf dieser Seite")}</h2><ol>{toc_html}</ol>' if toc else "") + "</aside>",
            "<div>",
            summary_block(s),
            '<section class="technical"><h2 class="techhead">' + both("Technical details", "Technische Details") + "</h2>",
            '<p class="de langnote">Der technische Teil ist auf Englisch – so, wie er im Wiki steht. Unterstrichene Begriffe haben eine Erklärung auf Deutsch.</p>',
            f'<article class="doc" data-autohint>{body}</article></section>',
            f'<nav class="pager" aria-label="Pages">{"".join(nav)}</nav>' if nav else "",
            "</div></div>",
            foot(),
        ]
        with open(os.path.join(out, n + ".html"), "w", encoding="utf-8") as f:
            f.write("\n".join(doc))

    # ---- index
    idx = [head("Wiki", "Every page of the Wolfenstein modding wiki, explained in plain words and in full technical detail."),
           '<div class="pagehead"><div class="wrap">',
           f'<h1>{both("The whole wiki, explained", "Das ganze Wiki, erklärt")}</h1>',
           '<p class="intro">' + both(
               f"All {len(names)} pages of the research wiki. Each one starts with a summary in plain words and then shows the full technical text.",
               f"Alle {len(names)} Seiten des Forschungs-Wikis. Jede beginnt mit einer Zusammenfassung in einfachen Worten und zeigt danach den vollständigen technischen Text.") + "</p>",
           '<label class="search"><span class="en">Search the wiki</span><span class="de">Wiki durchsuchen</span>'
           '<input type="search" id="q" autocomplete="off"></label>',
           "</div></div>", '<div class="wrap">']
    for key, en, de, den, dde, _m in CATEGORIES:
        items = []
        for n in order[key]:
            s = summaries.get(n)
            one = both(hints(s["en"]["oneLine"]), hints(s["de"]["oneLine"])) if s else ""
            items.append(f'<li data-s="{e((rendered[n][0] + " " + (s["en"]["oneLine"] + " " + s["de"]["oneLine"] if s else "")).lower())}">'
                         f'<a href="{e(n)}.html">{inline_md(rendered[n][0])}</a>{stamp(s["status"]) if s else ""}<p>{one}</p></li>')
        if not items:
            continue
        idx.append(f'<section class="cat" id="{key}"><div class="cathead"><h2>{both(e(en), e(de))}</h2>'
                   f'<p>{both(e(den), e(dde))} <span class="count">{len(items)}</span></p></div>'
                   f'<ul class="pages">{"".join(items)}</ul></section>')
    idx.append('<p class="nohits" hidden>' + both("No page matches.", "Keine Seite passt.") + "</p>")
    idx.append("</div>")
    idx.append(foot("""<script>
const q = document.getElementById("q");
q.addEventListener("input", () => {
  const v = q.value.trim().toLowerCase();
  let any = false;
  document.querySelectorAll(".cat").forEach(c => {
    let shown = 0;
    c.querySelectorAll("li").forEach(li => { const ok = !v || li.dataset.s.includes(v); li.hidden = !ok; if (ok) shown++; });
    c.hidden = !shown; if (shown) any = true;
  });
  document.querySelector(".nohits").hidden = any;
});
</script>"""))
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write("\n".join(idx))

    # ---- glossary
    gl = [head("Words explained", "The technical words of the Wolfenstein modding research, in plain language."),
          '<div class="pagehead"><div class="wrap">',
          f'<h1>{both("Words explained", "Begriffe erklärt")}</h1>',
          '<p class="intro">' + both("Every technical word that gets a hint on this site, in plain language.",
                                     "Jedes Fachwort, das auf dieser Seite einen Hinweis bekommt, in einfacher Sprache.") + "</p>",
          "</div></div>", '<div class="wrap"><dl class="gloss wide" id="gloss"></dl></div>',
          foot("""<script>
document.getElementById("gloss").innerHTML = Object.entries(TERMS)
  .sort((a, b) => a[1][0].localeCompare(b[1][0]))
  .map(([k, t]) => `<div id="g-${k}"><dt>${both(t[0], t[2])}</dt><dd>${both(t[1], t[3])}</dd></div>`).join("");
</script>""")]
    with open(os.path.join(out, "glossary.html"), "w", encoding="utf-8") as f:
        f.write("\n".join(gl))

    missing = [n for n in names if n not in summaries]
    print(f"built {len(names)} pages into {out}; {len(names) - len(missing)} with summaries")
    if missing:
        print("without summary:", ", ".join(missing))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--wiki", required=True, help="path to a clone of the GitHub wiki")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "wiki"))
    ap.add_argument("--summaries", default=os.path.join(ROOT, "docs", "wiki", "summaries.json"))
    args = ap.parse_args()
    if not os.path.isdir(args.wiki):
        print(f"wiki directory not found: {args.wiki}", file=sys.stderr)
        return 2
    return build(args.wiki, args.out, args.summaries)


if __name__ == "__main__":
    sys.exit(main())
