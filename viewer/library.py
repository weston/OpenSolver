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

SCHEMA = 1
KINDS = {"range": "ranges", "config": "configs"}
EXT = {"range": ".txt", "config": ".json"}
MAX_NAME = 200
MAX_COMPONENT = 100
MAX_CONTENT = 1 << 20  # 1 MB; ranges and configs are far smaller


class LibraryError(Exception):
    """Bad name, bad content, missing entry, or path-confinement violation."""


class Library:
    def __init__(self, root):
        self.root = os.path.abspath(os.path.expanduser(root))

    # ------------------------------------------------------------- paths

    def _section(self, kind):
        if kind not in KINDS:
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
        if not os.path.isdir(section):
            return out
        for dirpath, dirnames, filenames in os.walk(section):
            dirnames[:] = [d for d in dirnames
                           if not os.path.islink(os.path.join(dirpath, d))]
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
                out.append({"name": name, "bytes": st.st_size, "mtime": int(st.st_mtime)})
        out.sort(key=lambda e: e["name"])
        return out

    def get(self, kind, name):
        """Range content as text, or config content as a parsed JSON object."""
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
        tmp = f"{path}.tmp.{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)  # atomic on the same filesystem
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return name

    def rename(self, kind, src, dst):
        """Rename or move an entry; moving to a new folder creates it."""
        spath = self._existing_file(kind, src)
        dpath = self._resolve(kind, dst)
        if dpath == spath:
            return dst
        if os.path.lexists(dpath):
            raise LibraryError("target already exists")
        os.makedirs(os.path.dirname(dpath), exist_ok=True)
        os.rename(spath, dpath)
        self._prune_empty_dirs(self._section(kind), os.path.dirname(spath))
        return dst

    def delete(self, kind, name):
        path = self._existing_file(kind, name)
        os.remove(path)
        self._prune_empty_dirs(self._section(kind), os.path.dirname(path))
