import copy
import math
import os
import random
import tempfile
import threading
import time
import uuid

import server as vsrv
from jobs_api import jobs
from drill_spots import action_info, board_plan, merge_tree, node_preview
from drill_sessions import Sessions
from drill_resolve import DrillResolve, Subtree
from drill_locks import open_locked_simulation
from drill_grading import grade_frequencies


PLAYERS = ("OOP", "IP")
DECK = [r + s for r in vsrv.RANKS for s in vsrv.SUITS]


def cards(text):
    text = "".join(text.split())
    return [text[i:i + 2] for i in range(0, len(text), 2)]


def pick(weights, rng):
    total = sum(weights)
    if not math.isfinite(total) or total <= 0:
        raise vsrv.SolverError("no compatible hands or actions at this node")
    mark = rng.random() * total
    for i, weight in enumerate(weights):
        mark -= weight
        if weight > 0 and mark < 0:
            return i
    return max(i for i, w in enumerate(weights) if w > 0)


def hand_weights(order, own, other, board):
    blocked = set(board)
    combos = [set(cards(c)) for c in order]
    other = [w if w and not combo & blocked else 0 for combo, w in zip(combos, other)]
    total = sum(other)
    by_card = {c: 0.0 for c in DECK}
    for combo, weight in zip(combos, other):
        for c in combo:
            by_card[c] += weight
    return [w * max(0, total - sum(by_card[c] for c in combo) + other[i]) if w and not combo & blocked else 0
            for i, (combo, w) in enumerate(zip(combos, own))]


def deal_hands(order, ranges, board, rng):
    oop = pick(hand_weights(order, ranges[0], ranges[1], board), rng)
    blocked = set(board) | set(cards(order[oop]))
    ip = pick([w if w and not set(cards(combo)) & blocked else 0 for combo, w in zip(order, ranges[1])], rng)
    return [oop, ip]


def practice_hands(solver, node, order, hero, count, rng, weights):
    if count == 1:
        return [hero]
    board = set(cards(node["board"]))
    properties = [(vsrv.combo_class(c),) for c in order]
    if len(board) >= 3:
        rows = solver.cmd("show_categories " + "".join(cards(node["board"]))).splitlines()
        properties = list(zip(*(row.split() for row in rows[:2])))
        if len(properties) != vsrv.NUM_COMBOS:
            raise vsrv.SolverError("invalid hand categories")
    selected = [hero]
    while len(selected) < count:
        classes = {vsrv.combo_class(order[i]) for i in selected}
        used_properties = {properties[i] for i in selected}
        candidates = [i for i, w in enumerate(weights) if w > 0 and i not in selected]
        if not candidates:
            break
        new_classes = [i for i in candidates if vsrv.combo_class(order[i]) not in classes]
        varied = [i for i in new_classes if properties[i] not in used_properties]
        candidates = varied or new_classes or candidates
        selected.append(candidates[pick([weights[i] for i in candidates], rng)])
    rng.shuffle(selected)
    return selected


def choose_continuation(exercises, weights, rng):
    exercise = pick(weights, rng)
    return exercise, pick(exercises[exercise]["chosen"], rng)


def weighted_frequencies(strategies, weights):
    total = sum(weights)
    if total <= 0:
        raise vsrv.SolverError("no compatible hands at this node")
    result = [0.0] * len(strategies)
    for frequencies, weight in zip(zip(*strategies), weights):
        if weight <= 0:
            continue
        mass = sum(frequencies)
        if mass <= 0:
            raise vsrv.SolverError("no strategy for an in-range hand")
        for i, frequency in enumerate(frequencies):
            result[i] += frequency / mass * weight / total
    return result


