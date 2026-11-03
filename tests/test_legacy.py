"""Worlds, archives and configs from before 0.4 (Spanish ids) still load."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kingdom import config, legacy, story
from kingdom.daemon.world import World
from test_world import proc, snap

OLD = {v: k for k, v in legacy.IDS.items()}                  # new id -> old Spanish id
OLD_KIND = {v: k for k, v in legacy.KINDS.items()}
OLD_TPL = {v: k for k, v in legacy.TEMPLATES.items()}


def to_old(d: dict) -> dict:
    """What a 0.3 version would have saved for this world."""
    d = copy.deepcopy(d)
    for f in d["factions"]:
        if f.get("landmark"):
            f["landmark"] = OLD.get(f["landmark"], f["landmark"])
    for s in d["structures"]:
        if s.get("wonder"):
            s["wonder"] = OLD[s["wonder"]]
    for e in d["chronicle"]:
        e["kind"] = OLD_KIND.get(e["kind"], e["kind"])
        if e.get("k") in OLD_TPL:
            e["k"] = OLD_TPL[e["k"]]
    for p in d["power_log"]:
        p["power"] = OLD.get(p["power"], p["power"])
    return d


class LegacyTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()
        self.cfg["world"]["seed"] = 99

    def test_old_world_loads_the_same(self):
        w = World(self.cfg)
        for k in range(40):
            w.step(snap(t=1_000_000.0 + 2 * k, browsers={"firefox": proc(2500)},
                        apps={"cubase": proc(600), "obs": proc(400)}))
        new = w.to_dict()
        old = to_old(new)
        self.assertNotEqual(json.dumps(old), json.dumps(new))          # really in the old format
        self.assertEqual(World(self.cfg, old).to_dict(), World(self.cfg, copy.deepcopy(new)).to_dict())

    def test_old_power_command(self):
        w = World(self.cfg)
        w.resources.update(mana=999)
        w.step(snap(), [{"power": "lluvia", "x": 1, "y": 1}])
        self.assertEqual(w.power_log[-1]["power"], "rain")

    def test_old_user_config(self):
        with tempfile.TemporaryDirectory() as d:
            user = Path(d) / "config.toml"
            user.write_text('[powers]\nrayo = { mana = 1 }\n[apps.landmarks]\nmyapp = "molino"\n'
                            '[render]\nhemisphere = "sur"\n', encoding="utf-8")
            with mock.patch("kingdom.paths.user_config", return_value=user):
                cfg = config.load()
        self.assertEqual(cfg["powers"]["lightning"], {"mana": 1})          # the user's value wins
        self.assertNotIn("rayo", cfg["powers"])
        self.assertEqual(cfg["apps"]["landmarks"]["myapp"], "windmill")
        self.assertEqual(cfg["render"]["hemisphere"], "south")

    def test_old_story_archive(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "2026-01-01.jsonl"
            f.write_text(json.dumps({"t": 1, "kind": "poder", "k": "power.rayo", "text": "x"}) + "\n", encoding="utf-8")
            with mock.patch.object(story, "archive_file", return_value=f):
                e = story.load("2026-01-01")[0]
        self.assertEqual((e["kind"], e["k"]), ("power", "power.lightning"))


if __name__ == "__main__":
    unittest.main()
