import os
import re
import sys
import glob
import math
import json
import fnmatch
import time
import tempfile
import subprocess
import configparser
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TES3CONV = os.path.join(os.path.dirname(SCRIPT_DIR), "Shared", "tes3conv.exe")
_TRUE = {"1", "true", "yes", "on"}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# tes3conv record type -> display name. Only these placeable object types are kept
# as records; everything else (incl. creatures and body parts) is skipped.
OBJECT_TYPE_NAMES = {
    "Activator": "Activator", "Alchemy": "Alchemy", "Apparatus": "Apparatus",
    "Armor": "Armor", "Book": "Book", "Clothing": "Clothing", "Container": "Container",
    "Door": "Door", "Ingredient": "Ingredient", "Light": "Light", "Lockpick": "Lockpick",
    "MiscItem": "Misc Item", "Npc": "NPC", "Probe": "Probe", "RepairItem": "Repair Item",
    "Static": "Static", "Weapon": "Weapon",
}

# Types objectTypeNames doesn't map: ExportCells emits tostring(objType) -- the
# decimal of the record's FourCC. These are in nonDynamicData.objects too, so they
# appear as bare id/type/source_mod records with a numeric object_type string.
NUMERIC_TYPES = {
    "LeveledCreature": "1129727308",  # "LEVC"
    "LeveledItem": "1230390604",      # "LEVI"
    "Enchanting": "1212370501",       # "ENCH"
}
KEEP_TYPES = set(OBJECT_TYPE_NAMES) | set(NUMERIC_TYPES)

# Let There Be Darkness runtime light overrides. LTBD rewrites each base light's
# radius/colour when the game loads and ExportCells captured the result, so we replay
# it: apply the TLaD table (colour+radius), then the True Skyrimized Torches table
# (radius only, colour preserved), then nuke NEGATIVE lights to black. Set in main().
LTBD_TLAD = {}   # id.lower() -> {'color': (r,g,b), 'radius': int}
LTBD_TST = {}    # id.lower() -> {'radius': int}
_LTBD_REL = os.path.join("MWSE", "mods", "RFD", "LetThereBeDarkness", "overrides.lua")

# race id.lower() -> True if a beast race (Argonian/Khajiit/modded). Beast NPCs with
# no record mesh use base_animKnA.nif instead of base_anim[_female].nif.
RACE_BEAST = {}

CHUNK = 1000  # records per part
IDENTITY = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
_MESHES_PREFIX = re.compile(r"^(.*[\\/])?meshes[\\/]", re.IGNORECASE)


def num(x):
    """Reproduce ExportCells jsonNumber (%.8g) as it survives the merge's json.loads:
    ints stay ints (radius 384 -> 384), floats get 8 significant digits
    (2/255 -> 0.0078431373). This is exactly what merge_jsons would parse."""
    try:
        return json.loads("%.8g" % float(x))
    except (ValueError, TypeError):
        return 0


def compile_matcher(pattern):
    """Mirror Thumbnail Generator's search filter: comma-separated AND-terms; a term
    with * or ? is a whole-field glob, otherwise a case-insensitive substring; each
    term is tested against the record's id / name / mesh / source_mod (any field).
    Returns None for a blank pattern (imposes no constraint = match all)."""
    term_fns = []
    for raw in (pattern or "").split(","):
        t = raw.strip().lower().replace("\\", "/")
        if not t:
            continue
        if "*" in t or "?" in t:
            rx = re.compile(fnmatch.translate(t))
            term_fns.append(lambda fields, _rx=rx: any(_rx.match(f) for f in fields))
        else:
            term_fns.append(lambda fields, _t=t: any(_t in f for f in fields))
    if not term_fns:
        return None

    def matcher(obj):
        fields = ((obj.get("object_id") or "").lower(),
                  (obj.get("object_name") or "").lower(),
                  (obj.get("mesh") or "").lower().replace("\\", "/"),
                  (obj.get("source_mod") or "").lower())
        return all(fn(fields) for fn in term_fns)
    return matcher


