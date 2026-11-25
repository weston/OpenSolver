import re

import resolve
import server as vsrv
from jobs_api import jobs


class Subtree:
    def __init__(self, solver, root, sub_root):
        self.solver, self.root, self.sub_root = solver, root, sub_root

    def cmd(self, command, **kwargs):
        command = re.sub(re.escape(self.root) + r"(?=[:\s]|$)", self.sub_root, command)
        result = self.solver.cmd(command, **kwargs)
        return re.sub(re.escape(self.sub_root) + r"(?=[:\s]|$)", self.root, result)


def remaining_lines(source, ctx, disabled):
    def schematic(node):
        return ":".join("?" if resolve.CARD_TOKEN.fullmatch(t) else t for t in node.split(":"))

    prefix = schematic(ctx["id"]) + ":"
    removed = {a.rsplit(":", 1)[-1] for a in disabled}
    lines = set()
    for path in source.cmd("show_all_lines").splitlines():
        path = schematic(path)
        if not path.startswith(prefix):
            continue
        tokens = path[len(prefix):].split(":")
        if tokens[0] in removed or "f" in tokens:
            continue
        invested = [ctx["pot"]["oop"], ctx["pot"]["ip"]]
        actor = int(ctx["actor"] == "IP")
        line = [invested[0]] if actor else []
        for token in tokens:
            if token == "?":
                actor = 0
                continue
            amount = int(token[1:]) if token.startswith("b") else max(invested)
            invested[actor] = amount
            actor = 1 - actor
            line.append(amount)
        lines.add(tuple(line))
    return sorted(lines)


class DrillResolve(resolve.ResolveManager):
    def start(self, node, disabled, accuracy, seconds, baseline=None):
        ctx = resolve.read_context(self.main, node, with_ranges=True)
        lines = remaining_lines(self.main, ctx, disabled)
        self.view = Subtree(self.rsolver, node, ctx["subgame_root"])
        self.disabled = disabled
        self.baseline = baseline
        self.comparison = None
        acc = sum(ctx["pot"].values()) * accuracy / 100

        def job():
            self.rsolver.cmds(["set_threads 0", "set_info_freq 0"])
            if self.baseline is None:
                full_lines = remaining_lines(self.main, ctx, [])
                self._build_and_solve(ctx, {}, acc, lines=full_lines, seconds=seconds)
                rows = [vsrv.parse_floats(row, vsrv.NUM_COMBOS) for row in
                        self.view.cmd("show_strategy " + node).splitlines() if row.strip()]
                self.baseline = {"rows": rows, **self.evaluation(ctx)}
            self._build_and_solve(ctx, {}, acc, lines=lines, seconds=seconds)
            self._finish(ctx, acc, lines)
            restricted = self.evaluation(ctx, self.result["calc_results"])
            self.comparison = {"original": self.baseline, "simplified": restricted,
                               "cost": [max(0, self.baseline["lower"] - restricted["upper"]),
                                        max(0, self.baseline["upper"] - restricted["lower"])]}

        self._launch("resolve", node, job)

    def evaluation(self, ctx, calc=None):
        result = jobs.parse_results(calc or self.rsolver.cmd("calc_results"))
        player = ctx["actor"].lower()
        opponent = "ip" if player == "oop" else "oop"
        return {"ev": result["ev_" + player],
                "lower": ctx["pot"]["dead"] - result["mes_" + opponent],
                "upper": result["mes_" + player]}
