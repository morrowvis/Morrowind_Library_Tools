import sys
import os
import json
import hashlib
import shutil
import time
import tempfile
import subprocess
import configparser
from concurrent.futures import ThreadPoolExecutor

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TES3CONV = os.path.join(os.path.dirname(SCRIPT_DIR), "Shared", "tes3conv.exe")
HASH_BUF = 1 << 20  # 1 MiB

_TRUE = {"1", "true", "yes", "on"}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def load_settings():
    """Read config.ini next to this script; return the [settings] section as a dict."""
    cfg = configparser.ConfigParser()
    path = os.path.join(SCRIPT_DIR, "config.ini")
    if not cfg.read(path, encoding="utf-8") or not cfg.has_section("settings"):
        print(f"ERROR: missing or malformed config.ini next to the script:\n  {path}")
        sys.exit(2)
    return cfg["settings"]


def walk_files(root, subfolders):
    """rel_lower -> (rel_original, full_path, size)."""
    out = {}
    for sub in subfolders:
        base = os.path.join(root, sub)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, filenames in os.walk(base):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root).replace("\\", "/")
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                out[rel.lower()] = (rel, full, size)
    return out


def file_hash(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(HASH_BUF), b""):
            h.update(chunk)
    return h.hexdigest()


def _mesh_key(mesh):
    """A record's mesh path -> the walk_files key format (root-relative, lower, '/')."""
    m = mesh.lower().replace("\\", "/").lstrip("/")
    if m.startswith("meshes/"):
        m = m[len("meshes/"):]
    return "meshes/" + m


