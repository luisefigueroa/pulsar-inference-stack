"""Serve a model…'s pair question, the plan on a chosen pair and Running
services…' Restart, drawn by the bundled Gum at 80 and 60 columns with
NO_COLOR=1, in a scratch checkout of doubles (tests/test_serve_menu.build_root)
over a saved four-node ring. Every command the menus run is the ./pulsar
double; nothing reaches Docker, SSH, a GPU or the Hugging Face Hub.

PAIR_RENDERS_DIR, when set, also receives each screen as plain text."""
import json
import os
from pathlib import Path
import platform
import shlex
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from scripts import saved_observations as saved  # noqa: E402
from tests import test_ui_confirm  # noqa: E402
from tests.test_serve_menu import NOW, build_root, changes, root_environment, utc  # noqa: E402
from tests.test_serve_pair_question import (HOSTS, IDS, OTHER, QUESTION, card_inventory, recipe,  # noqa: E402
                                            ring_inventory, ring_topology)
from tests.test_serve_plan import SPEC, envelope, image_plan, prepare_plan  # noqa: E402
from tests.test_ui_confirm import views  # noqa: E402

DOWN, ENTER, ESC = b"\x1b[B", b"\r", b"\x1b"
WIDTHS = (80, 60)


def unhomed(**fields):
    """A two-node recipe with nothing recorded anywhere yet."""
    return recipe(2, home=None, local_state="missing", **fields)


@unittest.skipUnless(platform.system() == "Linux" and platform.machine() == "aarch64",
                     "the bundled Gum requires Linux arm64")
