import copy
import math
import os
import random
import tempfile
import threading
import uuid

import server as vsrv
from jobs_api import jobs


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


def practice_hands(solver, node, seat, order, hero, count, rng):
    if count == 1:
        return [hero]
    board = set(cards(node["board"]))
    reach = vsrv.parse_floats(solver.cmd(f"show_range {seat} {node['id']}"), vsrv.NUM_COMBOS)
    opponent = "IP" if seat == "OOP" else "OOP"
    other = vsrv.parse_floats(solver.cmd(f"show_range {opponent} {node['id']}"), vsrv.NUM_COMBOS)
    weights = hand_weights(order, reach, other, board)
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


def action_info(node, child):
    token = child["id"].rsplit(":", 1)[-1]
    actor = 0 if node["type"] == "OOP_DEC" else 1
    own, facing = node["pot"][actor], max(node["pot"][:2])
    if token == "f":
        return {"id": child["id"], "kind": "fold", "label": "Fold", "amount": None}
    if token == "c":
        return {"id": child["id"], "kind": "call" if facing > own else "check",
                "label": "Call" if facing > own else "Check", "amount": facing - own or None}
    amount = float(token[1:])
    raising = facing > own
    fraction = (amount - facing) / (sum(node["pot"]) + facing - own) if raising else (amount - own) / sum(node["pot"])
    return {"id": child["id"], "kind": "raise" if raising else "bet",
            "label": ("Raise " if raising else "Bet ") + f"{100 * fraction:.0f}%",
            "amount": amount - own}


class Hand:
    def __init__(self, solver, node_id, seat, mode, rng, count=1, previous_combo=None):
        self.solver, self.seat, self.mode, self.rng = solver, seat, mode, rng
        self.count = count
        self.order = vsrv.hand_order(solver)
        self.node = self.read_node(node_id)
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
        self.history = []
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
                self.history.append({"board": cards(child["board"]), "node": child["id"]})
                self.node = child
                continue
            actor = "OOP" if self.node["type"] == "OOP_DEC" else "IP"
            self.frequencies = self.strategy(actor)
            self.actions = [action_info(self.node, c) for c in self.children]
            if actor == self.seat:
                hero = self.hands[PLAYERS.index(self.seat)]
                selected = practice_hands(self.solver, self.node, self.seat, self.order, hero, self.count, self.rng)
                self.exercises = [{"combo": self.order[i], "index": i, "chosen": None, "feedback": None} for i in selected]
                self.action_evs = None
                self.phase = "decision"
                return
            self.take(pick(self.frequencies, self.rng), actor)
        raise vsrv.SolverError("hand exceeds the action limit")

    def take(self, index, actor):
        self.history.append({"actor": actor, **self.actions[index], "node": self.node["id"]})
        self.node = self.children[index]

    def act(self, action, exercise=0):
        if self.phase != "decision":
            raise vsrv.SolverError("there is no decision to play")
        if isinstance(exercise, bool) or not isinstance(exercise, int) or not 0 <= exercise < len(self.exercises):
            raise vsrv.SolverError("choose a hand from this node")
        item = self.exercises[exercise]
        if item["chosen"] is not None:
            raise vsrv.SolverError("this hand already has an action")
        index = next((i for i, a in enumerate(self.actions) if a["id"] == action), None)
        if index is None:
            raise vsrv.SolverError("choose an action from this node")
        if self.action_evs is None:
            self.action_evs = [vsrv.ev_pair(self.solver, self.seat, c["id"])["ev"] for c in self.children]
        evs = [row[item["index"]] for row in self.action_evs]
        if any(ev is None for ev in evs):
            raise vsrv.SolverError("action EV is unavailable for this hand")
        pot = sum(self.node["pot"])
        loss = max(evs) - evs[index]
        frequencies = [row[item["index"]] for row in self.strategy_rows]
        total = sum(frequencies)
        if not total > 0:
            raise vsrv.SolverError("no strategy for this practice hand")
        feedback = {"node": self.node["id"], "combo": item["combo"], "chosen": action, "pot": pot,
                    "loss": loss, "loss_pct": 100 * loss / pot if pot else 0,
                    "actions": [{**a, "frequency": f / total, "ev": ev} for a, f, ev in zip(self.actions, frequencies, evs)]}
        item["chosen"], item["feedback"] = action, feedback
        if item["index"] == self.hands[PLAYERS.index(self.seat)]:
            self.pending = index
        if all(e["chosen"] is not None for e in self.exercises):
            self.feedback = {"hands": [e["feedback"] for e in self.exercises]}
            self.phase = "feedback"
        return feedback

    def proceed(self):
        if self.phase != "feedback":
            raise vsrv.SolverError("play a decision first")
        hand = copy.copy(self)
        hand.history = list(self.history)
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
        return {"phase": self.phase, "seat": self.seat,
                "hero": self.order[self.hands[hero]] if self.count == 1 or self.phase == "complete" else None,
                "villain": self.order[self.hands[1 - hero]] if self.phase == "complete" and self.mode == "hand" else None,
                "node": self.node, "actions": self.actions if self.phase == "decision" else [],
                "exercises": [{"combo": e["combo"], "chosen": e["chosen"]} for e in self.exercises],
                "history": self.history, "feedback": self.feedback, "result": self.result}


