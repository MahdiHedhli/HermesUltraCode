"""The ultracode poller lane: fail-closed everywhere. A not-ready profile blocks BEFORE the
gate; a review block or any gate error blocks with review-required and no spawn; a released
pass spawns `hermes -p <profile> -m <model> chat`. Offline (fake backends + fake gate)."""

import unittest

from adapters.kanban_lane import (
    Outcome,
    build_spawn_argv,
    plan_enqueue,
    poll_once,
    process_claimed_task,
)
from core.roster import Roster
from core.task_tag import serialize_tag


def roster(board=None):
    d = {
        "orchestrator": {"profile": "orch", "lab": "anthropic"},
        "reviewer": {"profile": "xai", "lab": "xai"},
        "reviewer_mode": "cross_lab",
        "providers": [
            {"profile": "claude", "provider": "a", "lab": "anthropic"},
            {"profile": "xai", "provider": "x", "lab": "xai"},
        ],
        "routing": {"standard": ["claude", "xai"], "elevated": ["claude"]},
    }
    if board:
        d["board"] = board
    return Roster.from_dict(d)


class FakeResult:
    def __init__(self, released, decision="dispatched", added_directives=()):
        self.released = released
        self.decision = decision
        self.added_directives = added_directives
        self.dispatched_prompt = "Build the CSV exporter.\n\n[directives]" if released else None
        self.verdict = "pass" if released else "block"


class FakeGate:
    def __init__(self, result=None, raises=None):
        self.result = result
        self.raises = raises
        self.calls = 0

    def review_and_dispatch(self, goal, meta):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.result


class FakeKanban:
    def __init__(self, ready=()):
        self._ready = list(ready)
        self.comments, self.blocks, self.spawns, self.claims = [], [], [], []

    def list_ready(self):
        return self._ready

    def claim(self, tid, claimer):
        self.claims.append((tid, claimer))
        return True

    def add_comment(self, tid, author, body):
        self.comments.append((tid, author, body))

    def block(self, tid, reason):
        self.blocks.append((tid, reason))
        return True

    def spawn(self, profile, model, prompt, workspace):
        self.spawns.append((profile, model, prompt, workspace))
        return 4242


class PB:
    def __init__(self, auth=(), served=None):
        self._auth = set(auth)
        self._served = dict(served or {})

    def profile_exists(self, n):
        return True

    def auth_ok(self, n):
        return n in self._auth

    def served_models(self, n):
        return self._served.get(n)


class FakeTask:
    def __init__(self, tid="t1", goal="Build the CSV exporter.", comments="",
                 model_override=None, workspace=None):
        self.id = tid
        self.goal = goal
        self.comments = comments
        self.model_override = model_override
        self.workspace = workspace


MODEL = "anthropic/claude-sonnet"
TAG = serialize_tag("claude", "standard", MODEL)


class SpawnArgvTest(unittest.TestCase):
    def test_model_flag_present_and_absent(self):
        self.assertEqual(build_spawn_argv("claude", MODEL, "p"),
                         ["hermes", "-p", "claude", "-m", MODEL, "chat", "-q", "p"])
        self.assertNotIn("-m", build_spawn_argv("claude", None, "p"))


