"""The story of the day: the archived chronicle, told as a narrative.

The daemon archives every entry in data_dir()/chronicle/YYYY-MM-DD.jsonl (without the
300-entry limit of the in-memory chronicle). From there a text is built with a title,
a summary of what happened, parts of the day, characters and an ending. It comes out
as Markdown and as plain text (meant to be fed to a text-to-speech reader).
"""
import datetime as dt
import getpass
import json
import re
import socket
import time
from collections import Counter
from pathlib import Path

from . import legacy, paths
from .i18n import raw, tr, words

# Which templates count for the summary
_COUNT = {
    "war": ("war.start", "war.declare"), "battle": ("battle",), "peace": ("peace",),
    "found": ("found.realm", "found.app"), "fall": ("fall",), "dyn": ("dyn.succession", "dyn.reborn"),
    "hero": ("hero.born", "power.hero"), "dead": ("hero.died", "hero.fell", "hero.exile"),
    "storm": ("storm.start",), "capital": ("capital.moved",), "visit": ("visit.arrive", "visit.local"),
    "power": ("power.lightning", "power.rain", "power.blessing", "power.forest", "power.lighthouse", "power.festival",
              "power.discord", "power.exodus", "power.wonder", "power.dragon", "power.walls", "power.truce"),
    "wonder": ("wonder.done",), "pirates": ("pirates",), "sack": ("capital.sacked",),
}


# The big events of the day (for a short story or to read aloud)
_HIGHLIGHTS = {"war.declare", "war.start", "peace", "capital.sacked", "capital.moved", "fall", "dyn.succession",
               "dyn.reborn", "found.realm", "storm.start", "eruption.start", "monument", "exp.build.done",
               "visit.arrive", "era.new", "era.kernel", "exp.siege.held", "exp.siege.breach", "wonder.done",
               "pirates", "power.dragon", "dragon.leave", "walls.breach", "walls.held", "power.truce"}


def archive_file(day: str) -> Path:
    return paths.data_dir() / "chronicle" / f"{day}.jsonl"


def archive(entries: list[dict]):
    """Appends entries to the archive of each one's day."""
    by_day: dict[str, list[dict]] = {}
    for e in entries:
        by_day.setdefault(time.strftime("%Y-%m-%d", time.localtime(e["t"])), []).append(e)
    for day, items in by_day.items():
        f = archive_file(day)
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as out:
            for e in items:
                out.write(json.dumps(e, ensure_ascii=False) + "\n")


def backfill(entries: list[dict]):
    """Fills today's archive with entries of the saved chronicle that are older
    than what is already archived (e.g. from before an update or a restart)."""
    day = time.strftime("%Y-%m-%d")
    have = load(day)
    first = min((e["t"] for e in have), default=float("inf"))
    seen = {(e["t"], e["text"]) for e in have}
    older = [e for e in entries if time.strftime("%Y-%m-%d", time.localtime(e["t"])) == day
             and e["t"] < first and (e["t"], e["text"]) not in seen]
    if not older:
        return 0
    f = archive_file(day)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in older + have), encoding="utf-8")
    return len(older)


def load(day: str) -> list[dict]:
    f = archive_file(day)
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            out.append(legacy.entry(json.loads(line)))      # days archived before 0.4: Spanish ids
        except ValueError:
            pass
    return out


def count_parts(entries: list[dict]) -> list[str]:
    """['2 wars', '14 battles', ...] from a bunch of entries."""
    keys = Counter(e.get("k", "") for e in entries)
    counts = raw("story.count")
    return [counts[name][0 if n == 1 else 1].format(n=n) for name, ks in _COUNT.items()
            if name in counts and (n := sum(keys[k] for k in ks))]


def highlights(entries: list[dict], n: int = 6) -> list[str]:
    """The big events; if there were none, the latest ones."""
    big = [e["text"] for e in entries if e.get("k") in _HIGHLIGHTS]
    return (big or [e["text"] for e in entries])[-n:]


def since(t0: float) -> list[dict]:
    """Everything archived after instant t0 (may span several days)."""
    day, today = dt.date.fromtimestamp(t0), dt.date.today()
    out = []
    while day <= today:
        out += [e for e in load(day.isoformat()) if e.get("t", 0) > t0]
        day += dt.timedelta(days=1)
    return out


_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|(?<![\w:])(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{1,4}(?![\w:])", re.I)
_COMMON_HOSTS = {"localhost", "fedora", "ubuntu", "debian", "arch", "archlinux", "pop-os", "linux", "nixos"}


def scrub(text: str) -> str:
    """For sharing: removes IPs, the user and the machine name (old chronicles
    stored them as they were)."""
    text = _IP.sub("…", text)
    words_ = {getpass.getuser()}
    host = socket.gethostname().split(".")[0]
    if host.lower() not in _COMMON_HOSTS:
        words_.add(host)
    for w in words_:
        if len(w) >= 3:
            text = re.sub(rf"\b{re.escape(w)}\b", "…", text, flags=re.I)
    return text


def build(day: str, entries: list[dict], kingdom: str, era: str = "", markdown: bool = True) -> str:
    date = dt.date.fromisoformat(day).strftime("%d/%m/%Y")
    if not entries:
        return tr("story.empty", date=date)
    parts = count_parts(entries)
    title = tr("story.title", kingdom=kingdom, date=date)
    out = [f"# {title}" if markdown else title, ""]
    if era:
        out += [tr("story.era", era=era), ""]
    out += [tr("story.summary", events=len(entries), parts=", ".join(parts) if parts else tr("story.none")), ""]

    big = [e["text"] for e in entries if e.get("k") in _HIGHLIGHTS][:14]
    if big:
        out += [f"## {tr('story.highlights')}" if markdown else tr("story.highlights").upper(), ""]
        out += [f"- {t}" if markdown else t for t in big] + [""]

    sections = words("story_parts")
    for k, (start, name) in enumerate(sections):
        end = sections[k + 1][0] if k + 1 < len(sections) else 24
        texts = [e["text"] for e in entries if start <= time.localtime(e["t"]).tm_hour < end]
        if not texts:
            continue
        out += [f"## {name}" if markdown else name.upper(), ""]
        for j in range(0, len(texts), 5):                 # paragraphs of five events
            out += [" ".join(texts[j:j + 5]), ""]

    heroes = [e["text"] for e in entries if e.get("k") in ("hero.born", "power.hero", "hero.deed", "hero.died", "hero.fell")]
    if heroes:
        out += [f"## {tr('story.heroes')}" if markdown else tr("story.heroes").upper(), ""]
        out += [f"- {t}" if markdown else t for t in heroes] + [""]
    out.append(tr("story.end", kingdom=kingdom))
    return "\n".join(out) + "\n"


def export(day: str | None = None, kingdom: str | None = None, era: str = "") -> tuple[Path, Path] | None:
    """Writes stories/YYYY-MM-DD.md and .txt. Returns the paths (or None if there was no chronicle)."""
    day = day or time.strftime("%Y-%m-%d")
    entries = load(day)
    if not entries:
        return None
    if kingdom is None:
        try:
            saved = json.loads(paths.save_file().read_text(encoding="utf-8"))
            kingdom = saved.get("kingdom", "")
        except (OSError, ValueError):
            kingdom = ""
    folder = paths.data_dir() / "stories"
    folder.mkdir(parents=True, exist_ok=True)
    md, txt = folder / f"{day}.md", folder / f"{day}.txt"
    md.write_text(build(day, entries, kingdom, era, markdown=True), encoding="utf-8")
    txt.write_text(build(day, entries, kingdom, era, markdown=False), encoding="utf-8")
    return md, txt