class Hand:
    def __init__(self, solver, node_id, seat, mode, rng, count=1, previous_combo=None):
        self.solver, self.seat, self.mode, self.rng = solver, seat, mode, rng
        self.reference = solver
        self.count = count
        self.order = vsrv.hand_order(solver)
        self.node = self.read_node(node_id)
        root = self.read_node("r:0")
        self.big_blind = max(root["pot"][:2]) if not root["board"] else 100
        self.effective_stack = float(solver.cmd("show_effective_stack").strip())
        if self.node["type"] == "END_NODE":
            raise vsrv.SolverError("the hand has ended at this node")
        ranges = [vsrv.parse_floats(solver.cmd(f"show_range {p} {node_id}"), vsrv.NUM_COMBOS) for p in PLAYERS]
        if previous_combo is not None:
            hero = PLAYERS.index(seat)
            weights = hand_weights(self.order, ranges[hero], ranges[1 - hero], cards(self.node["board"]))
            if any(w > 0 for i, w in enumerate(weights) if i != previous_combo):
                ranges[hero][previous_combo] = 0
        self.hands = deal_hands(self.order, ranges, cards(self.node["board"]), rng)
        self.blocked = set(cards(self.order[self.hands[0]]) + cards(self.order[self.hands[1]]))
        self.history, self.street_start = self.read_line()
        self.feedback = None
        self.phase = "playing"
        self.result = None
        self.children = []
        self.actions = []
        self.frequencies = []
        self.exercises = []
        self.action_evs = None
        self.flops = {}
        if not self.node["board"]:
            solver.cmd("recover_subset")
            for row in solver.cmd("show_subset").splitlines():
                parts = row.split()
                if len(parts) == 4:
                    self.flops[frozenset(parts[1:])] = float(parts[0])
        self.advance()

    def read_node(self, node_id):
        return vsrv.parse_node_block(self.solver.cmd("show_node " + node_id))

    def read_line(self):
        history, street_start = [], [0, 0]
        parts = self.node["id"].split(":")
        if len(parts) == 2:
            return history, street_start
        parent = self.read_node("r:0")
        for end in range(3, len(parts) + 1):
            child = self.node if end == len(parts) else self.read_node(":".join(parts[:end]))
            if parent["type"] == "SPLIT_NODE":
                street_start = child["pot"][:2]
                history.append({"board": cards(child["board"]), "node": child["id"]})
            else:
                actor = "OOP" if parent["type"] == "OOP_DEC" else "IP"
                history.append({"actor": actor, "street": len(cards(parent["board"])), **action_info(parent, child, self.effective_stack), "node": parent["id"]})
            parent = child
        return history, street_start

    def strategy(self, actor):
        rows = [vsrv.parse_floats(row, vsrv.NUM_COMBOS)
                for row in self.solver.cmd("show_strategy " + self.node["id"]).splitlines() if row.strip()]
        if len(rows) != len(self.children):
            raise vsrv.SolverError("strategy does not match this node's actions")
        self.strategy_rows = rows
        weights = [row[self.hands[PLAYERS.index(actor)]] for row in rows]
        if any(w is None or w < 0 for w in weights) or sum(weights) <= 0:
            raise vsrv.SolverError("no strategy for the dealt hand")
        return [w / sum(weights) for w in weights]

    def advance(self):
        self.feedback = None
        self.original = None
        self.unrestricted = None
        self.disabled = []
        for _ in range(128):
            if self.node["type"] == "END_NODE":
                self.finish()
                return
            self.children = vsrv.parse_children(self.solver.cmd("show_children " + self.node["id"]))
            if self.node["type"] == "SPLIT_NODE":
                weights = []
                for child in self.children:
                    board = cards(child["board"])
                    weight = self.flops.get(frozenset(board), 1) if not self.node["board"] else 1
                    weights.append(0 if set(board) & self.blocked else weight)
                child = self.children[pick(weights, self.rng)]
                self.street_start = child["pot"][:2]
                self.history.append({"board": cards(child["board"]), "node": child["id"]})
                self.node = child
                continue
            actor = "OOP" if self.node["type"] == "OOP_DEC" else "IP"
            self.frequencies = self.strategy(actor)
            self.actions = [action_info(self.node, c, self.effective_stack) for c in self.children]
            if actor == self.seat and len(self.children) > 1:
                player = PLAYERS.index(self.seat)
                hero = self.hands[player]
                self.decision_ranges = [vsrv.parse_floats(self.solver.cmd(f"show_range {p} {self.node['id']}"), vsrv.NUM_COMBOS)
                                        for p in PLAYERS]
                weights = hand_weights(self.order, self.decision_ranges[player], self.decision_ranges[1 - player], cards(self.node["board"]))
                self.range_frequencies = weighted_frequencies(self.strategy_rows, weights) if sum(weights) > 0 else None
                selected = practice_hands(self.solver, self.node, self.order, hero, self.count, self.rng, weights)
                self.exercise_weights = [weights[i] for i in selected] if self.count > 1 else [1]
                self.exercises = [{"combo": self.order[i], "index": i, "chosen": None, "feedback": None} for i in selected]
                self.action_evs = None
                self.phase = "decision"
                if self.solver is not self.reference:
                    original = copy.copy(self)
                    original.solver = self.reference
                    original.strategy_rows = [vsrv.parse_floats(row, vsrv.NUM_COMBOS) for row in
                                              self.reference.cmd("show_strategy " + self.node["id"]).splitlines() if row.strip()]
                    ranges = [vsrv.parse_floats(self.reference.cmd(f"show_range {p} {self.node['id']}"), vsrv.NUM_COMBOS) for p in PLAYERS]
                    weights = hand_weights(self.order, ranges[player], ranges[1 - player], cards(self.node["board"]))
                    original.range_frequencies = weighted_frequencies(original.strategy_rows, weights) if sum(weights) else None
                    self.original = original
                return
            self.take(pick(self.frequencies, self.rng), actor)
        raise vsrv.SolverError("hand exceeds the action limit")

    def take(self, index, actor):
        self.history.append({"actor": actor, "street": len(cards(self.node["board"])), **self.actions[index], "node": self.node["id"]})
        self.node = self.children[index]

    def act(self, action, exercise=0, frequencies=None):
        if self.phase != "decision":
            raise vsrv.SolverError("there is no decision to play")
        if isinstance(exercise, bool) or not isinstance(exercise, int) or not 0 <= exercise < len(self.exercises):
            raise vsrv.SolverError("choose a hand from this node")
        item = self.exercises[exercise]
        if item["chosen"] is not None:
            raise vsrv.SolverError("this hand already has an action")
        if frequencies is None:
            index = next((i for i, a in enumerate(self.actions) if a["id"] == action), None)
            if index is None:
                raise vsrv.SolverError("choose an action from this node")
            chosen = [float(i == index) for i in range(len(self.actions))]
        else:
            if action is not None or not isinstance(frequencies, list) or len(frequencies) != len(self.actions):
                raise vsrv.SolverError("choose a frequency for each action")
            if any(isinstance(f, bool) or not isinstance(f, (int, float)) or not math.isfinite(f) or f < 0 for f in frequencies):
                raise vsrv.SolverError("action frequencies must be nonnegative numbers")
            total = sum(frequencies)
            if abs(total - 1) > 1e-6:
                raise vsrv.SolverError("action frequencies must add up to 100%")
            chosen = [f / total for f in frequencies]
        if any(f > 0 and a["id"] in self.disabled for a, f in zip(self.actions, chosen)):
            raise vsrv.SolverError("a disabled size cannot be selected")
        feedback = self.grade(item, chosen)
        item["chosen"], item["feedback"] = chosen, feedback
        if all(e["chosen"] is not None for e in self.exercises):
            selected, self.pending = choose_continuation(self.exercises, self.exercise_weights, self.rng)
            self.pending_hand = self.exercises[selected]["index"]
            self.feedback = self.summarize([e["feedback"] for e in self.exercises])
            if self.original:
                self.feedback["original"] = self.original.summarize([
                    self.original.grade(e, e["chosen"]) for e in self.exercises])
                self.feedback["removed_frequency"] = sum(
                    f for a, f in zip(self.original.actions, self.original.range_frequencies)
                    if a["id"] in self.disabled) if self.disabled else None
            self.phase = "feedback"
        return feedback

    def grade(self, item, chosen):
        if self.action_evs is None:
            self.action_evs = [vsrv.ev_pair(self.solver, self.seat, c["id"])["ev"]
                               if c["id"] not in self.disabled else [None] * vsrv.NUM_COMBOS for c in self.children]
        evs = [row[item["index"]] for row in self.action_evs]
        if any(ev is None and a["id"] not in self.disabled for ev, a in zip(evs, self.actions)):
            raise vsrv.SolverError("action EV is unavailable for this hand")
        pot = sum(self.node["pot"])
        frequencies = [row[item["index"]] for row in self.strategy_rows]
        total = sum(frequencies)
        if not total > 0:
            raise vsrv.SolverError("no strategy for this practice hand")
        frequencies = [f / total for f in frequencies]
        grade = grade_frequencies(chosen, frequencies, evs)
        return {"node": self.node["id"], "combo": item["combo"], "chosen": chosen, "pot": pot,
                    **grade, "loss_pct": 100 * grade["loss"] / pot if pot else 0,
                    "actions": [{**a, "frequency": f, "ev": ev} for a, f, ev in zip(self.actions, frequencies, evs)]}

    def summarize(self, hands):
        theoretical = list(zip(*[[a["frequency"] for a in f["actions"]] for f in hands]))
        choices = list(zip(*[h["chosen"] for h in hands]))
        for hand, weight in zip(hands, self.exercise_weights):
            hand["weight"] = weight / sum(self.exercise_weights)
        values = vsrv.ev_pair(self.solver, self.seat, self.node["id"])
        pairs = [(ev, w) for ev, w in zip(values["ev"], values["matchups"]) if ev is not None and w and w > 0]
        range_ev = sum(ev * w for ev, w in pairs) / sum(w for _, w in pairs)
        return {"hands": hands, "range_ev": range_ev, "frequencies": {
            "range": self.range_frequencies,
            "dealt_solver": weighted_frequencies(theoretical, self.exercise_weights),
            "dealt_user": weighted_frequencies(choices, self.exercise_weights)}}

    def resolved(self, solver, disabled):
        hand = copy.copy(self.unrestricted or self)
        hand.original = self.original or self
        hand.unrestricted = self.unrestricted or self
        hand.solver, hand.disabled = solver, disabled
        children = vsrv.parse_children(solver.cmd("show_children " + self.node["id"]))
        if {c["id"] for c in children} != {a["id"] for a in hand.actions} - set(disabled):
            raise vsrv.SolverError("re-solved actions do not match the selected sizes")
        rows = [vsrv.parse_floats(row, vsrv.NUM_COMBOS) for row in solver.cmd("show_strategy " + self.node["id"]).splitlines() if row.strip()]
        by_id = {c["id"]: row for c, row in zip(children, rows)}
        hand.strategy_rows = [by_id.get(a["id"], [0.0] * vsrv.NUM_COMBOS) for a in hand.actions]
        player = PLAYERS.index(hand.seat)
        weights = hand_weights(hand.order, hand.decision_ranges[player], hand.decision_ranges[1 - player], cards(hand.node["board"]))
        hand.range_frequencies = weighted_frequencies(hand.strategy_rows, weights) if sum(weights) else None
        hand.action_evs = None
        hand.exercises = [{**e, "chosen": None, "feedback": None} for e in hand.exercises]
        return hand

    def submit(self, choices):
        if not isinstance(choices, list) or len(choices) != len(self.exercises):
            raise vsrv.SolverError("choose frequencies for every hand")
        hand = copy.copy(self)
        hand.exercises = [dict(exercise) for exercise in self.exercises]
        for index, frequencies in enumerate(choices):
            hand.act(None, index, frequencies)
        return hand

    def proceed(self):
        if self.phase != "feedback":
            raise vsrv.SolverError("play a decision first")
        hand = copy.copy(self)
        hand.history = list(self.history)
        if self.count > 1:
            player = PLAYERS.index(self.seat)
            blocked = set(cards(self.node["board"]) + cards(self.order[self.pending_hand]))
            opponent = pick([w if not set(cards(combo)) & blocked else 0
                             for combo, w in zip(self.order, self.decision_ranges[1 - player])], self.rng)
            hand.hands = [self.pending_hand, opponent] if player == 0 else [opponent, self.pending_hand]
            hand.blocked = set(cards(self.order[hand.hands[0]]) + cards(self.order[hand.hands[1]]))
        hand.take(self.pending, self.seat)
        if self.mode == "node":
            hand.phase = "complete"
            hand.result = "Decision complete"
        else:
            hand.advance()
        return hand

    def finish(self):
        self.phase = "complete"
        if self.node["id"].endswith(":f"):
            folded = self.history[-1]["actor"]
            self.result = ("IP" if folded == "OOP" else "OOP") + " wins"
            return
        board = cards(self.node["board"])
        remaining = [c for c in DECK if c not in self.blocked and c not in board]
        board += self.rng.sample(remaining, 5 - len(board))
        self.node = {**self.node, "board": " ".join(board)}
        for p, index in zip(PLAYERS, self.hands):
            self.solver.cmd(f"set_range {p} " + " ".join("1" if i == index else "0" for i in range(vsrv.NUM_COMBOS)))
        self.solver.cmd("set_board " + "".join(board))
        equity = vsrv.parse_floats(self.solver.cmd("calc_eq " + self.seat).splitlines()[0], vsrv.NUM_COMBOS)[self.hands[PLAYERS.index(self.seat)]]
        self.result = "Split pot" if equity == 0.5 else (self.seat if equity == 1 else PLAYERS[1 - PLAYERS.index(self.seat)]) + " wins"

    def view(self):
        hero = PLAYERS.index(self.seat)
        wagers = [0, 0] if self.node["type"] == "END_NODE" else [a - b for a, b in zip(self.node["pot"][:2], self.street_start)]
        return {"phase": self.phase, "seat": self.seat,
                "hero": self.order[self.hands[hero]] if self.count == 1 or self.phase == "complete" else None,
                "villain": self.order[self.hands[1 - hero]] if self.phase == "complete" and self.mode == "hand" else None,
                "node": self.node, "actions": self.actions if self.phase == "decision" else [],
                "disabled": self.disabled,
                "wagers": wagers, "big_blind": self.big_blind,
                "stacks": [self.effective_stack - invested for invested in self.node["pot"][:2]],
                "continuation": {"combo": self.order[self.pending_hand], **self.actions[self.pending]}
                if self.phase == "feedback" else None,
                "exercises": [{"combo": e["combo"], "chosen": e["chosen"]} for e in self.exercises],
                "history": self.history, "feedback": self.feedback, "result": self.result}