class ProcessTest(unittest.TestCase):
    def _pb_ready(self):
        return PB(auth={"claude"}, served={"claude": [MODEL, "other"]})

    def test_pass_spawns_with_model(self):
        k, g = FakeKanban(), FakeGate(FakeResult(True, added_directives=("Confine to exporter.",)))
        out = process_claimed_task(FakeTask(comments=TAG), roster(), g, k, self._pb_ready())
        self.assertEqual(out.action, "spawned")
        self.assertEqual(k.spawns[0][0], "claude")
        self.assertEqual(k.spawns[0][1], MODEL)        # gate pass -> argv carries the model
        self.assertEqual(k.blocks, [])
        self.assertTrue(k.comments)

    def test_block_is_review_required_no_spawn(self):
        k, g = FakeKanban(), FakeGate(FakeResult(False, decision="blocked"))
        out = process_claimed_task(FakeTask(comments=TAG), roster(), g, k, self._pb_ready())
        self.assertEqual(out.action, "blocked")
        self.assertEqual(k.spawns, [])
        self.assertTrue(k.blocks and k.blocks[0][1].startswith("review-required:"))

    def test_gate_error_fails_closed(self):
        k, g = FakeKanban(), FakeGate(raises=TimeoutError("reviewer timeout"))
        out = process_claimed_task(FakeTask(comments=TAG), roster(), g, k, self._pb_ready())
        self.assertEqual(out.action, "blocked")
        self.assertEqual(k.spawns, [])
        self.assertTrue(k.blocks[0][1].startswith("review-required:"))

    def test_unready_profile_blocks_before_gate(self):
        # claude authed but does NOT serve the model; xai unauth -> exhausted -> block, gate never runs
        pb = PB(auth={"claude"}, served={"claude": ["something-else"]})
        k, g = FakeKanban(), FakeGate(FakeResult(True))
        out = process_claimed_task(FakeTask(comments=TAG), roster(), g, k, pb)
        self.assertEqual(out.action, "blocked")
        self.assertEqual(g.calls, 0)                   # fail closed BEFORE any review
        self.assertEqual(k.spawns, [])

    def test_no_tag_blocks(self):
        k, g = FakeKanban(), FakeGate(FakeResult(True))
        out = process_claimed_task(FakeTask(comments="just prose"), roster(), g, k, self._pb_ready())
        self.assertEqual(out.action, "blocked")
        self.assertEqual(g.calls, 0)
        self.assertIn("no route tag", out.reason)

    def test_model_cascade_rescues_within_provider(self):
        # claude's cheap slot isn't served but its heavy slot IS: the rung cascade steps to
        # heavy on the SAME profile instead of spilling to the next provider.
        r = Roster.from_dict({
            "orchestrator": {"profile": "orch", "lab": "anthropic"},
            "reviewer": None, "reviewer_mode": "off",
            "providers": [
                {"profile": "claude", "provider": "a", "lab": "anthropic",
                 "tiers": {"cheap": "sonnet", "heavy": "opus"}},
                {"profile": "xai", "provider": "x", "lab": "xai"},
            ],
            "routing": {"standard": ["claude", "xai"]}})
        pb = PB(auth={"claude"}, served={"claude": ["opus"]})       # only heavy served
        k, g = FakeKanban(), FakeGate(FakeResult(True))
        out = process_claimed_task(
            FakeTask(comments=serialize_tag("claude", "standard", "sonnet")), r, g, k, pb)
        self.assertEqual(out.action, "spawned")
        self.assertEqual(k.spawns[0][0], "claude")                  # same provider
        self.assertEqual(k.spawns[0][1], "opus")                    # heavier model rung

    def test_native_model_override_wins(self):
        pb = PB(auth={"claude"}, served={"claude": ["native/model"]})
        k, g = FakeKanban(), FakeGate(FakeResult(True))
        out = process_claimed_task(
            FakeTask(comments=serialize_tag("claude", "standard", "tag-model"),
                     model_override="native/model"),
            roster(), g, k, pb)
        self.assertEqual(out.action, "spawned")
        self.assertEqual(k.spawns[0][1], "native/model")   # native task.model overrides the tag


class EnqueueTest(unittest.TestCase):
    def test_ok_when_a_ready_profile_serves(self):
        d = plan_enqueue("standard", roster(), PB(auth={"claude"}, served={"claude": ["x"]}))
        self.assertTrue(d.ok)
        self.assertEqual(d.profile, "claude")
        self.assertTrue(d.tag)

    def test_block_when_unroutable(self):
        self.assertFalse(plan_enqueue("standard", roster(), PB()).ok)     # nothing authed

    def test_block_when_no_candidate_for_tier(self):
        pb = PB(auth={"claude"}, served={"claude": ["x"]})
        self.assertFalse(plan_enqueue("merge_adjacent", roster(), pb).ok)  # no routing list


