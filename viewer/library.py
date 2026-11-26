#!/usr/bin/env python3
"""Named ranges and tree-config library for the viewer, stored as plain files (#116).

Layout under the library root (default ~/.wsolver/library, overridable with
`viewer/server.py --library`):

    <root>/ranges/<folder>/<Name>.txt      solver range syntax, one file per range
    <root>/configs/<folder>/<Name>.json    build-form config, JSON with "schema": 1

Names are '/'-separated paths of folder components, e.g. "BTN vs BB/SRP/BB call".
Folders exist only as directories on disk. Files are human-readable so users can
also manage the library with Finder, an editor, or git.

Path confinement: every name is resolved against the section root and must stay
inside it. Absolute paths, NUL bytes, backslashes, empty/"." /".." components and
symlink escapes (checked with os.path.realpath) are all rejected.
"""

import json
import os
import shutil
import tempfile
import threading
import time

SCHEMA = 1
KINDS = {"range": "ranges", "config": "configs", "sim": "simulations"}
def _replace(src, dst):
    """os.replace, retried for ~2 s on Windows sharing violations: there a file another thread is replacing
    or reading at that moment can't be replaced, which is a wait, not a failure (#369)."""
    for k in range(100):
        try:
            return os.replace(src, dst)
        except PermissionError:
            if os.name != "nt" or k == 99:
                raise
            time.sleep(0.02)


EXT = {"range": ".txt", "config": ".json", "sim": ".cfr"}
MAX_NAME = 200
MAX_COMPONENT = 100
MAX_CONTENT = 1 << 20  # 1 MB; ranges and configs are far smaller


class LibraryError(Exception):
    """Bad name, bad content, missing entry, or path-confinement violation."""


