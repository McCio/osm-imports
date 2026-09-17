"""dbsn-triage: interactive decision recorder for ambiguous/new conflation output."""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

with contextlib.suppress(ImportError):
    import readline  # noqa: F401

_TRIAGE_SKIP = frozenset({"ref:dbsn", "osm:id"})


def _is_conflate_meta(k: str) -> bool:
    return k.startswith("note:") or k in _TRIAGE_SKIP


def _parse_osm_features(path: Path) -> list[dict]:
    """Parse OSM XML file into a list of feature dicts with 'id' and 'tags' keys."""
    features: list[dict] = []
    tree = ET.parse(str(path))
    root = tree.getroot()
    for elem in root:
        if elem.tag not in ("node", "way", "relation"):
            continue
        fid = f"{elem.tag[0]}{elem.attrib['id']}"
        tags = {t.attrib["k"]: t.attrib["v"] for t in elem if t.tag == "tag"}
        features.append({"id": fid, "tags": tags})
    return features


def _run_conflate(province_code: str, area_arg: str | None) -> int:
    repo_root = Path(__file__).parent.parent
    cmd = ["uv", "run", "dbsn-conflate", "--province", province_code, "--overwrite", "conflate"]
    if area_arg:
        cmd += ["--area", area_arg]
    return subprocess.run(cmd, cwd=repo_root).returncode


def _base_stem_parts(stem: str) -> tuple[str, str, str | None]:
    """Return (base_stem, province_code, area_arg) from a file stem."""
    is_new = stem.endswith("_new")
    base = stem.removesuffix("_new") if is_new else stem.removesuffix("_ambiguous")
    parts = base.split("_", 2)
    return base, parts[0], (parts[2].replace("_", " ") if len(parts) > 2 else None)


def _extract_paired_osm_ids(note: str) -> list[str]:
    """Extract OSM IDs referenced in a DBSN feature's note:conflate tag."""
    ids: list[str] = []
    for part in note.split(","):
        part = part.strip()
        for prefix in ("osm=",):
            if prefix in part:
                val = part.split(prefix, 1)[1]
                ids.extend(v for v in val.split("|") if v.startswith("w") or v.startswith("r"))
    return ids


def _build_pairing(features: list[dict]) -> dict[str, list[str]]:
    """Build osm_id → [dbsn_ids] pairing from note:conflate tags."""
    pairs: dict[str, list[str]] = {}
    for feat in features:
        dbsn_id = feat["tags"].get("ref:dbsn")
        if not dbsn_id:
            continue
        note = feat["tags"].get("note:conflate", "")
        for osm_id in _extract_paired_osm_ids(note):
            pairs.setdefault(osm_id, []).append(dbsn_id)
    return pairs


def _fmt_tags(tags: dict) -> str:
    skip = frozenset({"osm:id", "ref:dbsn", "note:conflate", "note:conflate:geometry",
                      "note:conflate:shared_node_ids"})
    parts = [f"{k}={v}" for k, v in tags.items()
             if k not in skip and not k.startswith("note:conflate:")]
    return ", ".join(parts) or "(no tags)"


def _fmt_resolution(rec: dict | None) -> str:
    if not rec:
        return "none"
    parts = [f"decision={rec.get('decision', '?')}"]
    if dbsn := rec.get("dbsn_classid"):
        parts.append(f"dbsn={dbsn}")
    if tags := rec.get("tags"):
        parts.append(f"tags={_fmt_tags(tags)}")
    return ", ".join(parts)



