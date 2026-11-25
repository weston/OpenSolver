import os
import tempfile

import server as vsrv
from jobs_api import jobs


def open_locked_simulation(main, sessions, solver_exe, body):
    session = str(body.get("session", ""))
    simulation = str(body.get("simulation", ""))
    seat = body.get("seat")
    if seat not in ("OOP", "IP"):
        raise vsrv.SolverError("choose OOP or IP locks")
    choices = sessions.choices(session, simulation, seat)
    if not choices:
        raise vsrv.SolverError("no drilled actions for this position and simulation")
    temp = tempfile.TemporaryDirectory(prefix="wsolver-drill-locks-")
    path = os.path.join(temp.name, "locked.cfr")
    try:
        solver = vsrv.Solver(solver_exe)
        try:
            solver.cmd(f'load_tree "{vsrv.safe_path(simulation)}" full', timeout=3600)
            solver.tree_present = True
            order = {combo: i for i, combo in enumerate(vsrv.hand_order(solver))}
            nodes = {}
            for choice in choices:
                nodes.setdefault(choice["node"], []).append(choice)
            for node, hands in nodes.items():
                current, children = vsrv._decision(solver, node)
                if current["type"] != seat + "_DEC":
                    raise vsrv.SolverError("the saved simulation's position has changed")
                rows = [vsrv.parse_floats(row, vsrv.NUM_COMBOS) for row in solver.cmd("show_strategy " + node).splitlines() if row.strip()]
                mask = [0] * vsrv.NUM_COMBOS
                for hand in hands:
                    if set(hand["mix"]) != {c["id"] for c in children}:
                        raise vsrv.SolverError("the saved simulation's actions have changed")
                    index = order[hand["combo"]]
                    mask[index] = 1
                    for row, child in zip(rows, children):
                        row[index] = hand["mix"][child["id"]]
                vsrv.op_lock(solver, {"id": node, "mode": "combos", "freq": rows, "combos": mask})
            solver.cmd(f'dump_tree "{path}" lossless', timeout=3600)
            locks = dict(solver.locks)
        finally:
            jobs.close_solver(solver)
        vsrv.open_drill_variant(main, path, locks, {"session": session, "simulation": simulation, "hero": seat})
    finally:
        temp.cleanup()
    return {"ok": True, "hero": seat, "nodes": len(nodes), "combos": len(choices)}