def pretty_json(obj, level=0):
    """Pretty-print like ExportCells' individual files: 2-space indent for objects
    and object-arrays, but scalar arrays (matrix rows, colours) stay on one line --
    unlike json.dump(indent=...), which puts every number on its own line."""
    ind, ind1 = "  " * level, "  " * (level + 1)
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        body = ",\n".join(ind1 + json.dumps(k) + ": " + pretty_json(v, level + 1)
                          for k, v in obj.items())
        return "{\n" + body + "\n" + ind + "}"
    if isinstance(obj, list):
        if not obj:
            return "[]"
        if all(not isinstance(e, (list, dict)) for e in obj):   # matrix row / colour
            return "[" + ", ".join(json.dumps(e) for e in obj) + "]"
        body = ",\n".join(ind1 + pretty_json(e, level + 1) for e in obj)
        return "[\n" + body + "\n" + ind + "]"
    return json.dumps(obj)


def load_settings():
    cfg = configparser.ConfigParser()
    path = os.path.join(SCRIPT_DIR, "config.ini")
    if not cfg.read(path, encoding="utf-8") or not cfg.has_section("settings"):
        print(f"ERROR: missing or malformed config.ini next to the script:\n  {path}")
        sys.exit(2)
    return cfg["settings"]


# ---- MO2 resolution (same model as Extract_Scripts) ---------------------

def _ini_value(raw):
    val = raw.strip()
    if val.startswith("@ByteArray(") and val.endswith(")"):
        val = val[len("@ByteArray("):-1]
    return val.replace("\\\\", "\\")