class Library:
    def __init__(self, root):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.simulation_lock = threading.RLock()

    def _job_simulations(self):
        try:
            with open(os.path.join(self.root, ".job-simulations.json")) as file:
                return json.load(file)
        except FileNotFoundError:
            return {}

    def _write_job_simulations(self, entries):
        os.makedirs(self.root, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".job-simulations-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as file:
                json.dump(entries, file)
            _replace(tmp, os.path.join(self.root, ".job-simulations.json"))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def include_job_simulations(self, simulations):
        with self.simulation_lock:
            entries = self._job_simulations()
            names = {entry["name"] for entry in self.list("sim")}
            changed = False
            for simulation in simulations:
                path = simulation["path"]
                if path in entries:
                    metadata = entries[path]["metadata"]
                    if any(metadata.get(key) != value for key, value in simulation["metadata"].items()):
                        metadata.update(simulation["metadata"])
                        changed = True
                    continue
                name = simulation["name"]
                suffix = 2
                while name in names:
                    name = f'{simulation["name"]} ({suffix})'
                    suffix += 1
                self._check_name(name)
                entries[path] = {"name": name, "metadata": simulation["metadata"]}
                names.add(name)
                changed = True
            if changed:
                self._write_job_simulations(entries)

    def job_simulation(self, name):
        self._check_name(name)
        for path, entry in self._job_simulations().items():
            if entry["name"] == name:
                return {**entry, "path": path}
        return None

    def _rename_job_simulation(self, src, dst):
        self._check_name(src)
        with self.simulation_lock:
            entries = self._job_simulations()
            for entry in entries.values():
                if entry["name"] == src:
                    entry["name"] = dst
                    self._write_job_simulations(entries)
                    return True
        return False

    def install_defaults(self):
        marker = os.path.join(self.root, ".defaults-v2")
        if os.path.exists(marker):
            return
        defaults = Library(os.path.join(os.path.dirname(__file__), "defaults"))
        for kind in KINDS:
            for entry in defaults.list(kind):
                name = entry["name"]
                if not os.path.exists(self._resolve(kind, name)):
                    self.save(kind, name, defaults.get(kind, name))
        os.makedirs(self.root, exist_ok=True)
        with open(marker, "w") as f:
            f.write("2\n")

    # ------------------------------------------------------------- paths

    def _section(self, kind):
        if not isinstance(kind, str) or kind not in KINDS:  # a list or dict kind is unhashable: a 500, not a 400
            raise LibraryError(f"kind must be one of {sorted(KINDS)}")
        return os.path.realpath(os.path.join(self.root, KINDS[kind]))

    def _check_name(self, name):
        if not isinstance(name, str):
            raise LibraryError("bad name")
        if "\x00" in name:
            raise LibraryError("NUL byte in name")
        if os.path.isabs(name) or "\\" in name:
            raise LibraryError("absolute path or backslash in name")
        if not name or len(name) > MAX_NAME:
            raise LibraryError("bad name length")
        for part in name.split("/"):
            if part in ("", ".", ".."):
                raise LibraryError(f"bad path component {part!r}")
            if len(part) > MAX_COMPONENT:
                raise LibraryError("path component too long")
        return name

    def _resolve(self, kind, name):
        """Map a (kind, name) to a filesystem path provably inside the section root.

        realpath resolves symlinks in every existing path component, so a symlink
        inside the library that points outside it is caught by the prefix check.
        """
        self._check_name(name)
        section = self._section(kind)
        target = os.path.realpath(os.path.join(section, name + EXT[kind]))
        if target != section and not target.startswith(section + os.sep):
            raise LibraryError("path escapes the library")
        return target

    def _existing_file(self, kind, name):
        path = self._resolve(kind, name)
        if not os.path.isfile(path):
            raise LibraryError("no such entry")
        return path

    def _prune_empty_dirs(self, section, dirpath):
        """Remove now-empty folders left by a delete/rename, up to the section root."""
        while dirpath != section and dirpath.startswith(section + os.sep):
            try:
                os.rmdir(dirpath)  # fails when non-empty: stop there
            except OSError:
                return
            dirpath = os.path.dirname(dirpath)

    # ----------------------------------------------------------- configs

    def _check_config(self, data):
        if not isinstance(data, dict):
            raise LibraryError("config must be a JSON object")
        schema = data.get("schema", SCHEMA)
        if schema != SCHEMA:
            raise LibraryError(f"unsupported config schema {schema!r}")
        # Optional node locks of the spot (#200), as /api/lock/export gives them: the viewer sets
        # them again after building the spot. Only the shape is checked here; /api/lock/apply
        # checks them against the tree.
        locks = data.get("locks")
        if locks is not None and (not isinstance(locks, list) or not all(
                isinstance(l, dict) and isinstance(l.get("id"), str) and isinstance(l.get("freq"), list)
                for l in locks)):
            raise LibraryError("locks must be a list of {id, mode, freq, ...} objects")
        return data

    def _config_text(self, content):
        """Configs are JSON objects with "schema": 1. Unknown extra fields are kept:
        the form redesign and #114 add sizing forms a current build doesn't know."""
        data = dict(self._check_config(content))
        data["schema"] = SCHEMA
        try:
            return json.dumps(data, indent=2, sort_keys=True) + "\n"
        except (TypeError, ValueError):
            raise LibraryError("config is not JSON-serializable") from None

    # ------------------------------------------------------------- CRUD

    def list(self, kind):
        """All entries as a flat, sorted list of {'name', 'bytes', 'mtime'}."""
        section = self._section(kind)
        out = []
        if kind == "sim":
            for path, entry in self._job_simulations().items():
                if entry["name"] is not None and os.path.isfile(path):
                    stat = os.stat(path)
                    out.append({**entry["metadata"], "name": entry["name"], "job": True, "bytes": stat.st_size, "mtime": int(stat.st_mtime)})
        if not os.path.isdir(section):
            return sorted(out, key=lambda entry: entry["name"])
        for dirpath, dirnames, filenames in os.walk(section):
            dirnames[:] = [d for d in dirnames
                           if not os.path.islink(os.path.join(dirpath, d)) and not (kind == "sim" and d.startswith(".open-"))]
            for f in sorted(filenames):
                full = os.path.join(dirpath, f)
                if not f.endswith(EXT[kind]) or os.path.islink(full) or not os.path.isfile(full):
                    continue
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                name = os.path.relpath(full, section)[:-len(EXT[kind])].replace(os.sep, "/")
                try:
                    self._check_name(name)
                except LibraryError:
                    continue
                entry = {"name": name, "bytes": st.st_size, "mtime": int(st.st_mtime)}
                if kind == "sim":
                    entry.update(self.simulation_metadata(full))
                out.append(entry)
        out.sort(key=lambda e: e["name"])
        return out

    def get(self, kind, name):
        """Range content as text, or config content as a parsed JSON object."""
        if kind == "sim":
            path = self.simulation_path(name)
            stat = os.stat(path)
            job = self.job_simulation(name)
            metadata = job["metadata"] if job else self.simulation_metadata(path)
            return {**metadata, "bytes": stat.st_size, "mtime": int(stat.st_mtime)}
        path = self._existing_file(kind, name)
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        if kind == "config":
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                raise LibraryError("config is not valid JSON") from None
            return self._check_config(data)
        return text

    def save(self, kind, name, content):
        """Create or overwrite an entry, creating folders as needed."""
        path = self._resolve(kind, name)
        if kind == "sim":
            raise LibraryError("use the simulation save operation")
        if kind == "config":
            text = self._config_text(content)
        else:
            if not isinstance(content, str) or not content.strip():
                raise LibraryError("range content must be non-empty text")
            if "\x00" in content:
                raise LibraryError("NUL byte in content")
            text = content if content.endswith("\n") else content + "\n"
        if len(text.encode("utf-8")) > MAX_CONTENT:
            raise LibraryError("content too large")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # a temp name of its own per write: the viewer's HTTP threads share one pid, so `.tmp.<pid>`
        # made two saves of one name write the same file and fail (#369). The suffix keeps it out of
        # listings (they match .txt / .json).
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".save-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            _replace(tmp, path)  # atomic on the same filesystem
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return name

    def rename(self, kind, src, dst):
        """Rename or move an entry; moving to a new folder creates it."""
        if kind == "sim":
            self._check_name(dst)
            if src != dst and any(entry["name"] == dst for entry in self.list(kind)):
                raise LibraryError("target already exists")
            if self._rename_job_simulation(src, dst):
                return dst
        spath = self._existing_file(kind, src)
        dpath = self._resolve(kind, dst)
        if dpath == spath:
            return dst
        if os.path.lexists(dpath):
            raise LibraryError("target already exists")
        os.makedirs(os.path.dirname(dpath), exist_ok=True)
        os.rename(spath, dpath)
        if kind == "sim" and os.path.isfile(spath + ".json"):
            _replace(spath + ".json", dpath + ".json")
        self._prune_empty_dirs(self._section(kind), os.path.dirname(spath))
        return dst

    def delete(self, kind, name):
        if kind == "sim" and self._rename_job_simulation(name, None):
            return
        path = self._existing_file(kind, name)
        os.remove(path)
        if kind == "sim" and os.path.isfile(path + ".json"):
            os.remove(path + ".json")
        self._prune_empty_dirs(self._section(kind), os.path.dirname(path))

    def simulation_metadata(self, path):
        try:
            with open(path + ".json", encoding="utf-8") as file:
                data = json.load(file)
            stat = os.stat(path)
            if data["bytes"] == stat.st_size and data["mtime_ns"] == stat.st_mtime_ns:
                return {key: data["metadata"][key] for key in ("board", "pot", "stack", "big_blind", "save_mode") if key in data["metadata"]}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return {}

    def update_simulation_metadata(self, name, metadata):
        with self.simulation_lock:
            entries = self._job_simulations()
            for entry in entries.values():
                if entry["name"] == name:
                    entry["metadata"] = metadata
                    self._write_job_simulations(entries)
                    return
        path = self._existing_file("sim", name)
        stat = os.stat(path)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".metadata-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as file:
                json.dump({"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns, "metadata": metadata}, file)
            _replace(tmp, path + ".json")
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def save_simulation(self, name, write_tree, metadata, overwrite=False):
        path = self._resolve("sim", name)
        if (os.path.exists(path) or self.job_simulation(name)) and not overwrite:
            raise LibraryError("a simulation with that name already exists")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._section("sim"), prefix=".simulation-", suffix=".tmp")
        os.close(fd)
        try:
            write_tree(tmp)
            _replace(tmp, path)
            self._rename_job_simulation(name, None)
            self.update_simulation_metadata(name, metadata)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return name

    def simulation_path(self, name):
        job = self.job_simulation(name)
        if job:
            if not os.path.isfile(job["path"]):
                raise LibraryError("no such entry")
            return job["path"]
        return self._existing_file("sim", name)

    def simulation_snapshot(self, name):
        source = self.simulation_path(name)
        snapshot = tempfile.TemporaryDirectory(prefix=".open-", dir=self._section("sim"))
        try:
            target = os.path.join(snapshot.name, "tree.cfr")
            os.link(source, target)
            return snapshot, target
        except Exception:
            snapshot.cleanup()
            raise

    def export_simulation(self, name, destination):
        source = self.simulation_path(name)
        destination = os.path.abspath(os.path.expanduser(destination))
        with open(source, "rb") as src, open(destination, "xb") as dst:
            try:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            except Exception:
                os.remove(destination)
                raise