class DrillService:
    def __init__(self, main, solver_exe, queue=None, rng=None):
        self.main, self.solver_exe, self.saved_queue = main, solver_exe, queue
        self.rng = rng or random.Random()
        self.lock = threading.RLock()
        self.prep_lock = threading.Lock()
        self.temp = None
        self.reader = None
        self.worker = None
        self.cancel = threading.Event()
        self.config = None
        self.hand = None
        self.ready = []
        self.used_solve = None
        self.preparing = False
        self.error = None
        self.progress = None
        self.number = self.revision = self.decisions = self.completed = 0
        self.loss_pct = 0
        self.session = ""

    def token(self):
        return f"{self.session}:{self.number}:{self.revision}"

    def state(self):
        with self.lock, self.prep_lock:
            return copy.deepcopy({"active": self.config is not None, "token": self.token(),
                                  "config": self.config and {k: self.config[k] for k in (
                                      "source", "mode", "seat", "node", "ahead", "format", "count",
                                      "boards", "accuracy", "seconds", "batch", "spot") if k in self.config},
                                  "hand": self.hand.view() if self.hand else None, "number": self.number,
                                  "preparing": self.preparing, "ready": bool(self.ready), "ready_count": len(self.ready),
                                  "can_deal": bool(self.ready or self.used_solve),
                                  "error": self.error, "progress": self.progress,
                                  "stats": {"hands": self.completed, "decisions": self.decisions,
                                            "loss_pct": self.loss_pct / self.decisions if self.decisions else 0}})

    def start(self, body):
        if self.config is not None:
            raise vsrv.SolverError("end the current drill first")
        source, mode, seat = body.get("source", "current"), body.get("mode", "hand"), body.get("seat", "OOP")
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
            if not self.main.tree_present or self.main.state()["solving"]:
                raise vsrv.SolverError("open a saved tree or finish the current solve first")
            if vsrv.parse_node_block(self.main.cmd("show_node " + node))["type"] == "END_NODE":
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
                raise vsrv.SolverError("choose a tree on the Build tab first")
            config["spot"] = str(body.get("spot", ""))
            config["boards"] = body.get("boards", "random")
            if config["boards"] not in ("random", "fixed"):
                raise vsrv.SolverError("invalid board mode")
            board = str(tree.get("board", "")).replace(" ", "") if config["boards"] == "fixed" else ""
            if config["boards"] == "fixed" and (len(cards(board)) not in (3, 4, 5) or len(set(cards(board))) != len(cards(board)) or any(c not in DECK for c in cards(board))):
                raise vsrv.SolverError("choose a board on the Build tab first")
            config["board"] = board
            config["setup"], _ = vsrv.setup_from_body({**tree, "board": board})
            for key, default, limit in (("accuracy", 0.5, 100), ("seconds", 60, 3600)):
                value = body.get(key, default)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= limit:
                    raise vsrv.SolverError(f"invalid {key}")
                config[key] = value
        self.temp = tempfile.TemporaryDirectory(prefix="wsolver-drill-")
        self.q = jobs.Queue(os.path.join(self.temp.name, "next"))
        try:
            if source == "current":
                config["path"] = os.path.join(self.temp.name, "current.cfr")
                with self.main.cmd_lock:
                    if self.main.state()["solving"]:
                        raise vsrv.SolverError("finish the current solve first")
                    self.main.cmd(f'dump_tree "{config["path"]}" lossless', timeout=3600)
            self.config = config
            self.session = uuid.uuid4().hex
            self.number = self.revision = self.decisions = self.completed = 0
            self.loss_pct = 0
            self.cancel.clear()
            self.prepare()
        except Exception:
            self.close()
            raise

    def prepare(self, retry=False):
        with self.prep_lock:
            if self.preparing or len(self.ready) >= self.config["ahead"] or (self.error and not retry):
                return
            self.preparing, self.error, self.progress = True, None, None
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
        board = config["board"] or "".join(self.rng.sample(DECK, 3))
        job = self.q.add_batch("next-" + uuid.uuid4().hex, config["setup"], [(board, 1)], config["accuracy"], None,
                               config["seconds"], "full", None)[0]
        status = jobs.run_job(self.q, job, self.solver_exe, 2, log=lambda _: None, progress=self._progress)
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

    def next(self):
        if self.hand and self.hand.phase != "complete":
            raise vsrv.SolverError("finish this hand first")
        with self.prep_lock:
            ready = self.ready[0] if self.ready else None
        solve = ready or self.used_solve
        if solve is None:
            raise vsrv.SolverError("the next simulation is still preparing")
        if self.reader is None:
            self.reader = vsrv.Solver(self.solver_exe)
            self.reader.cmd("set_threads 2")
        path = vsrv.safe_path(solve["path"])
        self.reader.cmd(f'load_tree "{path}" full', timeout=3600)
        seat = self.config["seat"]
        if seat == "alternate":
            seat = PLAYERS[self.number % 2]
        previous_combo = self.hand.hands[PLAYERS.index(seat)] if ready is None and self.hand and self.hand.seat == seat else None
        hand = Hand(self.reader, self.config["node"], seat, self.config["mode"], self.rng,
                    self.config["count"], previous_combo)
        previous_solve = self.used_solve
        self.used_solve = solve
        self.hand = hand
        self.number += 1
        self.revision += 1
        if hand.phase == "complete":
            self.completed += 1
        if ready is not None:
            with self.prep_lock:
                self.ready.pop(0)
            if previous_solve and "job" in previous_solve:
                self.remove_job(previous_solve["job"])
        self.prepare()

    def close(self):
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
        if self.temp:
            self.temp.cleanup()
        self.temp = self.reader = self.worker = self.hand = self.config = None
        self.ready = []
        self.used_solve = None
        self.error = self.progress = None
        self.preparing = False
        self.revision += 1

    def api(self, method, path, body):
        if path == "/api/drill/state" and method == "GET":
            return 200, self.state()
        vsrv.require_post(method)
        body = vsrv.need_body(body)
        with self.lock:
            if path == "/api/drill/start":
                self.start(body)
            elif path in ("/api/drill/next", "/api/drill/action", "/api/drill/continue", "/api/drill/stop", "/api/drill/retry"):
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
                elif path.endswith("/action"):
                    self.hand.act(body.get("action"), body.get("exercise", 0))
                    if self.hand.phase == "feedback":
                        results = self.hand.feedback["hands"]
                        self.decisions += len(results)
                        self.loss_pct += sum(f["loss_pct"] for f in results)
                    self.revision += 1
                else:
                    self.hand = self.hand.proceed()
                    self.revision += 1
                    if self.hand.phase == "complete":
                        self.completed += 1
            else:
                return 404, {"error": "unknown drill endpoint"}
            return 200, self.state()