def read_mo2_ini(base):
    profile, game_path = "Default", None
    try:
        with open(os.path.join(base, "ModOrganizer.ini"), encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                low = s.lower()
                if low.startswith("selected_profile"):
                    profile = _ini_value(s.split("=", 1)[1]) or profile
                elif low.startswith("gamepath"):
                    game_path = _ini_value(s.split("=", 1)[1])
    except OSError:
        pass
    return profile, game_path


_GAMEFILE = re.compile(r"GameFile(\d+)\s*=\s*(.+)", re.IGNORECASE)


def read_active_plugins(ini_path):
    entries = []
    try:
        with open(ini_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _GAMEFILE.match(line.strip())
                if m and m.group(2).strip():
                    entries.append((int(m.group(1)), m.group(2).strip()))
    except OSError:
        return []
    entries.sort()
    return [name for _i, name in entries]


def read_local_settings(prof_dir):
    try:
        with open(os.path.join(prof_dir, "settings.ini"), encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip().lower().replace(" ", "")
                if s.startswith("localsettings="):
                    return s.split("=", 1)[1] in _TRUE
    except OSError:
        pass
    return False


def read_lines(path):
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    out.append(line)
    except OSError:
        pass
    return out


def collect_mo2_active(base, plugins_mode):
    profile, game_path = read_mo2_ini(base)
    game_data = os.path.join(game_path, "Data Files") if game_path else None
    prof_dir = os.path.join(base, "profiles", profile)

    enabled = []
    for line in read_lines(os.path.join(prof_dir, "modlist.txt")):
        if line.startswith("+") and not line.endswith("_separator"):
            enabled.append(line[1:])

    search = [os.path.join(base, "overwrite")]
    search += [os.path.join(base, "mods", m) for m in enabled]
    if game_data:
        search.append(game_data)

    plugin_files = {}
    for entry in search:
        try:
            names = os.listdir(entry)
        except OSError:
            continue
        for fn in names:
            low = fn.lower()
            if low.endswith(".esp") or low.endswith(".esm"):
                plugin_files.setdefault(low, os.path.join(entry, fn))

    if plugins_mode == "all":
        wanted = read_lines(os.path.join(prof_dir, "loadorder.txt"))
        source = "all managed plugins (loadorder.txt)"
    else:
        local = read_local_settings(prof_dir)
        prof_ini = os.path.join(prof_dir, "Morrowind.ini")
        game_ini = os.path.join(game_path, "Morrowind.ini") if game_path else None
        primary, secondary = (prof_ini, game_ini) if local else (game_ini, prof_ini)
        wanted = read_active_plugins(primary) if primary else []
        if not wanted and secondary:
            wanted = read_active_plugins(secondary)
        source = "active plugins (Morrowind.ini [Game Files])"
    if not wanted:
        print("ERROR: could not resolve the plugin list. Use plugins = all to fall back "
              "to loadorder.txt, or check the MO2 folder.")
        sys.exit(1)

    jobs, missing = [], []
    for name in wanted:
        path = plugin_files.get(name.lower())
        if path:
            jobs.append((name, path))
        else:
            missing.append(name)
    return profile, len(enabled), jobs, missing, source, search


def collect_folder(root):
    jobs = []
    for dirpath, _dirs, filenames in os.walk(root):
        for fn in filenames:
            low = fn.lower()
            if low.endswith(".esp") or low.endswith(".esm"):
                jobs.append((fn, os.path.join(dirpath, fn)))
    return sorted(jobs, key=lambda j: j[0].lower())


# ---- Let There Be Darkness overrides ------------------------------------

def find_ltbd(base):
    """overrides.lua from the highest-priority enabled mod that ships it, or None."""
    profile, _ = read_mo2_ini(base)
    prof_dir = os.path.join(base, "profiles", profile)
    enabled = [line[1:] for line in read_lines(os.path.join(prof_dir, "modlist.txt"))
               if line.startswith("+") and not line.endswith("_separator")]
    search = [os.path.join(base, "overwrite")] + [os.path.join(base, "mods", m) for m in enabled]
    for entry in search:
        p = os.path.join(entry, _LTBD_REL)
        if os.path.isfile(p):
            return p
    return None


def parse_ltbd(path):
    """Parse the two per-light override tables separately:
      overrideLightTLaD -> {id_lower: {'color': (r,g,b), 'radius': int}}
      overrideLightTST  -> {id_lower: {'radius': int}}
    (overrideTableTLaD / overrideTableDL are keyed by cell, not light, so ignored.)"""
    tables = {"overrideLightTLaD": {}, "overrideLightTST": {}}
    cur_table = None
    cur = None
    ent = {}

    def flush():
        if cur_table and cur is not None:
            tables[cur_table][cur] = ent

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                mt = re.match(r'(\w+)\s*=\s*\{$', s)   # top-level table start
                if mt:
                    flush()
                    cur, ent = None, {}
                    cur_table = mt.group(1) if mt.group(1) in tables else None
                    continue
                if cur_table is None:
                    continue
                m = re.match(r'\["(.+?)"\]\s*=\s*\{', s)   # entry start
                if m:
                    flush()
                    cur, ent = m.group(1).lower(), {}
                    continue
                mc = re.search(r'color\s*=\s*tes3vector3\.new\((\d+)\s*,\s*(\d+)\s*,\s*(\d+)\)', s)
                if mc and cur is not None:
                    ent["color"] = tuple(int(v) for v in mc.groups())
                mr = re.search(r'radius\s*=\s*(\d+)', s)
                if mr and cur is not None:
                    ent["radius"] = int(mr.group(1))
            flush()
    except OSError:
        return {}, {}
    return tables["overrideLightTLaD"], tables["overrideLightTST"]


# ---- tes3conv ------------------------------------------------------------

def run_tes3conv(job):
    """(name, records, error) for one plugin."""
    name, path = job
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".json", prefix="cmr_")
        os.close(fd)
        proc = subprocess.run(
            [TES3CONV, "-o", path, tmp],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, creationflags=_NO_WINDOW,
        )
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            return (name, [], err[-1] if err else f"tes3conv exit {proc.returncode}")
        with open(tmp, encoding="utf-8") as f:
            records = json.load(f)
    except Exception as e:
        return (name, [], f"{type(e).__name__}: {e}")
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return (name, records, None)


# ---- record -> object ----------------------------------------------------

def relative_mesh(mesh):
    """Mirror utils.getRelativeMeshPath: strip a leading "...\\meshes\\", switch
    slashes to backslash, and lowercase only the extension (base case preserved)."""
    if not mesh:
        return None
    m = _MESHES_PREFIX.sub("", mesh).replace("/", "\\")
    m = re.sub(r"\.[^.]+$", lambda mm: mm.group(0).lower(), m)
    return m or None


def _is_female(rec):
    return "female" in (rec.get("npc_flags") or "").lower()


def _spaced(s):
    """tes3conv gives PascalCase slot names (RightGauntlet); MWSE's slotName has
    spaces (Right Gauntlet). Split on the camelCase boundary."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)


def build_record_object(rec, source_mod):
    """One top-level record object (phase 1: no NIF children). Returns (dict, mesh_or_None)
    or None if the record is skipped (grass mesh)."""
    t = rec.get("type")
    obj_id = rec.get("id", "")

    # leveled lists / enchantments: bare id + numeric object_type + source_mod
    if t in NUMERIC_TYPES:
        return {"name": obj_id, "type": "EMPTY", "matrix_local": IDENTITY,
                "parent": "master records", "object_id": obj_id,
                "object_type": NUMERIC_TYPES[t], "source_mod": source_mod}, None

    display = OBJECT_TYPE_NAMES.get(t)
    if not display:
        return None
    data = rec.get("data") or {}
    is_light = (t == "Light")

    # NPCs with no record mesh fall back to the skeleton (engine behaviour):
    # beast races use base_animKnA.nif, otherwise by gender.
    mesh = rec.get("mesh") or ""
    if t == "Npc" and not mesh:
        if RACE_BEAST.get((rec.get("race") or "").lower()):
            mesh = "base_animKnA.nif"
        elif _is_female(rec):
            mesh = "base_anim_female.nif"
        else:
            mesh = "base_anim.nif"
    if mesh:
        low = mesh.lower().replace("/", "\\")
        if low.startswith("grass\\") or "meshes\\grass\\" in low:
            return None  # ExportCells skips grass meshes entirely

    fields = [("object_id", obj_id)]
    name = rec.get("name")
    if name:
        fields.append(("object_name", name))

    # arrows/bolts are reclassified as Ammunition (no weapon_type field emitted)
    weapon_type = None
    if t == "Weapon":
        wt = data.get("weapon_type")
        if wt in ("Arrow", "Bolt"):
            display = "Ammunition"
        elif wt:
            # MWSE names the two-handed axe differently from tes3conv
            weapon_type = "AxeTwoClose" if wt == "AxeTwoHand" else wt
    fields.append(("object_type", display))
    if weapon_type:
        fields.append(("weapon_type", weapon_type))
    elif t == "Clothing" and data.get("clothing_type"):
        fields.append(("clothing_type", _spaced(data["clothing_type"])))
    elif t == "Apparatus" and data.get("apparatus_type"):
        at = data["apparatus_type"]
        # MWSE apparatus uses the camelCase enum key (alembic, not Alembic)
        fields.append(("apparatus_type", at[0].lower() + at[1:] if at else at))
    elif t == "Armor" and data.get("armor_type"):
        fields.append(("armor_type", _spaced(data["armor_type"])))

    fields.append(("source_mod", source_mod))
    if is_light and "carry" in (data.get("flags") or "").lower():
        fields.append(("can_carry", True))
    rel = relative_mesh(mesh)
    if rel:
        fields.append(("mesh", rel))
    script = rec.get("script")
    if script:
        fields.append(("script", script))

    if is_light:
        color = data.get("color")
        radius = data.get("radius")
        lid = obj_id.lower()
        tlad = LTBD_TLAD.get(lid)          # TLaD: colour + radius
        if tlad:
            if "color" in tlad:
                color = tlad["color"]
            if "radius" in tlad:
                radius = tlad["radius"]
        tst = LTBD_TST.get(lid)            # then TST: radius only, colour kept
        if tst and "radius" in tst:
            radius = tst["radius"]
        if "NEGATIVE" in (data.get("flags") or "").upper():
            color = (0, 0, 0)              # then nuke negative lights to black
        if color and len(color) >= 3 and radius is not None:
            ld = {"color": [num(color[0] / 255), num(color[1] / 255), num(color[2] / 255)],
                  "radius": num(radius)}
            fields.insert(len(fields) - 1, ("light_data", ld))

    obj = {"name": obj_id, "type": "EMPTY", "matrix_local": IDENTITY, "parent": "master records"}
    for k, v in fields:
        obj[k] = v
    return obj, (rel and mesh)


# ---- Phase 2: NIF child expansion (es3) ---------------------------------
# Records whose mesh contains particle/light nodes (and all lights) get the mesh's
# emitter/particle/light/AttachLight sub-nodes expanded as children, mirroring
# jsons.processInstance. Uses the loose .nif on disk (es3); the game's runtime scene
# graph can differ, so this is a close approximation, not a byte-match.

_ES3 = None
_SPECIAL = (b"NiBSParticleNode", b"NiPointLight", b"NiSpotLight")
_INST = "\x01INST"   # placeholder base -> the record's own id (unnamed light nodes)


def load_es3():
    global _ES3
    if _ES3 is None:
        pattern = os.path.join(os.path.expandvars(r"%APPDATA%"), "Blender Foundation",
                               "Blender", "*", "scripts", "addons", "io_scene_mw", "lib")
        hits = sorted(glob.glob(pattern))
        if hits:
            sys.path.insert(0, hits[-1])
        from es3 import nif
        _ES3 = nif
    return _ES3


def _tn(o):
    return type(o).__name__


def _mat4(o):
    """buildMatrix4x4: rows = rotation columns * scale, last row = translation."""
    t, R = o.translation, o.rotation
    s = o.scale if o.scale else 1.0
    rows = [[num(R[0][c] * s), num(R[1][c] * s), num(R[2][c] * s), 0] for c in range(3)]
    rows.append([num(t[0]), num(t[1]), num(t[2]), 1])
    return rows


def _particle_ctrl(pnode):
    for ch in (pnode.children or []):
        if ch is None:
            continue
        c = getattr(ch, "controller", None)
        while c is not None:
            if _tn(c) == "NiParticleSystemController":
                return c
            c = getattr(c, "next_controller", None)
    return None


def _emissive(pnode):
    for ch in (pnode.children or []):
        if ch is None:
            continue
        for p in (ch.properties or []):
            if _tn(p) == "NiMaterialProperty" and hasattr(p, "emissive_color"):
                return p.emissive_color
    return None


def build_template(root, is_light):
    """Traverse a mesh once into a reusable template: (emitter_bases, protos).
    Returns None if the mesh yields no children. Names are assigned later, per
    record, so the chunk-global counter is honoured."""
    order, parent = [], {}

    def walk(o):
        order.append(o)
        for c in (o.children or []):
            if c is not None:
                parent[id(c)] = o
                walk(c)
    walk(root)

    def is_particle(o):
        return _tn(o) == "NiBSParticleNode"

    def is_lightnode(o):
        return _tn(o) in ("NiPointLight", "NiSpotLight")

    has_special = any(is_particle(o) or is_lightnode(o) for o in order[1:])
    if not is_light and not has_special:
        return None

    selected = set()
    emitters = {}          # id(emitter_node) -> index into emitter_list
    emitter_list = []

    def mark_ancestors(node):
        p = parent.get(id(node))
        while p is not None and p is not root:
            selected.add(p.name or "")
            p = parent.get(id(p))

    for o in order:
        if is_lightnode(o) or is_particle(o):
            selected.add(o.name or "")
            mark_ancestors(o)
        if is_particle(o):
            ctrl = _particle_ctrl(o)
            em = getattr(ctrl, "emitter", None) if ctrl else None
            if em is not None and id(em) not in emitters:
                emitters[id(em)] = len(emitter_list)
                emitter_list.append({"base": em.name or "emitter", "birth": ctrl.birth_rate,
                                     "speed": ctrl.speed, "size": ctrl.initial_size,
                                     "emissive": _emissive(o)})
                selected.add(em.name or "")
                mark_ancestors(em)

    protos, proto_idx = [], {}
    for o in order:
        if o is root:
            continue
        par = parent.get(id(o))
        if par is None:
            continue
        if par is root:
            parent_idx = -1
        else:
            parent_idx = proto_idx.get(id(par))
            if parent_idx is None:
                continue   # parent was filtered out -> skip
        if _tn(o) == "RootCollisionNode":
            continue
        nm = o.name or ""
        if (is_light or has_special) and nm not in selected and o.name != "AttachLight":
            continue

        t = _tn(o)
        p = {"base": o.name if o.name else "Node", "matrix": _mat4(o),
             "parent_idx": parent_idx, "emitter_idx": -1, "ps_emitter_idx": -1, "extra": None}
        if t == "NiBSParticleNode":
            p["kind"] = "PARTICLE_SYSTEM"
            ctrl = _particle_ctrl(o)
            em = getattr(ctrl, "emitter", None) if ctrl else None
            ei = emitters.get(id(em)) if em is not None else None
            if ei is not None:
                p["ps_emitter_idx"] = ei
                e = emitter_list[ei]
                ps = {}
                if e["emissive"] is not None:
                    ec = e["emissive"]
                    ps["emissive_color"] = [num(ec[0]), num(ec[1]), num(ec[2])]
                ps["birth_rate"] = num(e["birth"])
                ps["speed"] = num(e["speed"])
                ps["initial_size"] = num(e["size"])
                p["extra"] = ps
        elif t == "NiLODNode":
            p["kind"] = "LOD"
        elif t in ("NiPointLight", "NiSpotLight"):
            p["kind"] = "POINTLIGHT" if t == "NiPointLight" else "SPOTLIGHT"
            if not o.name:
                p["base"] = _INST
            d = getattr(o, "diffuse", None)
            p["extra"] = {"color": [num(d[0]), num(d[1]), num(d[2])]} if d is not None else {"color": [1, 1, 1]}
        elif id(o) in emitters:
            p["kind"] = "EMITTER"
            p["emitter_idx"] = emitters[id(o)]
        elif t in ("NiTriShape", "NiTriStrips"):
            p["kind"] = "MESH"
        else:
            p["kind"] = "EMPTY"   # NiNode and its subclasses (switch, billboard)
        proto_idx[id(o)] = len(protos)
        protos.append(p)

    if not protos:
        return None
    return ([e["base"] for e in emitter_list], protos)


def instantiate(tmpl, inst_name, counters):
    """Realise a template under a record, assigning names from the shared counter."""
    emitter_bases, protos = tmpl
    emitter_names = [seq_name(counters, b) for b in emitter_bases]
    names = [None] * len(protos)
    out = []
    for i, p in enumerate(protos):
        if p["emitter_idx"] >= 0:
            nm = emitter_names[p["emitter_idx"]]
        else:
            base = inst_name if p["base"] == _INST else p["base"]
            nm = seq_name(counters, base)
        names[i] = nm
        parent = inst_name if p["parent_idx"] < 0 else names[p["parent_idx"]]
        obj = {"name": nm, "type": p["kind"], "matrix_local": p["matrix"], "parent": parent}
        if p["kind"] == "PARTICLE_SYSTEM":
            if p["ps_emitter_idx"] >= 0:
                obj["emitter"] = emitter_names[p["ps_emitter_idx"]]
            if p["extra"] is not None:
                obj["particle_system"] = p["extra"]
        elif p["kind"] in ("POINTLIGHT", "SPOTLIGHT") and p["extra"] is not None:
            obj["light_data"] = p["extra"]
        out.append(obj)
    return out


def seq_name(counters, base):
    counters[base] = counters.get(base, 0) + 1
    c = counters[base]
    return base if c == 1 else "%s.%03d" % (base, c - 1)


def build_vfs(search_dirs):
    """Loose-mesh virtual file system: meshes-relative path (lower, /) -> full path,
    first hit wins by the same priority as plugin files."""
    vfs = {}
    for entry in search_dirs:
        mroot = os.path.join(entry, "meshes")
        if not os.path.isdir(mroot):
            continue
        for dp, _d, fns in os.walk(mroot):
            for fn in fns:
                if fn.lower().endswith(".nif"):
                    full = os.path.join(dp, fn)
                    rel = os.path.relpath(full, mroot).replace("\\", "/").lower()
                    vfs.setdefault(rel, full)
    return vfs


def mesh_has_special(full):
    try:
        with open(full, "rb") as f:
            raw = f.read()
    except OSError:
        return False
    return any(s in raw for s in _SPECIAL)


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    if len(argv) < 1:
        print("usage: build_master_records.py -- <input_folder>")
        sys.exit(2)
    root = argv[0]
    if not os.path.isdir(root):
        print(f"ERROR: input folder does not exist:\n  {root}")
        sys.exit(1)
    if not os.path.isfile(TES3CONV):
        print(f"ERROR: tes3conv.exe not found in the Shared folder:\n  {TES3CONV}")
        sys.exit(2)

    settings = load_settings()
    plugins_mode = settings.get("plugins", "enabled").strip().lower()
    if plugins_mode not in ("enabled", "all"):
        plugins_mode = "enabled"
    try:
        workers = int(settings.get("workers", "-1"))
    except ValueError:
        workers = -1
    if workers <= 0:
        workers = os.cpu_count() or 1

    apply_ltbd = settings.get("apply_ltbd", "auto").strip().lower()
    try:
        spacing = float(settings.get("spacing", "0"))
    except ValueError:
        spacing = 0.0

    mo2 = os.path.isfile(os.path.join(root, "ModOrganizer.ini"))
    missing = []
    search_dirs = []
    if mo2:
        profile, mod_count, jobs, missing, source, search_dirs = collect_mo2_active(root, plugins_mode)
        print(f"MO2 instance detected (profile '{profile}', {mod_count} enabled mods).")
        print(f"Resolved {len(jobs)} plugin(s) from {source}"
              + (f"; {len(missing)} not found on disk." if missing else "."))
        if apply_ltbd != "false":
            ltbd_path = find_ltbd(root)
            if ltbd_path:
                global LTBD_TLAD, LTBD_TST
                LTBD_TLAD, LTBD_TST = parse_ltbd(ltbd_path)
                print(f"Let There Be Darkness detected: replaying {len(LTBD_TLAD)} colour/"
                      f"radius + {len(LTBD_TST)} torch-radius override(s), nuking negatives.")
    else:
        jobs = collect_folder(root)
        search_dirs = [root]
        print(f"Folder mode: {len(jobs)} plugin(s) found.")
    if not jobs:
        print("Nothing to do.")
        return

    # tes3conv every plugin, keep results in load order
    started = time.time()
    results = [None] * len(jobs)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(run_tes3conv, job): i for i, job in enumerate(jobs)}
        for fut in as_completed(futs):
            results[futs[fut]] = fut.result()
            done += 1
            if done % 10 == 0 or done == len(jobs):
                print(f"\r  tes3conv {done}/{len(jobs)}  ({time.time() - started:.0f}s)   ",
                      end="", flush=True)
    print()
    failed = [(r[0], r[2]) for r in results if r and r[2]]

    # merge winner-takes-all by id across load order (only kept object types); also
    # note beast races (for the NPC skeleton-mesh fallback)
    winners = {}  # id_lower -> (record, source_mod)
    for name, records, err in results:
        if err:
            continue
        for rec in records:
            t = rec.get("type")
            rid = rec.get("id")
            if not rid:
                continue
            if t in KEEP_TYPES:
                winners[rid.lower()] = (rec, name)
            elif t == "Race":
                RACE_BEAST[rid.lower()] = "BEAST_RACE" in ((rec.get("data") or {}).get("flags") or "")

    # optional record filters (blank = keep everything), like Thumbnail Generator
    name_matcher = compile_matcher(settings.get("name", ""))
    type_filter = {t.strip().lower() for t in settings.get("type", "").split(",") if t.strip()}

    # build objects, sorted by id (case-sensitive, like Lua tostring(a.id)<tostring(b.id))
    built = []
    for rec, src in winners.values():
        res = build_record_object(rec, src)
        if not res:
            continue
        obj = res[0]
        if type_filter and obj.get("object_type", "").lower() not in type_filter:
            continue
        if name_matcher and not name_matcher(obj):
            continue
        built.append((rec.get("id", ""), obj))
    built.sort(key=lambda t: t[0])
    objects = [o for _id, o in built]
    if name_matcher or type_filter:
        print(f"Filters kept {len(objects)} record(s).")

    # phase 2: loose-mesh VFS + per-mesh child template cache (keyed by mesh + is_light)
    vfs, tmpl_cache = {}, {}
    if search_dirs:
        load_es3()
        print("Building loose-mesh index...")
        vfs = build_vfs(search_dirs)
        print(f"  {len(vfs)} loose .nif indexed")

    def children_of(obj, counters):
        mesh = obj.get("mesh")
        if not mesh or not vfs:
            return []
        is_light = obj.get("object_type") == "Light"
        rel = mesh.replace("\\", "/").lower()
        key = (rel, is_light)
        if key not in tmpl_cache:
            tmpl = None
            full = vfs.get(rel)
            if full and (is_light or mesh_has_special(full)):
                try:
                    stream = _ES3.NiStream()
                    stream.load(full)
                    if stream.roots:
                        tmpl = build_template(stream.roots[0], is_light)
                except Exception:
                    tmpl = None
            tmpl_cache[key] = tmpl
        tmpl = tmpl_cache[key]
        return instantiate(tmpl, obj["name"], counters) if tmpl else []

    # chunk into parts of 1000; each part gets the "master records" root first, and a
    # fresh child-naming counter (matching one exportObjectGroup call per part)
    root_obj = {"name": "master records", "type": "EMPTY", "matrix_local": IDENTITY, "parent": None}
    parts = []
    n_chunks = max(1, (len(objects) + CHUNK - 1) // CHUNK)
    # spacing > 0 lays the records out in one continuous square-ish grid instead of all
    # stacked at the origin (their children stay relative, so they move with the record).
    row_size = max(1, math.ceil(len(objects) ** 0.5)) if spacing else 1
    grid_i = 0
    started = time.time()
    child_count = 0
    for ci in range(n_chunks):
        chunk = objects[ci * CHUNK:(ci + 1) * CHUNK]
        counters = {}
        part_objs = [root_obj]
        for obj in chunk:
            # Assign the record's instance name through the shared counter (like
            # exportObjectGroup) so a mesh node sharing the record id becomes id.001
            # instead of colliding with the record -> which produced parent==name
            # self-loops that hang the emitters consumer's (guardless) BFS.
            obj["name"] = seq_name(counters, obj["object_id"])
            if spacing:
                x, y = (grid_i % row_size) * spacing, (grid_i // row_size) * spacing
                obj["matrix_local"] = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [num(x), num(y), 0, 1]]
                grid_i += 1
            part_objs.append(obj)
            kids = children_of(obj, counters)
            part_objs.extend(kids)
            child_count += len(kids)
        json_name = "master records" if n_chunks == 1 else f"master records part {ci + 1}"
        parts.append((json_name + ".json", {"json_name": json_name, "objects": part_objs}))
        print(f"\r  expanding meshes: part {ci + 1}/{n_chunks}  ({time.time() - started:.0f}s)   ",
              end="", flush=True)
    print()

    # merge order = filenames sorted as strings (merge_jsons: input_dir.glob then .sort())
    parts.sort(key=lambda p: p[0])
    output = settings.get("output", "single").strip().lower()
    pretty = settings.get("json_format", "minified").strip().lower() in ("pretty", "multiline", "indented", "readable")
    out_dir = os.path.join(SCRIPT_DIR, "output")
    os.makedirs(out_dir, exist_ok=True)
    for old in glob.glob(os.path.join(out_dir, "*.json")):   # clear the previous run's json
        try:
            os.remove(old)
        except OSError:
            pass

    def _write(obj, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(pretty_json(obj)) if pretty else json.dump(obj, f)

    if output == "split":
        # one flat file per part: [ ...objects... ]
        for fn, entry in parts:
            _write(entry["objects"], os.path.join(out_dir, fn))
        total_mb = sum(os.path.getsize(os.path.join(out_dir, fn)) for fn, _ in parts) / (1024 * 1024)
        print(f"\nWrote {len(parts)} part file(s) -> {out_dir}  ({total_mb:.1f} MB total)")
    else:
        # one file, parts wrapped: [{json_name, objects}, ...]
        out_path = os.path.join(out_dir, "master structure.json")
        _write([entry for _fn, entry in parts], out_path)
        print(f"\nWrote {out_path}  ({os.path.getsize(out_path) / (1024 * 1024):.1f} MB)")
    print(f"  records  : {len(objects)}")
    print(f"  children : {child_count}")
    print(f"  parts    : {n_chunks}")
    if failed:
        print(f"  failed  : {len(failed)} plugin(s)")
        for name, err in failed[:10]:
            print(f"     {name}: {err}")
    if missing:
        print(f"  unresolved from load order : {len(missing)}")


if __name__ == "__main__":
    main()