def _interactive_main(ambiguous_path: Path, resolutions_path: Path,
                      province_code: str, area_arg: str | None) -> None:
    dir_ = ambiguous_path.parent
    stem = ambiguous_path.stem
    for suf in ("_ambiguous", "_osm_only", "_new", "_conflated"):
        stem = stem.removesuffix(suf)
    base = stem
    new_path = dir_ / f"{base}_new.osm"
    osm_only_path = dir_ / f"{base}_osm_only.osm"
    conflated_path = dir_ / f"{base}_conflated.osm"

    ambiguous = _parse_osm_features(ambiguous_path) if ambiguous_path.exists() else []
    new_features = _parse_osm_features(new_path) if new_path.exists() else []
    osm_only_features = _parse_osm_features(osm_only_path) if osm_only_path.exists() else []
    conflated_features = _parse_osm_features(conflated_path) if conflated_path.exists() else []

    # Indexes
    osm_by_id: dict[str, dict] = {
        f["tags"]["osm:id"]: f for f in ambiguous if "osm:id" in f["tags"]
    }
    # osm_only features: extract ID from note:conflate=osm_id=wXXX
    osm_only_by_id: dict[str, dict] = {
        f["tags"]["osm:id"]: f for f in osm_only_features if "osm:id" in f["tags"]
    }
    # Merge so a single lookup covers both ambiguous and osm_only OSM features
    all_osm_by_id = {**osm_only_by_id, **osm_by_id}  # ambiguous takes priority on collision

    dbsn_amb_by_id: dict[str, dict] = {
        f["tags"]["ref:dbsn"]: f for f in ambiguous if "ref:dbsn" in f["tags"]
    }
    dbsn_new_by_id: dict[str, dict] = {
        f["tags"]["ref:dbsn"]: f for f in new_features if "ref:dbsn" in f["tags"]
    }
    dbsn_conflated_by_id: dict[str, dict] = {
        f["tags"]["ref:dbsn"]: f for f in conflated_features
        if "ref:dbsn" in f["tags"] and "osm:id" not in f["tags"]
    }
    # Reverse: paired OSM ID → DBSN ID, for looking up conflated pairs by OSM ID
    conflated_osm_to_dbsn: dict[str, str] = {}
    for dbsn_id, feat in dbsn_conflated_by_id.items():
        for oid in _extract_paired_osm_ids(feat["tags"].get("note:conflate", "")):
            conflated_osm_to_dbsn[oid] = dbsn_id
    osm_to_dbsn = _build_pairing(ambiguous)

    existing: dict = {}
    if resolutions_path.exists():
        existing = json.loads(resolutions_path.read_text(encoding="utf-8"))

    pending: dict = {}  # decisions made this session, not yet saved

    print(
        f"\n  Loaded: {len(ambiguous)} ambiguous, {len(new_features)} new,"
        f" {len(osm_only_features)} osm_only features"
    )
    print(f"  Existing resolutions: {len(existing)}")
    print("  Commands: osm:wXXX | dbsn:<classid> | save | quit | ?\n")

    def _current(key: str) -> dict | None:
        return pending.get(key) or existing.get(key)

    def _tag_update(osm_id: str, feat: dict, paired_dbsn: list | None = None) -> None:
        cur = _current(osm_id)
        if cur and cur.get("decision") == "tag_update":
            base_tags = dict(cur.get("tags", {}))
        else:
            osm_tags = {k: v for k, v in feat["tags"].items() if not _is_conflate_meta(k)}
            dbsn_feat = dbsn_amb_by_id.get(paired_dbsn[0]) if paired_dbsn else None
            if dbsn_feat:
                dbsn_tags = {k: v for k, v in dbsn_feat["tags"].items() if not _is_conflate_meta(k)}
                base_tags = {**osm_tags, **dbsn_tags}
            else:
                base_tags = osm_tags
        def _fmt_edits(d: dict) -> str:
            parts = []
            for k, v in d.items():
                parts.append(f"[-{k[1:]}]" if k.startswith("-") else f"{k}={v}")
            return ", ".join(parts) if parts else "(none)"

        print(f"  Current tags: {_fmt_edits(base_tags)}")
        print("  Enter changes (key=value to set/override, -key to delete, blank to finish):")
        edits = dict(base_tags)
        while True:
            try:
                line = input("    ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                break
            if line.startswith("-"):
                key = line[1:].strip()
                edits.pop(key, None)      # remove any positive entry
                edits[f"-{key}"] = ""     # record deletion
            elif "=" in line:
                k, _, v = line.partition("=")
                k = k.strip()
                edits.pop(f"-{k}", None)      # cancel deletion marker if re-adding the key
                edits[k] = v.strip()
        print(f"  New tags: {_fmt_edits(edits)}")
        try:
            confirm = input("  Confirm? [y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            confirm = "n"
        if confirm == "y":
            rec: dict = {"decision": "tag_update", "tags": edits}
            if paired_dbsn:
                rec["dbsn_classid"] = paired_dbsn[0]
            pending[osm_id] = rec
            print("  → tag_update recorded (pending)")

    def _decide_for_osm(osm_id: str, osm_feat: dict, in_ambiguous: bool, paired_dbsn: list) -> None:
        cur = _current(osm_id)
        kind = "ambiguous" if in_ambiguous else "osm_only"
        print(f"\n  OSM {osm_id} ({kind})")
        print(f"    tags:      {_fmt_tags(osm_feat['tags'])}")
        note = osm_feat["tags"].get("note:conflate", "")
        if note:
            print(f"    conflate:  {note}")
        sids = osm_feat["tags"].get("note:conflate:shared_node_ids", "")
        if sids:
            print(f"    shared nodes: {sids}")
        for did in paired_dbsn:
            p = dbsn_amb_by_id.get(did)
            if p:
                print(f"    paired DBSN {did}: {_fmt_tags(p['tags'])}")
        print(f"    current resolution: {_fmt_resolution(cur)}")
        print()
        if in_ambiguous:
            print("    [1] keep_osm    keep OSM as-is; DBSN not imported (resolved)")
            print("    [b] keep both   keep OSM as-is; also import DBSN as new feature")
            print("    [2] replace     use DBSN geometry (new nodes)")
        if _current(osm_id):
            print("    [t] add tags    set tag overrides on the current decision")
        print("    [3] delete      delete this OSM feature")
        print("    [4] tag_update  specify tags manually")
        is_contained = "contained_by" in osm_feat["tags"].get("note:conflate", "")
        b_val = osm_feat["tags"].get("building", "yes")
        part_val = b_val if b_val != "yes" else "yes"
        if in_ambiguous and is_contained:
            print(f"    [5] make part   tag as building:part={part_val} within DBSN outline")
        print("    [c] cancel")
        try:
            choice = input("    choice: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice == "1":
            if not in_ambiguous:
                print("  Not available for osm_only features.")
            else:
                rec: dict = {"decision": "keep_osm"}
                if paired_dbsn:
                    rec["dbsn_classid"] = paired_dbsn[0]
                pending[osm_id] = rec
                print("  → keep_osm recorded (pending)")
        elif choice == "b":
            if not in_ambiguous:
                print("  Not available for osm_only features.")
            else:
                # keep_osm without dbsn_classid → DBSN not resolved, will be imported as new
                pending[osm_id] = {"decision": "keep_osm"}
                print("  → keep_osm recorded; DBSN will be imported as new feature (pending)")
        elif choice == "2":
            if not in_ambiguous:
                print("  Not available for osm_only features.")
            else:
                dbsn_cid = paired_dbsn[0] if paired_dbsn else None
                if not dbsn_cid:
                    print("  No paired DBSN feature found — cannot replace.")
                else:
                    pending[osm_id] = {"decision": "replace", "dbsn_classid": dbsn_cid}
                    print(f"  → replace with {dbsn_cid} (pending)")
        elif choice == "t":
            cur_rec = dict(_current(osm_id) or {})
            print("  Tag overrides (applied on top of output tags). key=value or -key to delete, blank to finish:")
            overrides: dict = dict(cur_rec.get("tags", {}))
            while True:
                try:
                    line = input("    ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not line:
                    break
                if line.startswith("-"):
                    k = line[1:].strip()
                    overrides.pop(k, None)
                    overrides[f"-{k}"] = ""
                elif "=" in line:
                    k, _, v = line.partition("=")
                    k = k.strip()
                    overrides.pop(f"-{k}", None)
                    overrides[k] = v.strip()
            cur_rec["tags"] = overrides
            pending[osm_id] = cur_rec
            print(f"  → tags recorded on {cur_rec.get('decision', '(no decision yet)')} (pending)")
        elif choice == "3":
            pending[osm_id] = {"decision": "delete"}
            print("  → delete recorded (pending)")
        elif choice == "4":
            _tag_update(osm_id, osm_feat, paired_dbsn if in_ambiguous else None)
        elif choice == "5":
            if not in_ambiguous or not is_contained:
                print("  Not available here.")
            else:
                tag_overrides = {"-building": "", "building:part": part_val}
                rec = {"decision": "keep_osm", "tags": tag_overrides}
                if paired_dbsn:
                    rec["dbsn_classid"] = paired_dbsn[0]
                pending[osm_id] = rec
                print(f"  → keep_osm as building:part={part_val} recorded (pending)")
        elif choice not in ("c", ""):
            print("  Invalid choice.")
        print()

    def _save() -> None:
        merged = {k: v for k, v in {**existing, **pending}.items() if v is not None}
        new_c = sum(1 for k, v in pending.items() if v is not None and k not in existing)
        rev_c = sum(1 for k, v in pending.items() if v is not None and k in existing and existing[k] != v)
        rem_c = sum(1 for k, v in pending.items() if v is None and k in existing)
        resolutions_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n  [triage ] {province_code}: {new_c} new, {rev_c} revised, {rem_c} removed → {resolutions_path.name}")
        rc = _run_conflate(province_code, area_arg)
        if rc != 0:
            print(f"  [warn   ] {province_code}: conflate re-run failed", file=sys.stderr)
        pending.clear()
        existing.update(merged)

    while True:
        try:
            raw = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not raw:
            continue

        if raw in ("?", "help"):
            print("  osm:wXXX     look up OSM feature by original ID")
            print("  dbsn:<classid> look up DBSN feature by classid (ambiguous or new)")
            print("  save         write resolutions and re-run conflate")
            print("  quit         exit (asks to save if pending)")
            continue

        if raw in ("save", "s"):
            if not pending:
                print("  (nothing pending)")
            else:
                _save()
                break
            continue

        if raw in ("quit", "q"):
            if pending:
                try:
                    ans = input(f"  {len(pending)} unsaved decision(s). Save before quit? [y/n] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    ans = "n"
                if ans == "y":
                    _save()
            break

        # Normalise ID
        # Normalise separators (colon or equals) and strip braces from UUIDs
        if raw.startswith("osm:") or raw.startswith("osm="):
            fid = f"osm:{raw[4:].strip()}"
        elif raw.startswith("dbsn:") or raw.startswith("dbsn="):
            fid = f"dbsn:{raw[5:].strip().strip('{}')}"
        elif raw[:1] in ("w", "r") and raw[1:2].isdigit():
            fid = f"osm:{raw}"
        else:
            fid = f"dbsn:{raw.strip('{}')}"

        # Batch paste: "ref:dbsn=UUID\nref:dbsn=UUID\n..." (e.g. from JOSM tag copy)
        # Terminal paste buffers all lines; subsequent input() calls drain them automatically.
        if "=" in raw and raw.split("=", 1)[0].strip() == "ref:dbsn":
            batch_raws = [raw]
            while True:
                try:
                    nxt = input("  > ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not nxt:
                    break
                if "=" in nxt and nxt.split("=", 1)[0].strip() == "ref:dbsn":
                    batch_raws.append(nxt)
                else:
                    print(f"  [batch] unexpected line '{nxt}', stopping")
                    break

            batch_dbsn_ids = [br.split("=", 1)[1].strip().strip("{}") for br in batch_raws]
            new_ids   = [bid for bid in batch_dbsn_ids if bid in dbsn_new_by_id]
            amb_ids   = [bid for bid in batch_dbsn_ids if bid in dbsn_amb_by_id]
            miss_ids  = [bid for bid in batch_dbsn_ids if bid not in dbsn_new_by_id and bid not in dbsn_amb_by_id]

            print(f"\n  Batch: {len(batch_dbsn_ids)} IDs — {len(new_ids)} new, {len(amb_ids)} ambiguous" +
                  (f", {len(miss_ids)} not found" if miss_ids else ""))

            # Build menu from what's present
            opts: list[str] = []
            if new_ids:
                opts.append("1")
                print("    [1] skip        prevent these buildings from being created")
                opts.append("t")
                print("    [t] add tags    set tag overrides on import (for all new)")
            if amb_ids:
                # Ambiguous: decisions apply to their paired OSM IDs
                opts += ["2", "3", "4"]
                print("    [2] keep_osm    keep OSM as-is; DBSN not imported")
                print("    [3] replace     use DBSN geometry for all paired OSM features")
                print("    [4] delete      delete all paired OSM features")
            print("    [c] cancel")

            try:
                bchoice = input("    choice: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                continue

            if bchoice not in opts:
                if bchoice not in ("c", ""):
                    print("  Invalid choice.")
                print()
                continue

            if bchoice == "1":
                for bid in new_ids:
                    pending[f"dbsn:{bid}"] = {"decision": "skip"}
                print(f"  → skip recorded for {len(new_ids)} features (pending)")

            elif bchoice == "t":
                print("  Tag overrides (applied on import for all new). key=value or -key to delete, blank to finish:")
                overrides: dict = {}
                while True:
                    try:
                        line = input("    ").strip()
                    except (EOFError, KeyboardInterrupt):
                        break
                    if not line:
                        break
                    if line.startswith("-"):
                        k = line[1:].strip()
                        overrides.pop(k, None)
                        overrides[f"-{k}"] = ""
                    elif "=" in line:
                        k, _, v = line.partition("=")
                        k = k.strip()
                        overrides.pop(f"-{k}", None)
                        overrides[k] = v.strip()
                for bid in new_ids:
                    key = f"dbsn:{bid}"
                    cur_rec = dict(_current(key) or {})
                    cur_rec["tags"] = overrides
                    pending[key] = cur_rec
                print(f"  → tags recorded for {len(new_ids)} features (pending)")

            elif bchoice in ("2", "3", "4"):
                decisions = {"2": "keep_osm", "3": "replace", "4": "delete"}
                dec = decisions[bchoice]
                applied = 0
                for bid in amb_ids:
                    paired = [oid for oid, dids in osm_to_dbsn.items() if bid in dids]
                    if not paired:
                        continue
                    osm_id = paired[0]
                    rec: dict = {"decision": dec}
                    if dec in ("keep_osm", "replace"):
                        rec["dbsn_classid"] = bid
                    pending[osm_id] = rec
                    applied += 1
                print(f"  → {dec} recorded for {applied} OSM features (pending)")

            print()
            continue

        if fid.startswith("osm:"):
            osm_id = fid[4:]
            feat = all_osm_by_id.get(osm_id)
            if not feat and osm_id in conflated_osm_to_dbsn:
                # OSM feature was auto-conflated — redirect to the conflated DBSN handler
                fid = f"dbsn:{conflated_osm_to_dbsn[osm_id]}"
                # fall through to dbsn: branch by re-processing this iteration
                raw = fid
                # push back and re-parse
                if fid.startswith("dbsn:"):
                    dbsn_id = fid[5:]
                    feat = dbsn_conflated_by_id.get(dbsn_id)
                    if feat:
                        note = feat["tags"].get("note:conflate", "")
                        paired_osm_ids = _extract_paired_osm_ids(note)
                        print(f"\n  DBSN {dbsn_id} (conflated, paired with {osm_id})")
                        print(f"    tags:    {_fmt_tags(feat['tags'])}")
                        print(f"    current resolution: {_fmt_resolution(_current(osm_id))}")
                        print()
                        print("    [1] keep_osm    override: keep OSM geometry + tags, skip DBSN geometry")
                        print("    [c] cancel")
                        try:
                            c = input("    choice: ").strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            print()
                            continue
                        if c == "1":
                            pending[osm_id] = {"decision": "keep_osm", "dbsn_classid": dbsn_id}
                            print(f"  → keep_osm recorded for {osm_id} (pending)")
                        print()
                continue
            if not feat:
                cur = _current(osm_id)
                if cur:
                    decision = cur.get("decision", "?")
                    tags = cur.get("tags", {})
                    dbsn_cid = cur.get("dbsn_classid")
                    print(f"\n  OSM {osm_id} (resolved — not in current ambiguous)")
                    print(f"    decision:  {decision}")
                    if dbsn_cid:
                        print(f"    dbsn:      {dbsn_cid}")
                        dbsn_feat = dbsn_conflated_by_id.get(dbsn_cid) or dbsn_amb_by_id.get(dbsn_cid)
                        if dbsn_feat:
                            print(f"    dbsn tags: {_fmt_tags(dbsn_feat['tags'])}")
                    if tags:
                        print(f"    tags:      {_fmt_tags(tags)}")
                    print()
                    print("    [1] keep_osm    keep OSM geometry")
                    print("    [2] replace     use DBSN geometry")
                    print("    [t] add tags    set tag overrides on the current decision")
                    print("    [3] delete      delete this OSM feature")
                    print("    [4] tag_update  specify tags manually")
                    print("    [x] remove      delete this resolution entirely")
                    print("    [c] cancel")
                    try:
                        choice = input("    choice: ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        continue
                    if choice == "x":
                        pending[osm_id] = None  # sentinel: delete on save
                        print("  → resolution removal pending (save to apply)")
                    elif choice == "t":
                        cur_rec = dict(cur)
                        print("  Tag overrides (applied on top of output tags)."
                              " key=value or -key to delete, blank to finish:")
                        overrides: dict = dict(cur_rec.get("tags", {}))
                        while True:
                            try:
                                line = input("    ").strip()
                            except (EOFError, KeyboardInterrupt):
                                break
                            if not line:
                                break
                            if line.startswith("-"):
                                k = line[1:].strip()
                                overrides.pop(k, None)
                                overrides[f"-{k}"] = ""
                            elif "=" in line:
                                k, _, v = line.partition("=")
                                k = k.strip()
                                overrides.pop(f"-{k}", None)
                                overrides[k] = v.strip()
                        cur_rec["tags"] = overrides
                        pending[osm_id] = cur_rec
                        print(f"  → tags recorded on {cur_rec.get('decision', '?')} (pending)")
                    elif choice in ("1", "2", "3", "4"):
                        print("  (no live feature data — use full tags manually)")
                        if choice == "4":
                            _tag_update(osm_id, {"tags": cur.get("tags", {})})
                        elif choice == "2":
                            if dbsn_cid:
                                rec: dict = {"decision": "replace", "dbsn_classid": dbsn_cid}
                                inherited = cur.get("tags")
                                if inherited:
                                    rec["tags"] = inherited
                                pending[osm_id] = rec
                                print(f"  → replace with {dbsn_cid} recorded (pending)")
                            else:
                                print("  → cannot replace: no paired DBSN classid in existing resolution")
                        elif choice in ("1", "3"):
                            decisions = {"1": "keep_osm", "3": "delete"}
                            rec = {"decision": decisions[choice]}
                            if dbsn_cid and choice == "1":
                                rec["dbsn_classid"] = dbsn_cid
                            pending[osm_id] = rec
                            print(f"  → {decisions[choice]} recorded (pending)")
                    print()
                else:
                    print(f"  Not found: {osm_id}")
                continue
            in_ambiguous = osm_id in osm_by_id
            paired_dbsn = osm_to_dbsn.get(osm_id, [])
            _decide_for_osm(osm_id, feat, in_ambiguous, paired_dbsn)

        elif fid.startswith("dbsn:"):
            dbsn_id = fid[5:]
            feat = dbsn_new_by_id.get(dbsn_id) or dbsn_amb_by_id.get(dbsn_id)
            if not feat:
                conflated_feat = dbsn_conflated_by_id.get(dbsn_id)
                if conflated_feat:
                    note = conflated_feat["tags"].get("note:conflate", "")
                    paired_osm_ids = _extract_paired_osm_ids(note)
                    print(f"\n  DBSN {dbsn_id} (conflated)")
                    print(f"    tags:    {_fmt_tags(conflated_feat['tags'])}")
                    if paired_osm_ids:
                        print(f"    paired OSM: {paired_osm_ids[0]}")
                        print(f"    current resolution: {_fmt_resolution(_current(paired_osm_ids[0]))}")
                    print()
                    print("    [1] keep_osm    override: keep OSM geometry + tags, skip DBSN geometry")
                    print("    [c] cancel")
                    try:
                        c = input("    choice: ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        continue
                    if c == "1" and paired_osm_ids:
                        pending[paired_osm_ids[0]] = {"decision": "keep_osm", "dbsn_classid": dbsn_id}
                        print(f"  → keep_osm recorded for {paired_osm_ids[0]} (pending)")
                    print()
                else:
                    print(f"  Not found: {dbsn_id}")
                continue

            is_new_feat = dbsn_id in dbsn_new_by_id
            kind = "new" if is_new_feat else "ambiguous"
            print(f"\n  DBSN {dbsn_id} ({kind})")
            print(f"    tags:    {_fmt_tags(feat['tags'])}")

            if not is_new_feat:
                paired_osm = [oid for oid, dids in osm_to_dbsn.items() if dbsn_id in dids]
                if paired_osm:
                    osm_id = paired_osm[0]
                    print(f"    paired OSM: {osm_id}\n")
                    osm_feat = all_osm_by_id.get(osm_id)
                    if not osm_feat:
                        print(f"  Paired OSM feature {osm_id} not in index.\n")
                        continue
                    _decide_for_osm(osm_id, osm_feat, True, [dbsn_id])
                else:
                    print("  No paired OSM feature found in ambiguous index.\n")
                continue

            # New DBSN feature: skip or tag overrides
            key = f"dbsn:{dbsn_id}"
            cur = _current(key)
            print(f"    current resolution: {_fmt_resolution(cur)}")
            print()
            print("    [1] skip   prevent this building from being created")
            print("    [t] add tags    set tag overrides on import")
            print("    [c] cancel")

            try:
                choice = input("    choice: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                continue

            if choice == "1":
                pending[key] = {"decision": "skip"}
                print("  → skip recorded (pending)")
            elif choice == "t":
                cur_rec = dict(cur or {})
                print("  Tag overrides (applied on import). key=value or -key to delete, blank to finish:")
                overrides: dict = dict(cur_rec.get("tags", {}))
                while True:
                    try:
                        line = input("    ").strip()
                    except (EOFError, KeyboardInterrupt):
                        break
                    if not line:
                        break
                    if line.startswith("-"):
                        k = line[1:].strip()
                        overrides.pop(k, None)
                        overrides[f"-{k}"] = ""
                    elif "=" in line:
                        k, _, v = line.partition("=")
                        k = k.strip()
                        overrides.pop(f"-{k}", None)
                        overrides[k] = v.strip()
                cur_rec["tags"] = overrides
                pending[key] = cur_rec
                print(f"  → tags recorded on {cur_rec.get('decision', '(no decision yet)')} (pending)")
            elif choice not in ("c", ""):
                print("  Invalid choice.")
            print()

        else:
            print(f"  Unknown ID format: {raw}. Use osm:wXXX or dbsn:<classid>")

    if pending:
        print(f"  {len(pending)} unsaved decision(s) discarded.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Triage DBSN conflation decisions (interactive)")
    parser.add_argument("source", help="Path to _ambiguous.osm, _new.osm or _osm_only.osm")
    args = parser.parse_args()

    source_path = Path(args.source)
    if not source_path.exists():
        print(f"Error: {source_path} not found", file=sys.stderr)
        sys.exit(1)

    base_stem, province_code, area_arg = _base_stem_parts(source_path.stem)
    resolutions_path = source_path.with_name(f"{base_stem}_resolutions.json")

    _interactive_main(source_path, resolutions_path, province_code, area_arg)


if __name__ == "__main__":
    main()