class PollTest(unittest.TestCase):
    def test_poll_claims_then_processes(self):
        k = FakeKanban(ready=[FakeTask(tid="t9", comments=TAG)])
        g = FakeGate(FakeResult(True))
        outs = poll_once(roster(), g, k, PB(auth={"claude"}, served={"claude": [MODEL]}))
        self.assertEqual(k.claims, [("t9", "ultracode")])
        self.assertEqual(outs[0].action, "spawned")


class BTask:
    def __init__(self, tid, comments="", status="ready", assignee="ultracode"):
        self.id = tid
        self.comments = comments
        self.status = status
        self.assignee = assignee
        self.goal = "board task"
        self.model_override = None
        self.workspace = None


class BoardKanban:
    """Board-aware fake: a shared world {board: [tasks]}; this backend sees ONLY its board."""

    def __init__(self, board, world):
        self.board = board
        self._world = world
        self.claims, self.creates, self.ensured = [], [], 0

    def ensure_board(self):
        self.ensured += 1
        self._world.setdefault(self.board, [])

    def list_ready(self):
        return [t for t in self._world.get(self.board, [])
                if t.status == "ready" and t.assignee == "ultracode"]

    def claim(self, tid, claimer):
        self.claims.append((self.board, tid))
        return True

    def create(self, goal, tag):
        tid = f"{self.board}-{len(self._world.get(self.board, []))}"
        self._world.setdefault(self.board, []).append(BTask(tid, comments=tag))
        self.creates.append((self.board, goal))
        return tid

    def open_assignees(self):
        return {t.assignee for t in self._world.get(self.board, [])}

    def add_comment(self, *a, **k):
        pass

    def block(self, tid, reason):
        return True

    def spawn(self, *a, **k):
        return 1


import os


class BoardScopingTest(unittest.TestCase):
    def test_backend_carries_config_board_not_a_literal(self):
        from adapters.kanban_lane import HermesKanbanBackend
        self.assertEqual(HermesKanbanBackend(roster(board="uc-prod").board).board, "uc-prod")

    def test_create_lands_on_the_lane_board_only(self):
        world = {}
        from adapters.kanban_lane import create_ultracode_task
        r = roster()   # default board = ultracode
        k = BoardKanban(r.board, world)
        tid, dec = create_ultracode_task("do it", "standard", r, k,
                                         PB(auth={"claude"}, served={"claude": ["x"]}))
        self.assertTrue(dec.ok)
        self.assertEqual(k.creates[0][0], "ultracode")     # created on the lane board
        self.assertEqual(len(world["ultracode"]), 1)
        self.assertNotIn("default", world)                 # nothing on any other board

    def test_poller_only_claims_its_board(self):
        world = {"ultracode": [BTask("uc-1", comments=TAG)],
                 "default": [BTask("def-1", comments=TAG)]}
        k = BoardKanban("ultracode", world)
        poll_once(roster(), FakeGate(FakeResult(True)), k, PB(auth={"claude"}, served={"claude": [MODEL]}))
        self.assertEqual([c[1] for c in k.claims], ["uc-1"])   # def-1 (other board) is invisible

    def test_ensure_board_is_idempotent(self):
        k = BoardKanban("ultracode", {})
        k.ensure_board()
        k.ensure_board()
        self.assertIn("ultracode", k._world)                   # no error, board exists

    def test_no_board_literal_in_poller_adapters(self):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        lane = open(os.path.join(base, "adapters", "kanban_lane.py"), encoding="utf-8").read()
        for bad in ('board="ultracode"', "board = 'ultracode'",
                    'create_board("ultracode")', 'connect(board="ultracode")'):
            self.assertNotIn(bad, lane)                         # board is config, never inlined
        self.assertIn("self.board", lane)                      # ops go through the scoped board attr
        roster_src = open(os.path.join(base, "core", "roster.py"), encoding="utf-8").read()
        self.assertIn('DEFAULT_BOARD = "ultracode"', roster_src)  # single source of the default


if __name__ == "__main__":
    unittest.main()
