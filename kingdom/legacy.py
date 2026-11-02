"""Ids from versions before 0.4, when the internal names were Spanish.

Saved worlds, the daily story archive, user configs and commands written by an older
renderer may still use them; everything that reads those passes them through here, so an
old world keeps loading with its realms, wonders, landmarks and chronicle intact.

Strings used as seeds for generated names (`place("volcan")`, `structure_name(..., "academia")`)
are NOT ids and stay as they were: changing them would rename places on existing maps.
"""

POWERS = {"rayo": "lightning", "lluvia": "rain", "bendicion": "blessing", "heroe": "hero", "bosque": "forest",
          "faro": "lighthouse", "fiesta": "festival", "discordia": "discord", "exodo": "exodus",
          "maravilla": "wonder", "murallas": "walls", "tregua": "truce"}
WONDERS = {"coloso": "colossus", "coliseo": "colosseum", "observatorio": "observatory", "jardines": "gardens"}
LANDMARKS = {"oraculo": "oracle", "teatro": "theater", "taberna": "tavern", "atalaya": "watchtower",
             "molino": "windmill", "granero": "granary", "mercado": "market", "herreria": "smithy"}
SEASONS = {"invierno": "winter", "primavera": "spring", "verano": "summer", "otoño": "autumn"}
HEMISPHERES = {"norte": "north", "sur": "south"}
# chronicle entry "kind" (a category; nothing reads it back, but it is stored)
KINDS = {"caida": "fall", "calor": "heat", "cronica": "chronicle", "dinastia": "dynasty", "escribas": "scribes",
         "expedicion": "expedition", "fortaleza": "fortress", "fundacion": "founding", "guerra": "war",
         "heroe": "hero", "migracion": "migration", "mundo": "world", "naturaleza": "nature", "obra": "works",
         "paz": "peace", "poder": "power", "pueblo": "people", "soberano": "sovereign", "visita": "visit",
         "academia": "academy", "fe": "faith", "hito": "milestone"}
# chronicle entry "k" (which text template it came from)
TEMPLATES = {f"power.{a}": f"power.{b}" for a, b in POWERS.items()} | {"wonder.coloso.held": "wonder.colossus.held"}

IDS = POWERS | WONDERS | LANDMARKS | SEASONS


def power(name: str) -> str:
    return POWERS.get(name, name)


def entry(e: dict) -> dict:
    """A chronicle entry (world.json or the daily archive)."""
    if e.get("kind") in KINDS:
        e["kind"] = KINDS[e["kind"]]
    if e.get("k") in TEMPLATES:
        e["k"] = TEMPLATES[e["k"]]
    return e


def world(d: dict) -> dict:
    """A saved world (world.json), in place."""
    for f in d.get("factions") or []:
        if f.get("landmark") in LANDMARKS:
            f["landmark"] = LANDMARKS[f["landmark"]]
    for s in d.get("structures") or []:
        if s.get("wonder") in WONDERS:
            s["wonder"] = WONDERS[s["wonder"]]
    for e in d.get("chronicle") or []:
        entry(e)
    for p in d.get("power_log") or []:
        if p.get("power") in POWERS:
            p["power"] = POWERS[p["power"]]
    return d


def config(cfg: dict) -> dict:
    """A merged config: a user config.toml may still use the old power and landmark names."""
    powers = cfg.get("powers") or {}
    for old, new in POWERS.items():
        if old in powers:
            powers[new] = powers.pop(old)          # the user's setting wins over the default
    marks = (cfg.get("apps") or {}).get("landmarks") or {}
    for app, mark in list(marks.items()):
        marks[app] = LANDMARKS.get(mark, mark)
    render = cfg.get("render") or {}
    if render.get("hemisphere") in HEMISPHERES:
        render["hemisphere"] = HEMISPHERES[render["hemisphere"]]
    return cfg