class PairRenders(test_ui_confirm.BundledGum):
    def checkout(self, menus, entries, documents):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        build_root(root, menus, gum=True)
        topology = ring_topology(root)
        (root / "topology.json").write_text(json.dumps(topology))
        saved.save_changes(dict(changes()[0], topology_id=topology["topology_id"]), root / "cluster-changes.json",
                           clock=lambda: utc(NOW))
        (root / "catalog.json").write_text(json.dumps({"entries": entries}))
        for key, document in documents.items():
            (root / "fixtures" / f"{key}.json").write_text(json.dumps(document))
        exports = "".join(f"export {key}={shlex.quote(value)}\n" for key, value in root_environment(root).items())
        return root, topology, exports

    def keep(self, name, screen):
        directory = os.environ.get("PAIR_RENDERS_DIR")
        if directory:
            Path(directory, name).write_text("\n".join(screen).rstrip() + "\n")

    def question(self, root, exports, columns):
        code, output = self.drive(exports + f'exec bash {shlex.quote(str(root / "scripts/serve-menu.sh"))}\n',
                                  ENTER, ready=b"Back", columns=columns,
                                  then=[(QUESTION.encode(), ESC), (b"esc back", ESC)])
        self.assertEqual(code, 0, output)
        screen = next(shot for shot in views(output, 24, columns) if QUESTION in shot)
        self.assertTrue(all(len(line) <= columns for line in screen), "\n".join(screen))
        # Nothing was planned, prepared or started.
        self.assertEqual((root / "pulsar.log").read_text().splitlines(), ["inventory --json"])
        return screen

    def test_the_question_with_nothing_prepared(self):
        root, _, exports = self.checkout(("serve-menu.sh", "cluster-review.sh"), [unhomed()],
                                         {"inventory": ring_inventory()})
        for columns in WIDTHS:
            with self.subTest(columns=columns):
                (root / "pulsar.log").unlink(missing_ok=True)
                screen = self.question(root, exports, columns)
                self.keep(f"question-nothing-prepared-{columns}.txt", screen)
                text = "\n".join(screen)
                # The cursor starts on the default pair; the order is this machine's pairs, then cable order.
                self.assertTrue(screen[screen.index(QUESTION) + 2].startswith(f"> {HOSTS[0]} and {HOSTS[1]}"), text)
                flat = " ".join(text.split())
                order = [flat.index(f"{HOSTS[a]} and {HOSTS[b]}, API on {HOSTS[a]}")
                         for a, b in ((0, 1), (0, 3), (1, 2), (2, 3))]
                self.assertEqual(order, sorted(order), text)
                self.assertIn("what Start uses when no machines are named", flat)
                self.assertIn("  Back", screen)

    def test_the_question_with_files_on_one_pair_and_another_service_on_another(self):
        root, topology, exports = self.checkout(("serve-menu.sh", "cluster-review.sh"), [], {})
        topology_id = topology["topology_id"]
        views_of = [{"rank": 0, "node_id": IDS[1], "topology_id": topology_id},
                    {"rank": 1, "node_id": IDS[2], "topology_id": topology_id}]
        (root / "catalog.json").write_text(json.dumps({"entries": [
            unhomed(prepared_copies=views_of), recipe(1, spec_id=OTHER, model_id="org/other")]}))
        (root / "fixtures/inventory.json").write_text(json.dumps(ring_inventory((OTHER, "running", [3]))))
        for columns in WIDTHS:
            with self.subTest(columns=columns):
                (root / "pulsar.log").unlink(missing_ok=True)
                screen = self.question(root, exports, columns)
                self.keep(f"question-files-and-service-{columns}.txt", screen)
                flat = " ".join(" ".join(screen).split())
                self.assertIn(f"{HOSTS[1]} and {HOSTS[2]}, API on {HOSTS[1]}; its copies are recorded here", flat)
                self.assertIn(f"org/other is running on {HOSTS[3]}", flat)

    def test_the_plan_for_the_pair_on_saved_ranks_2_and_3(self):
        copy = prepare_plan((IDS[2], "copy"), (IDS[3], "copy"))
        copy["actions"][0]["passes_through"] = [{"copies": "to", "node_id": IDS[2], "through": [IDS[1]]}]
        root, _, exports = self.checkout(("serve-menu.sh", "cluster-review.sh"), [recipe(2, local_state="missing")], {
            "inventory": ring_inventory(), "prepare-plan": envelope(copy),
            "image-plan": envelope(image_plan((2, "ok"), (3, "ok"))), "start-plan": envelope({"checks": []})})
        for columns in WIDTHS:
            with self.subTest(columns=columns):
                (root / "pulsar.log").unlink(missing_ok=True)
                code, output = self.drive(
                    exports + f'exec bash {shlex.quote(str(root / "scripts/serve-menu.sh"))}\n', ENTER, ready=b"Back",
                    columns=columns, rows=40,
                    then=[(QUESTION.encode(), DOWN * 3 + ENTER), (b"> Cancel", ESC), (b"esc back", ESC)])
                self.assertEqual(code, 0, output)
                plan = next(shot for shot in views(output, 40, columns) if "> Cancel" in shot)
                self.keep(f"plan-ranks-2-and-3-{columns}.txt", plan)
                flat = " ".join(" ".join(plan).split())
                self.assertIn(f"Serve org/model on {HOSTS[2]} and {HOSTS[3]}", flat)
                self.assertIn(f"Copies to {HOSTS[2]} pass through {HOSTS[1]}.", flat)
                self.assertTrue(all(len(line) <= columns for line in plan))
                placement = f"--placement-nodes {HOSTS[2]},{HOSTS[3]}"
                self.assertEqual((root / "pulsar.log").read_text().splitlines(), [
                    "inventory --json", f"model prepare {SPEC} {placement} --plan --json",
                    f"image stage {SPEC} {placement} --plan --json", f"start {SPEC} {placement} --dry-run --json"])

    def test_running_services_card_with_restart_and_the_command_it_leads_to(self):
        menus = ("services-menu.sh", "serve-menu.sh", "cluster-review.sh", "connect_card.py")
        root, _, exports = self.checkout(menus, [recipe(2)], {"inventory": card_inventory((SPEC, [2, 3]))})
        (root / "fixtures/stop.inventory").write_text(json.dumps(card_inventory()))
        script = exports + f'exec bash {shlex.quote(str(root / "scripts/services-menu.sh"))}\n'
        for columns in WIDTHS:
            with self.subTest(columns=columns):
                for name in ("pulsar.log", "fixtures/inventory.count", "fixtures/stop.count",
                             "fixtures/start.count"):
                    (root / name).unlink(missing_ok=True)
                (root / "fixtures/inventory.json").write_text(json.dumps(card_inventory((SPEC, [2, 3]))))
                # The card, Restart…, its plan, Go ahead, its outcome, then Esc out.
                code, output = self.drive(script, DOWN * 3, ready=b"Stop every service", columns=columns, rows=40,
                                          then=[(b"> Restart", ENTER), (b"> Cancel", DOWN + ENTER),
                                                (b"Same as:", b""), (b"> Back", ESC), (b"Running services", ESC)])
                self.assertEqual(code, 0, output)
                shots = views(output, 40, columns)
                card = next(shot for shot in shots if any(line.startswith("> Restart") for line in shot))
                plan = next(shot for shot in shots if "> Cancel" in shot)
                outcome = next(shot for shot in reversed(shots) if any("Same as:" in line for line in shot))
                self.keep(f"card-restart-{columns}.txt", card)
                self.keep(f"restart-plan-{columns}.txt", plan)
                self.keep(f"restart-outcome-{columns}.txt", outcome)
                self.assertTrue(any(line.startswith(f"org/model running on {HOSTS[2]}") for line in card),
                                "\n".join(card))
                self.assertIn(f"Restart org/model on {HOSTS[2]} and {HOSTS[3]}", " ".join(" ".join(plan).split()))
                self.assertIn(f"./pulsar start abababababab --placement-nodes {HOSTS[2]},{HOSTS[3]}",
                              " ".join(" ".join(outcome).split()))
                for screen in (card, plan, outcome):
                    self.assertTrue(all(len(line) <= columns for line in screen))
                self.assertEqual([line for line in (root / "pulsar.log").read_text().splitlines()
                                  if "--json" not in line],
                                 [f"stop {SPEC}", f"start {SPEC} --placement-nodes {HOSTS[2]},{HOSTS[3]}"])


if __name__ == "__main__":
    unittest.main()
