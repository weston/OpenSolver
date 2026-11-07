#!/usr/bin/env python3
"""Keep docs/commands.md in step with the UPI commands in src/upi*.cpp (#404).

The doc promises a row for every registered command, each linking to where it is registered.
Nothing kept that true: five commands had no row, and every line link had drifted.

    python3 tools/check_commands_doc.py          # report problems; exit 1 if any
    python3 tools/check_commands_doc.py --fix    # rewrite the links to the registering lines

--fix only rewrites links; a missing row has to be written by hand. tests/test_packaging.py fails
on a missing or extra row or a wrong count, and only reports stale links.
"""
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(ROOT, "docs", "commands.md")
ROW = re.compile(r"^\| \[`([A-Za-z_0-9:]+)`\]\(\.\./(src/[a-z_0-9.]+)(?:#L(\d+))?\)")
REG = re.compile(r'\b(?:H|handlers_)\["([a-z_0-9]+)"\]\s*=')


def _sources():
    out = []
    for path in sorted(glob.glob(os.path.join(ROOT, "src", "upi*.cpp"))):
        rel = "src/" + os.path.basename(path)
        with open(path, encoding="utf-8") as f:
            out.append((rel, f.read().splitlines()))
    return out


def registrations():
    """{command: (file, line)} for every `H["x"] =` / `handlers_["x"] =` in src/upi*.cpp."""
    reg = {}
    for rel, lines in _sources():
        for i, line in enumerate(lines, 1):
            for m in REG.finditer(line):
                reg.setdefault(m.group(1), (rel, i))
    return reg


def _quoted(name):
    """Where a command registered another way (a loop over names, the parser's exit/quit) first
    appears quoted in the sources, or None."""
    pat = re.compile(r'"' + re.escape(name) + r'"')
    for rel, lines in _sources():
        for i, line in enumerate(lines, 1):
            if pat.search(line):
                return rel, i
    return None


def problems(fix=False):
    """(errors, stale): errors are missing or extra rows and a wrong count; stale are line links that
    drifted, which any edit to the sources causes, so make test reports them without failing (--fix)."""
    reg = registrations()
    with open(DOC, encoding="utf-8") as f:
        doc = f.read().splitlines(keepends=True)
    seen, out, stale = set(), [], []
    for k, line in enumerate(doc):
        m = ROW.match(line)
        if not m:
            continue
        name, rel, ln = m.group(1), m.group(2), m.group(3)
        seen.add(name)
        want = reg.get(name) or _quoted(name)
        if want is None:
            out.append(f"{name}: has a row but is not in src/upi*.cpp")
            continue
        if (rel, int(ln) if ln else None) != want:
            if fix:
                new = f"| [`{name}`](../{want[0]}#L{want[1]})"
                doc[k] = new + line[m.end():]
            else:
                stale.append(f"{name}: links to {rel}{'#L' + ln if ln else ''}, registered at {want[0]}#L{want[1]}")
    rows = sum(1 for line in doc if ROW.match(line))
    m = re.search(r"^(\d+) commands: (\d+) public command names, (\d+) more .*?plus (\d+) local", "".join(doc), re.M)
    if not m:
        out.append("the 'N commands: ...' summary line is missing")
    elif int(m.group(1)) != rows or sum(map(int, m.groups()[1:])) != rows:
        out.append(f"the summary line says {m.group(1)} commands ({' + '.join(m.groups()[1:])}); the table has {rows}")
    for name in sorted(set(reg) - seen):
        out.append(f"{name}: registered at {reg[name][0]}#L{reg[name][1]} but has no row")
    if fix:
        with open(DOC, "w", encoding="utf-8", newline="\n") as f:
            f.writelines(doc)
    return out, stale


if __name__ == "__main__":
    fix = "--fix" in sys.argv[1:]
    errors, stale = problems(fix=fix)
    for p in errors + stale:
        print(p)
    if errors:
        print(f"{len(errors)} error(s): rows to add or remove by hand")
    elif stale and not fix:
        print(f"{len(stale)} stale link(s): run with --fix")
    elif not stale:
        print("docs/commands.md matches the sources")
    sys.exit(1 if errors or (stale and not fix) else 0)