class DrillService:
    def __init__(self, main, solver_exe, queue=None, rng=None, sessions_path=None):
        self.main, self.solver_exe, self.saved_queue = main, solver_exe, queue
        self.rng = rng or random.Random()
        self.sessions = Sessions(sessions_path or (os.path.join(queue.root, "drill-sessions.sqlite3") if queue else ":memory:"))
        self.lock = threading.RLock()
        self.prep_lock = threading.Lock()
        self.temp = None
        self.reader = None
        self.playing_solver = None
        self.resolver = None
        self.resolve_applied = False
        self.worker = None
        self.cancel = threading.Event()
        self.config = None
        self.hand = None
        self.ready = []
        self.used_solve = None
        self.preparing = False
        self.error = None
        self.progress = None
        self.convergence = None
        self.preparation = None
        self.phase_started = 0
        self.number = self.revision = self.decisions = self.completed = 0
        self.loss_pct = 0
        self.session = ""
        self.run = ""

    def token(self):
        return f"{self.session}:{self.number}:{self.revision}:{self.run}"

    def state(self):
        with self.lock, self.prep_lock:
            self.settle_resolve()
            resolution = None
            if self.resolver:
                status = self.resolver.status()
                resolution = {"busy": status["busy"], "error": status["error"], "disabled": self.resolver.disabled}
            return copy.deepcopy({"active": self.config is not None, "token": self.token(),
                                  "config": self.config and {k: self.config[k] for k in (
                                      "source", "mode", "seat", "node", "ahead", "format", "count",
                                      "boards", "selected_boards", "board_filters", "accuracy", "seconds", "merge_similar", "batch", "spot") if k in self.config},
                                  "hand": self.hand.view() if self.hand else None, "number": self.number,
                                  "preparing": self.preparing, "ready": bool(self.ready), "ready_count": len(self.ready),
                                  "can_deal": bool(self.ready or self.used_solve),
                                  "error": self.error, "progress": self.progress, "convergence": self.convergence,
                                  "resolution": resolution,
                                  "preparation": {**self.preparation, "elapsed": time.monotonic() - self.phase_started}
                                  if self.preparing and self.preparation else None,
                                  "stats": {"hands": self.completed, "decisions": self.decisions,
                                            "loss_pct": self.loss_pct / self.decisions if self.decisions else 0}})

    def start(self, body, resume=None):
        if self.config is not None:
            raise vsrv.SolverError("end the current drill first")
        source, mode, seat = body.get("source", "live"), body.get("mode", "hand"), body.get("seat", "alternate")
        if source not in ("current", "saved", "live") or mode not in ("node", "hand") or seat not in (*PLAYERS, "alternate"):
            raise vsrv.SolverError("invalid drill settings")
        node = vsrv.node_id_arg({"id": [str(body.get("node", "r:0"))]}) if mode == "node" else "r:0"
        if node == "r":
            node = "r:0"
        ahead = body.get("ahead", 3) if source == "live" else 1
        if isinstance(ahead, bool) or not isinstance(ahead, int) or not 1 <= ahead <= 20:
            raise vsrv.SolverError("solves ahead must be a whole number from 1 to 20")
        hand_format = body.get("format", "single")
        count = body.get("count", 5) if hand_format == "set" else 1
        if hand_format not in ("single", "set") or isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 20:
            raise vsrv.SolverError("hands per node must be a whole number from 1 to 20")
        config = {"source": source, "mode": mode, "seat": seat, "node": node, "ahead": ahead,
                  "format": hand_format, "count": count}
        if source == "current":
            if resume and not os.path.isfile(body.get("path", "")):
                raise vsrv.SolverError("this session's saved tree is unavailable")
            if not resume and (not self.main.tree_present or self.main.state()["solving"]):
                raise vsrv.SolverError("open a saved tree or finish the current solve first")
            if not resume and vsrv.parse_node_block(self.main.cmd("show_node " + node))["type"] == "END_NODE":
                raise vsrv.SolverError("the hand has ended at this node")
        elif source == "saved":
            if self.saved_queue is None:
                raise vsrv.SolverError("job queue is unavailable")
            config["batch"] = str(body.get("batch", ""))
            config["saves"] = [j for j in self.saved_queue.jobs(config["batch"])
                               if j["status"] == "done" and os.path.isfile(j["out"])]
            if not config["saves"]:
                raise vsrv.SolverError("this batch has no finished simulations")
        else:
            tree = body.get("tree")
            if not isinstance(tree, dict):
                raise vsrv.SolverError("choose a tree on the Tree tab first")
            config["spot"] = str(body.get("spot", ""))
            config.update(board_plan(body, tree))
            config["merge_similar"] = body.get("merge_similar", True)
            tree = merge_tree(tree, config["merge_similar"])
            config["setup"], _ = vsrv.setup_from_body({**tree, "board": config["board"]})
        for key, default, limit in (("accuracy", 0.5, 100), ("seconds", 60, 3600)):
            value = body.get(key, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= limit:
                raise vsrv.SolverError(f"invalid {key}")
            config[key] = value
        self.temp = tempfile.TemporaryDirectory(prefix="wsolver-drill-")
        self.q = jobs.Queue(os.path.join(self.temp.name, "next"))
        try:
            self.session = resume["id"] if resume else uuid.uuid4().hex
            self.run = uuid.uuid4().hex
            if source == "current":
                config["path"] = body["path"] if resume else self.sessions.snapshot_path(self.session)
                if not resume:
                    with self.main.cmd_lock:
                        if self.main.state()["solving"]:
                            raise vsrv.SolverError("finish the current solve first")
                        self.main.cmd(f'dump_tree "{config["path"]}" lossless', timeout=3600)
            self.config = config
            self.number = self.revision = self.decisions = self.completed = 0
            self.loss_pct = 0
            if resume:
                self.number = resume["number"]
                self.completed, self.decisions, self.loss_pct = resume["hands"], resume["decisions"], resume["loss"]
                self.sessions.resume(self.session)
            else:
                settings = {**body, **self.state()["config"]}
                if source == "current":
                    settings["path"] = config["path"]
                self.sessions.start(self.session, settings)
            self.cancel.clear()
            self.prepare()
        except Exception:
            self.close()
            raise

    def prepare(self, retry=False):
        with self.prep_lock:
            if self.preparing or len(self.ready) >= self.config["ahead"] or (self.error and not retry):
                return
            self.preparing, self.error, self.progress, self.preparation = True, None, None, None
        self.worker = threading.Thread(target=self._prepare, daemon=True)
        self.worker.start()

    def _prepare(self):
        try:
            while True:
                with self.prep_lock:
                    if self.cancel.is_set() or len(self.ready) >= self.config["ahead"]:
                        self.preparing = False
                        return
                    self.progress = None
                    self.preparation = None
                ready = self.prepare_one()
                with self.prep_lock:
                    if not self.cancel.is_set():
                        self.ready.append(ready)
        except Exception as e:
            with self.prep_lock:
                if not self.cancel.is_set():
                    self.error = str(e)
                self.preparing = False

    def prepare_one(self):
        config = self.config
        if config["source"] == "current":
            return {"path": config["path"]}
        if config["source"] == "saved":
            saves = config["saves"]
            chosen = saves[pick([j["weight"] for j in saves], self.rng)]
            return {"path": chosen["out"]}
        for job in self.q.jobs():
            if job["status"] == "failed":
                self.remove_job(job)
        board = self.rng.choice(config["board_pool"]) if config["board_pool"] else "".join(self.rng.sample(DECK, 3))
        job = self.q.add_batch("next-" + uuid.uuid4().hex, config["setup"], [(board, 1)], config["accuracy"], None,
                               config["seconds"], "full", None)[0]
        status = jobs.run_job(self.q, job, self.solver_exe, 0, log=lambda _: None,
                              progress=self._progress, phase=self._phase)
        job = self.q.job(job["id"])
        if status != "done":
            raise vsrv.SolverError(job.get("error") or "simulation stopped")
        return {"path": job["out"], "result": job["result"], "job": job}

    def remove_job(self, job):
        self.q.remove([job])
        if os.path.exists(job["out"]):
            os.remove(job["out"])
        directory = os.path.dirname(job["out"])
        if os.path.isdir(directory):
            os.rmdir(directory)

    def _progress(self, job, progress):
        with self.prep_lock:
            self.progress = progress
            points = self.convergence["points"]
            point = {"t": progress["t"], "explo": progress["explo"]}
            if points and points[-1]["t"] == point["t"]:
                points[-1] = point
            else:
                points.append(point)
            if len(points) > 600:
                points[:] = points[::2]

    def _phase(self, job, phase):
        with self.prep_lock:
            if phase == "building":
                self.convergence = {"job": job["id"], "board": job["board"], "points": [],
                                    "pot": jobs.start_pot(self.config["setup"]), "target": self.config["accuracy"]}
            self.preparation = {"stage": phase, "board": job["board"]}
            self.phase_started = time.monotonic()

    def next(self):
        if self.hand and self.hand.phase != "complete":
            raise vsrv.SolverError("finish this hand first")
        with self.prep_lock:
            ready = self.ready[0] if self.ready else None
        solve = ready or self.used_solve
        if solve is None:
            raise vsrv.SolverError("the next simulation is still preparing")
        if self.playing_solver:
            jobs.close_solver(self.playing_solver)
            self.playing_solver = None
        if self.reader is None:
            self.reader = vsrv.Solver(self.solver_exe)
            self.reader.cmd("set_threads 2")
        path = vsrv.safe_path(solve["path"])
        self.reader.cmd(f'load_tree "{path}" full', timeout=3600)
        seat = self.config["seat"]
        if seat == "alternate":
            seat = PLAYERS[self.number % 2]
        previous_combo = self.hand.hands[PLAYERS.index(seat)] if ready is None and self.hand and self.hand.seat == seat else None
        node = self.resolve_node(self.reader, self.config["node"])
        hand = Hand(self.reader, node, seat, self.config["mode"], self.rng,
                    self.config["count"], previous_combo)
        previous_solve = self.used_solve
        self.used_solve = solve
        self.hand = hand
        self.number += 1
        self.revision += 1
        if hand.phase == "complete":
            self.completed += 1
            self.sessions.finish_hand(self.session, self.session_stats())
        if ready is not None:
            with self.prep_lock:
                self.ready.pop(0)
            if previous_solve and "job" in previous_solve:
                self.remove_job(previous_solve["job"])
        self.prepare()

    def settle_resolve(self):
        if not self.resolver or self.resolve_applied:
            return
        status = self.resolver.status()
        if status["busy"] or status["error"] or not status["tree"]:
            return
        try:
            self.hand = self.hand.resolved(self.resolver.view, self.resolver.disabled)
        except vsrv.SolverError as e:
            with self.resolver.mu:
                self.resolver.error = str(e)
            return
        self.resolve_applied = True
        self.revision += 1

    def clear_resolve(self, keep=False):
        if self.resolver:
            self.resolver.stop()
            worker = getattr(self.resolver, "worker", None)
            if worker:
                worker.join(timeout=10)
                if worker.is_alive():
                    raise vsrv.SolverError("the re-solve is still stopping")
            if not keep:
                jobs.close_solver(self.resolver.rsolver)
            self.resolver = None
        self.resolve_applied = False

    def set_sizes(self, disabled):
        if self.hand.phase != "decision" or any(e["chosen"] is not None for e in self.hand.exercises):
            raise vsrv.SolverError("sizes can be changed before submitting a decision")
        allowed = {a["id"] for a in self.hand.actions if a["kind"] in ("bet", "raise")}
        if not isinstance(disabled, list) or any(not isinstance(a, str) or a not in allowed for a in disabled):
            raise vsrv.SolverError("choose bet or raise sizes from this node")
        base = self.hand.unrestricted or self.hand
        self.clear_resolve()
        self.hand = base
        if disabled:
            self.resolver = DrillResolve(base.solver, vsrv.Solver, self.solver_exe)
            try:
                self.resolver.start(base.node["id"], sorted(set(disabled)), self.config.get("accuracy", 0.5), self.config.get("seconds", 60))
            except Exception:
                self.clear_resolve()
                raise
        self.revision += 1

    def browse(self):
        source = self.hand.solver
        node = self.hand.node["id"]
        if self.hand.feedback:
            node = self.hand.feedback["hands"][0]["node"]
        if source is self.reader:
            vsrv.open_drill_snapshot(self.main, self.used_solve["path"])
            return node
        if isinstance(source, Subtree):
            node = source.sub_root + node[len(source.root):]
            source = source.solver
        with tempfile.TemporaryDirectory(prefix="wsolver-drill-browse-") as temp:
            path = os.path.join(temp, "spot.cfr")
            source.cmd(f'dump_tree "{path}" lossless no_compress', timeout=3600)
            vsrv.open_drill_snapshot(self.main, path)
        return node

    def close(self):
        self.clear_resolve()
        self.cancel.set()
        if self.temp:
            os.makedirs(self.q.root, exist_ok=True)
            with open(self.q.stop_file, "w") as f:
                f.write("now")
        if self.worker:
            self.worker.join(timeout=10)
            if self.worker.is_alive():
                raise vsrv.SolverError("simulation is stopping; try End session again")
        if self.reader:
            jobs.close_solver(self.reader)
        if self.playing_solver:
            jobs.close_solver(self.playing_solver)
            self.playing_solver = None
        if self.temp:
            self.temp.cleanup()
        if self.config:
            self.sessions.end(self.session)
        self.temp = self.reader = self.worker = self.hand = self.config = None
        self.ready = []
        self.used_solve = None
        self.error = self.progress = self.preparation = None
        self.convergence = None
        self.preparing = False
        self.revision += 1

    def session_stats(self):
        return self.completed, self.decisions, self.loss_pct

    def resolve_node(self, solver, pattern):
        node = "r:0"
        order = vsrv.hand_order(solver)
        for token in pattern.split(":")[2:]:
            if token == "?":
                children = vsrv.parse_children(solver.cmd("show_children " + node))
                ranges = [vsrv.parse_floats(solver.cmd(f"show_range {p} {node}"), vsrv.NUM_COMBOS) for p in PLAYERS]
                weights = [sum(hand_weights(order, *ranges, cards(c["board"]))) for c in children]
                node = children[pick(weights, self.rng)]["id"]
            else:
                node += ":" + token
        return node

    def preview(self, body):
        if body.get("source", "live") == "live":
            return node_preview(body)
        saves = [j for j in self.saved_queue.jobs(str(body.get("batch", "")))
                 if j["status"] == "done" and os.path.isfile(j["out"])] if self.saved_queue else []
        if not saves:
            raise vsrv.SolverError("this batch has no finished simulations")
        solver = vsrv.Solver(self.solver_exe)
        try:
            solver.cmd(f'load_tree "{vsrv.safe_path(saves[0]["out"])}"', timeout=3600)
            stack = float(solver.cmd("show_effective_stack").strip())
            node_id = vsrv.node_id_arg({"id": [str(body.get("node", "r:0"))]})
            parts = node_id.split(":")
            path = []
            parent = None
            for end in range(2, len(parts) + 1):
                prefix = ":".join(parts[:end])
                node = vsrv.parse_node_block(solver.cmd("show_node " + prefix))
                label = action_info(parent, node, stack)["label"] if parent and parent["type"].endswith("_DEC") else "Start" if parent is None else "Next street"
                path.append({"id": prefix, "label": label})
                parent = node
            children = vsrv.parse_children(solver.cmd("show_children " + node_id))
            chance = node["type"] == "SPLIT_NODE"
            choices = [{"id": node_id + ":?", "label": "Next street", "kind": "check", "terminal": False}] if chance else [
                {**action_info(node, {**c, "id": node_id + ":" + c["id"].rsplit(":", 1)[-1]}, stack),
                 "terminal": c["type"] == "END_NODE"} for c in children]
            return {"node": node_id, "path": path, "choices": choices, "street": {0: "Preflop", 3: "Flop", 4: "Turn", 5: "River"}[len(cards(node["board"]))],
                    "actor": node["type"].split("_")[0] if not chance else "", "decision": node["type"].endswith("_DEC")}
        finally:
            jobs.close_solver(solver)

    def api(self, method, path, body):
        if path == "/api/drill/state" and method == "GET":
            return 200, self.state()
        vsrv.require_post(method)
        body = vsrv.need_body(body)
        with self.lock:
            if path in ("/api/drill/sessions", "/api/drill/reviews"):
                offset = body.get("offset", 0)
                if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                    raise vsrv.SolverError("invalid page")
                if path.endswith("/sessions"):
                    return 200, {"sessions": self.sessions.list(offset), "active": self.session if self.config else None}
                session = str(body.get("session", ""))
                return 200, {"reviews": self.sessions.reviews(session, offset),
                             "simulations": self.sessions.simulations(session, offset)}
            if path == "/api/drill/open-locks":
                return 200, open_locked_simulation(self.main, self.sessions, self.solver_exe, body)
            if path == "/api/drill/preview":
                return 200, self.preview(body)
            if path == "/api/drill/start":
                self.start(body)
            elif path == "/api/drill/resume":
                session = self.sessions.get(str(body.get("session", "")))
                if session is None:
                    raise vsrv.SolverError("session not found")
                if self.config:
                    self.close()
                self.start(session["settings"], resume=session)
            elif path in ("/api/drill/next", "/api/drill/action", "/api/drill/submit", "/api/drill/continue", "/api/drill/stop", "/api/drill/retry", "/api/drill/sizes", "/api/drill/browse"):
                if self.config is None or body.get("token") != self.token():
                    raise vsrv.SolverError("the drill has changed; refresh before playing")
                if path.endswith("/stop"):
                    self.close()
                elif path.endswith("/next"):
                    self.next()
                elif path.endswith("/retry"):
                    self.prepare(retry=True)
                elif self.hand is None:
                    raise vsrv.SolverError("deal a hand first")
                elif path.endswith("/sizes"):
                    self.set_sizes(body.get("disabled"))
                elif self.resolver and (self.resolver.status()["busy"] or not self.resolve_applied):
                    raise vsrv.SolverError("wait for the sizes to finish resolving")
                elif path.endswith("/browse"):
                    node = self.browse()
                    return 200, {**self.state(), "browse_node": node}
                elif path.endswith(("/action", "/submit")):
                    if path.endswith("/submit"):
                        self.hand = self.hand.submit(body.get("choices"))
                    else:
                        self.hand.act(body.get("action"), body.get("exercise", 0), body.get("frequencies"))
                    if self.hand.phase == "feedback":
                        results = self.hand.feedback["hands"]
                        self.decisions += len(results)
                        self.loss_pct += sum(f["loss_pct"] for f in results)
                        review = self.hand.view()
                        if self.config["source"] == "saved":
                            review["simulation"] = self.used_solve["path"]
                        self.sessions.record(self.session, self.number, review, self.session_stats())
                    self.revision += 1
                else:
                    self.hand = self.hand.proceed()
                    if self.resolver:
                        if self.playing_solver:
                            jobs.close_solver(self.playing_solver)
                        self.playing_solver = self.resolver.rsolver
                        self.clear_resolve(keep=True)
                    self.revision += 1
                    if self.hand.phase == "complete":
                        self.completed += 1
                        self.sessions.finish_hand(self.session, self.session_stats())
            else:
                return 404, {"error": "unknown drill endpoint"}
            return 200, self.state()