def _tes3conv_records(plugin):
    """Run tes3conv on one plugin; return its record list (or [] on any failure)."""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".json", prefix="elc_")
        os.close(fd)
        proc = subprocess.run(
            [TES3CONV, "-o", plugin, tmp],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        if proc.returncode != 0:
            return []
        with open(tmp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def creature_mesh_exclusions(libs, workers):
    """Meshes that ExportCells' mesh filter would drop: those used *only* by
    creature records. A mesh any non-creature record also uses is kept (that's
    how ExportCells decides). Plugins are the .esp/.esm at each library's root.
    Returns (exclusion_keys, plugin_count)."""
    plugins = {}
    for lib in libs:
        try:
            entries = os.listdir(lib)
        except OSError:
            continue
        for fn in entries:
            if fn.lower().endswith((".esp", ".esm")):
                plugins.setdefault(fn.lower(), os.path.join(lib, fn))
    if not plugins:
        return set(), 0

    creature, kept = set(), set()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for recs in ex.map(_tes3conv_records, plugins.values()):
            for r in recs:
                mesh = r.get("mesh")
                if not mesh:
                    continue
                key = _mesh_key(mesh)
                (creature if r.get("type") == "Creature" else kept).add(key)
    return creature - kept, len(plugins)


def main():
    if len(sys.argv) < 3:
        print("usage: extract_library_changes.py <new_lib> <old_lib>")
        sys.exit(2)
    new_lib, old_lib = sys.argv[1], sys.argv[2]

    s = load_settings()
    subfolders = [x.strip() for x in s.get("subfolders", "meshes, textures").split(",") if x.strip()]
    quick = s.get("quick", "false").strip().lower() in _TRUE
    write_flagged = s.get("flagged_meshes", "true").strip().lower() in _TRUE
    exclude_creatures = s.get("exclude_creatures", "false").strip().lower() in _TRUE
    try:
        workers = int(s.get("workers", "-1"))
    except ValueError:
        workers = -1
    if workers <= 0:
        workers = os.cpu_count() or 1
    output_dir = os.path.join(SCRIPT_DIR, "output")

    for label, path in [("New library", new_lib), ("Old library", old_lib)]:
        if not os.path.isdir(path):
            print(f"ERROR: {label} folder does not exist:\n  {path}")
            sys.exit(1)

    print(f"Scanning new library: {new_lib}")
    new_files = walk_files(new_lib, subfolders)
    print(f"Scanning old library: {old_lib}")
    old_files = walk_files(old_lib, subfolders)
    print(f"  new: {len(new_files)} file(s)   old: {len(old_files)} file(s)")

    excluded_meshes = 0
    if exclude_creatures:
        if not os.path.isfile(TES3CONV):
            print(f"ERROR: exclude_creatures needs tes3conv.exe in the Shared folder:\n  {TES3CONV}")
            sys.exit(2)
        print("Reading plugins for creature meshes (tes3conv)...")
        excl, n_plugins = creature_mesh_exclusions([new_lib, old_lib], workers)
        before = len(new_files) + len(old_files)
        new_files = {k: v for k, v in new_files.items() if k not in excl}
        old_files = {k: v for k, v in old_files.items() if k not in excl}
        excluded_meshes = before - len(new_files) - len(old_files)
        print(f"  {n_plugins} plugin(s) scanned; {len(excl)} creature-only mesh(es) "
              f"-> {excluded_meshes} file(s) dropped from the diff")

    print(f"  comparing subfolders: {', '.join(subfolders)}"
          f"   mode: {'quick (size only)' if quick else 'size + hash'}\n")

    def classify(item):
        _key, (rel, full, size) = item
        old = old_files.get(_key)
        if old is None:
            return ("NEW", rel, full, None)
        _orel, ofull, osize = old
        if size != osize:
            return ("MODIFIED", rel, full, f"size {osize}->{size}")
        if quick:
            return ("SAME", rel, full, None)
        if file_hash(full) != file_hash(ofull):
            return ("MODIFIED", rel, full, "content changed (same size)")
        return ("SAME", rel, full, None)

    items = sorted(new_files.items())
    new_only, modified, same = [], [], 0
    started = time.time()

    # Threads, not processes: the work is I/O-bound (hashing reads files), which
    # threads speed up, and they don't spawn child processes -- so the launcher's
    # `pause` is never disturbed. Results come back in submission order.
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, (cat, rel, full, why) in enumerate(ex.map(classify, items), 1):
            if cat == "NEW":
                new_only.append((rel, full))
            elif cat == "MODIFIED":
                modified.append((rel, full, why))
            else:
                same += 1
            if i % 500 == 0 or i == len(items):
                print(f"\r  compared {i}/{len(items)}  ({time.time() - started:.0f}s)   ",
                      end="", flush=True)
    print()

    to_copy = [(rel, full, "NEW") for rel, full in new_only] + \
              [(rel, full, "MODIFIED") for rel, full, _why in modified]

    for rel, full, _tag in to_copy:
        dest = os.path.join(output_dir, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(full, dest)

    os.makedirs(output_dir, exist_ok=True)

    # flagged_meshes.txt for the MWSE Thumbnail Generator: one record-style mesh
    # path per line (relative to meshes\, backslashes, leading "meshes\" dropped).
    # Only .nif files -- textures/.kf are not render subjects.
    flagged = []
    if write_flagged:
        for rel, _full, _tag in to_copy:
            low = rel.lower()
            if low.startswith("meshes/") and low.endswith(".nif"):
                flagged.append(rel[len("meshes/"):].replace("/", "\\"))
        with open(os.path.join(output_dir, "flagged_meshes.txt"), "w", encoding="utf-8") as f:
            for line in sorted(flagged):
                f.write(line + "\n")

    with open(os.path.join(output_dir, "_report.txt"), "w", encoding="utf-8") as f:
        f.write("Library comparison report\n")
        f.write(f"  generated : {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"  new library: {new_lib}\n")
        f.write(f"  old library: {old_lib}\n")
        f.write(f"  subfolders : {', '.join(subfolders)}\n")
        f.write(f"  mode       : {'quick (size only)' if quick else 'size + hash'}\n")
        if exclude_creatures:
            f.write(f"  creatures  : excluded ({excluded_meshes} mesh file(s) dropped)\n")
        f.write("\n")
        f.write(f"NEW files ({len(new_only)}):\n")
        for rel, _full in new_only:
            f.write(f"  {rel}\n")
        f.write(f"\nMODIFIED files ({len(modified)}):\n")
        for rel, _full, why in modified:
            f.write(f"  {rel}   [{why}]\n")
        # files removed in the new library (present in old, gone in new)
        removed = [old_files[k][0] for k in old_files if k not in new_files]
        f.write(f"\nREMOVED in new library ({len(removed)}) - not copied, listed only:\n")
        for rel in sorted(removed):
            f.write(f"  {rel}\n")

    print("\n" + "=" * 48)
    print(f"  NEW      : {len(new_only)}")
    print(f"  MODIFIED : {len(modified)}")
    print(f"  unchanged: {same}")
    print(f"  copied   : {len(to_copy)} file(s) -> {output_dir}")
    if write_flagged:
        print(f"  flagged  : {len(flagged)} .nif path(s) -> flagged_meshes.txt")
    print("=" * 48)


if __name__ == "__main__":
    main()
