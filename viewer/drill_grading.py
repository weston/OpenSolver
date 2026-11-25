DEFAULT_FREQUENCY_TOLERANCE = 10


def grade_frequencies(chosen, frequencies, evs, tolerance=DEFAULT_FREQUENCY_TOLERANCE):
    best = max(ev for ev in evs if ev is not None)
    mistakes = [max(0, choice - frequency) if frequency < 0.01 else 0
                for choice, frequency in zip(chosen, frequencies)]
    if sum(mistakes) > 1e-9:
        loss = sum(weight * max(0, best - ev) for weight, ev in zip(mistakes, evs) if ev is not None)
        return {"loss": loss, "grade": "mistake"}
    distance = sum(abs(choice - frequency) for choice, frequency in zip(chosen, frequencies)) / 2
    return {"loss": 0, "grade": "correct" if distance <= tolerance / 100 + 1e-9 else "frequency"}


def regrade_feedback(feedback):
    for hand in feedback["hands"]:
        hand.update(grade_frequencies(hand["chosen"], [a["frequency"] for a in hand["actions"]],
                                     [a["ev"] for a in hand["actions"]]))
        hand["loss_pct"] = 100 * hand["loss"] / hand["pot"] if hand["pot"] else 0
    if feedback.get("original"):
        regrade_feedback(feedback["original"])
