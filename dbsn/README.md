# DBSN buildings import

Converts [DBSN](https://igmi.esercito.difesa.it/servizi/database-di-sintesi-nazionale/) building data into OSM XML for the [DBSN buildings import](https://wiki.openstreetmap.org/wiki/Import/Catalogue/DBSN/Buildings), and conflates it against existing OSM data.

Releases are tagged `dbsn-<date>` (e.g. `dbsn-2025-07-28`) and hold every layer. Download the `.osm.bz2` for your province and load it into JOSM. To decompress: `bzip2 -d file.osm.bz2`. To produce your own files or to conflate, read on. Developer documentation (module map, adding layers, tag overrides) is in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Prerequisites

```bash
uv sync
```

No system GDAL required (`fiona` ships bundled GDAL wheels).

`osmium-tool` is required for `dbsn-validate` and `dbsn-conflate`:

macOS:
```bash
brew install osmium-tool
```

Ubuntu / Debian:
```bash
sudo apt install osmium-tool
```

## Commands

All commands accept `--province CODE|NAME|REGION|all` (comma-separated for multiple).

| Command | Description |
|---|---|
| `uv run dbsn-discover`   | Fetch latest DBSN links from IGM, write `data/dbsn/sources.json` |
| `uv run dbsn-neighbours` | Populate province adjacency and bounding boxes in `sources.json` from OSM (run once) |
| `uv run dbsn-download --province MI` | Download province ZIP archives (also pre-fetches neighbour ZIPs for cross-province extension) |
| `uv run dbsn-download --province MI --no-neighbours` | Download only the requested province, skip neighbour pre-fetch |
| `uv run dbsn-download --province VE --area Marghera` | Pre-fetch only the neighbours that touch the area |
| `uv run dbsn-extract  --province MI` | Extract the selected layers from GDB to FlatGeobuf, extend cross-province buildings |
| `uv run dbsn-extract  --province MI --no-extend` | Extract only, skip cross-province extension |
| `uv run dbsn-convert  --province MI` | Convert FlatGeobuf to OSM XML |
| `uv run dbsn-convert  --province MI --format geojson` | Convert to GeoJSON instead |
| `uv run dbsn-convert  --province MI --compress` | Produce `.osm.bz2` (~10× smaller; JOSM accepts it natively) |
| `uv run dbsn-convert  --province VE --area Marghera` | Convert only features intersecting the named `loc_sg` locality |
| `uv run dbsn-convert  --province VE --layers edifc,attr_sp` | Convert these layers instead of the configured default |
| `uv run dbsn-validate --province MI` | Validate OSM/OSM.BZ2 files with `osmium check-refs` |
| `uv run dbsn-all      --province all` | Run all steps in sequence |
| `uv run dbsn-all      --province all --neighbours` | Force re-run of the neighbours step |
| `uv run dbsn-all      --province all --no-extend` | Run pipeline but skip cross-province extension |
| `uv run dbsn-all      --province all --neighbours --overpass-url URL` | Force neighbours step using a custom Overpass endpoint (default `https://overpass-api.de`) |
| `uv run dbsn-conflate --province VE --area Marghera` | Conflate against OSM, see [Conflation](#conflation) |

Add `--overwrite` to reprocess existing output files.

`--layers` and `--area` behave the same in every command that accepts them, and are passed on to the steps a command runs (`dbsn-all` and `dbsn-conflate` run extract and convert on their own):

| Option | `download` | `extract` | `convert` | `validate` | `all` | `conflate` |
|---|---|---|---|---|---|---|
| `--layers` | | yes | yes | yes | yes | yes |
| `--area` | neighbour pre-fetch | neighbour extension | yes | yes | yes | yes |

## Pipeline steps

```
discover → neighbours → download → extract (+extend) → convert → validate
```

`dbsn-all` skips `discover` if `sources.json` already exists and a specific province is
requested. It skips `neighbours` if every province in `sources.json` already has a `neighbours`
and a `bbox` field. Pass `--neighbours` to force re-running the adjacency step.

## Layers

A DBSN province archive holds several layers. Only the main buildings layer is processed by default; the minor ones are opt-in, because removing them from a finished import (swimming pools, sports pitches, sheds) is far more work than adding them.

| Layer | Contents | Default |
|---|---|---|
| `edifc` | Buildings | yes |
| `edi_min` | Minor buildings: sheds, kiosks, garages, tombs, roofs | no |
| `mn_ind` | Industrial structures (substations, other) | no |
| `mn_mau` | Monuments and street furniture | no |
| `sc_dis` | Excavation and dump areas (landfill, quarry) | no |
| `attr_sp` | Sports facilities: swimming pools, pitches, tracks | no |
| `ponte`, `galler` | Bridges and tunnels as areas | no |

Choose the layers in this order, the first that applies wins:

1. `--layers a,b` on the command line (`--layers all` selects every layer);
2. `dbsn/layers.yaml`, most specific first: the area (keyed by area slug, e.g. `marghera`), the province code, the `region` of `sources.json`, then `defaults`;
3. `edifc` only.

Put a layer choice you repeat in `dbsn/layers.yaml`, so the command stays `dbsn-conflate --province VE --area Marghera`:

```yaml
defaults: [edifc]
areas:
  marghera: [edifc, attr_sp]
```

A list replaces the less specific one, it is not merged. An unknown layer name is an error.

The layer set is part of the output names: `VE_2025-07-28_edifc.osm`, `VE_2025-07-28_marghera_edifc+attr_sp.osm`, and the conflation directories and files likewise. A run with every layer keeps the plain name (`VE_2025-07-28.osm`, the release name), so outputs of different layer sets never shadow each other. Output made before the layers existed holds every layer: to carry on with it, pass `--layers all`.

## Working on one area

`--area LOCALITY` (a `loc_sg` locality, e.g. `Marghera`) limits convert and conflation to that locality. It also limits the cross-province work to the neighbours that can matter:

- `dbsn-download`, `dbsn-extract` and every task that extends buildings across the boundary only handle the neighbours whose bounding box lies within about 1 km of the area (`bbox` in `sources.json`, written by `dbsn-neighbours`). Extracting a neighbour needs its whole ZIP, so skipping the far ones saves the download and the memory.
- A neighbour without a `bbox` is always included. Run `uv run dbsn-neighbours` once to fill it in.
- Extension files are always complete for their province pair, so a later run without `--area` reuses them.

## Cross-province buildings

Buildings that straddle a province boundary appear as non-overlapping fragments in each
province's source GDB. The `extract` step detects these via a shared `classid` and unions the
fragments using the adjacent province's data, writing the complete geometry into **both**
provinces' outputs. The full building is therefore present in every province it touches.

Extension happens automatically unless `--no-extend` is passed. To re-extend without
re-extracting (e.g. after updating `neighbours` in `sources.json`):

```bash
rm data/dbsn/buildings/*_*_*.fgb   # delete ext files only
uv run dbsn-extract --province MI  # raw FGB exists → skips extraction, runs extension only
```

## Data layout

```
data/dbsn/
  sources.json           # canonical per-province source list with adjacency (committed)
  zips/                  # downloaded province ZIPs (kept; re-extract without re-download)
  buildings/
    {CODE}_{date}_{layer}.fgb   # per-province raw FlatGeobuf, one per layer
    {C1}_{C2}_{C1date}_{C2date}_{layer}.fgb   # cross-province ext file (two code segments, alphabetical)
  osm/                   # per-province OSM XML (or .osm.bz2 / .geojson); the stem is {CODE}_{date}[_{area}][_{layers}]
  conflate/              # conflation outputs, see below
```

Ext files contain only the cross-boundary buildings as complete (unioned) geometries.
`convert` reads them and overrides the raw fragments by `classid`. They are small and
cheap to regenerate; delete them and re-run `extract` to rebuild.

`data/dbsn/unzipped/` is created during extract and deleted automatically once the
FlatGeobuf is written.

### `sources.json` format

```json
{
  "PD": {
    "province": "Padova",
    "region": "Veneto",
    "date": "2025-07-28",
    "igm_url": "...",
    "wmit_url": "...",
    "tsv_date": "2025-07-28",
    "status": "ok",
    "zip_size": 123456789,
    "neighbours": ["RO", "TV", "VE", "VI", "VR"],
    "bbox": [11.3, 44.9, 12.3, 45.7]
  }
}
```

`neighbours` and `bbox` (`[min lon, min lat, max lon, max lat]` of the province boundary) are populated by `dbsn-neighbours` and committed. `neighbours` lists the codes of all provinces that share a boundary, used by `extract` to locate cross-province buildings; `bbox` lets `--area` runs skip the neighbours that are too far to matter.

## Conflation

The conflation step matches DBSN building footprints against existing OSM features in the same area, produces a change file ready for upload, and flags cases that need human review.

### Running

```bash
uv run dbsn-conflate --province VE --area Marghera
```

The PBF for the province is downloaded automatically if not already present. Output goes to `data/dbsn/conflate/{CODE}_{date}_{area}[_{layers}][_osm-{osm-area}]/`.

Useful flags:

| Flag | Effect |
|---|---|
| `--layers a,b\|all` | DBSN layers to conflate, see [Layers](#layers). Default: `dbsn/layers.yaml`, else `edifc` only. |
| `--osm-area NAME\|r/ID\|w/ID` | Further clip to an OSM-side area — an admin relation or named area (relation or closed way) read from the province PBF, by name (`Isola dei Petroli`) or id (`r/6628636`, `w/107637324`). Intersected with `--area` when both given; outputs get an `_osm-` slug so they never shadow unslugged runs. Match is exact + case-insensitive; needs the PBF already downloaded. Only the clipped part of the PBF is read in memory (cached next to the outputs as `*.clip.osm.pbf`, rebuilt when the PBF changes). |
| `--josm [HOST:PORT]` | After conflation, open the PBF (reference layer) plus the `ambiguous`, `new`, `osm-only` and `conflated` review files in JOSM via remote control (default `localhost:8111`). Enable remote control in JOSM preferences first. Without `--josm` or `--no-josm`, this happens automatically when JOSM answers on the default host, only for provinces conflated in this run (an explicit `--josm` opens every selected province). Review files without data are not opened (JOSM rejects an empty layer). Each other review file opens as an editable copy `<file>_edited.osm` (a copy with saved edits is never overwritten; a copy nobody saved is replaced with a fresh one after each conflation run), bound to its file so Cmd+S saves in place; upload stays disabled (`upload="never"`). Each run adds fresh layers — close stale ones manually (JOSM has no delete-layer remote call). |
| `--no-josm` | Never open layers in JOSM, even when it is running. |
| `--overwrite conflate` / `--overwrite pbf` | Re-run match logic on cached PBF / re-download the PBF |
| `--review-identical` | Keep zero-delta matches (IoU≥`identical_iou`, no tag changes) in `_conflated.osm` |
| `--apply-edits` / `--no-apply-edits` | Default on. Before regenerating, absorb edits saved in `<file>_edited.osm` into `_resolutions.json` (`_new` and `_conflated` for now), then replace the `_edited` copy with a fresh one (every `_edited` copy without saved edits is refreshed after each run). Unsaved JOSM edits are not on disk and cannot be absorbed. `--overwrite conflate` (or bare `--overwrite`) starts from scratch instead: every `_edited` copy is deleted without absorbing, saved edits included, and `--josm` then opens fresh copies. |

### Output files

| File | Contents |
|---|---|
| `_changes.osc` | OSC change file: `create` for new buildings, `modify` for tag and geometry updates. Deletes are only emitted when a triage decision explicitly requests one (see below). A way's `source` tag is kept only where the OSM geometry is kept; geometry that comes from DBSN (created, replaced or hand edited) carries none, the changeset does. |
| `_changeset.txt` | Changeset tags to paste into the upload dialog (an osmChange file cannot carry them): `comment=` computed from the counts (`Added 55 features, updated 15 features`; with resolutions: `Reviewed import: added 55 features, updated 15 features (3 skipped, 15 reshaped)`, counting skipped, reshaped, deleted and kept OSM) and `source=`, the DBSN `source` values of every created or geometry-modified feature, `;`-joined without duplicates, the bare `Regione` named after the province's region (`Regione Veneto`). |
| `_new.osm` | DBSN features with no OSM match — will be created on upload |
| `_ambiguous.osm` | Feature pairs that need human review (see below) |
| `_osm_only.osm` | OSM features with no DBSN match (kept for reference; not in the change file) |
| `_resolutions.json` | Append-only decision log written by `dbsn-triage` |

### What goes to `_ambiguous.osm`

- **Near-miss pairs**: IoU below the match threshold but above `near_miss_iou` — geometrically close but not confident enough to auto-match.
- **N:M pairs**: multiple DBSN features match multiple OSM features (complex overlap).
- **Shared-node cases**: the matched OSM way shares nodes with adjacent ways — replacing its geometry would break connectivity.

Every OSM feature in `_ambiguous.osm` carries `osm:id=<original_osm_id>` (e.g. `osm:id=w138864508`) so decisions survive the file-level ID renumbering. Every DBSN feature carries `ref:dbsn=<classid>`.

Shared-node cases also carry:
- `note:conflate` — `osm_ref=<id>,shared_nodes:<count>` summary
- `note:conflate:shared_node_ids` — comma-separated node IDs that are shared

### Reviewing `_ambiguous.osm` in JOSM

1. Open `_ambiguous.osm` and the area PBF as separate layers — `dbsn-conflate --josm` does this for you.
2. For shared-node cases: look up the `note:conflate:shared_node_ids` values in the PBF layer to see which adjacent ways use those nodes.
3. Run `uv run dbsn-triage <path>/_ambiguous.osm` and decide each feature interactively (paste `ref:dbsn=…` values straight from JOSM for batch triage):

| Decision | Effect on next run's `.osc` |
|---|---|
| `keep_osm` | Pair excluded from future ambiguous; no new OSC action (initial tag modify already emitted) |
| `replace` | Delete old OSM way + create DBSN geometry |
| `delete` | Delete old OSM way |
| `tag_update` | Modify OSM way with your chosen tags (overrides auto-merge) |

On save, decisions are written to `_resolutions.json` and conflation re-runs automatically.

**Or edit `_ambiguous_edited.osm` in JOSM** (`--josm` opens it) and save; the next `dbsn-conflate` run absorbs it. Each member is judged on its own:

- delete a DBSN member: the DBSN feature is not imported (its OSM mate stays, and falls back to `_osm_only.osm`);
- delete the OSM member of a `near_miss` or `shape_match` pair (DBSN member kept): the pair is interpreted as a match, the OSM way is updated in place with the merged tags and the DBSN shape (edits of the DBSN member are applied on top; edits of the OSM member are not absorbed; to really delete the way, set `note:conflate:decision=delete` on it instead);
- delete any other OSM member (`ambiguous` N:M, `contained_by`, ...): the way is deleted, its DBSN mate is matched again (usually imported as new);
- set `note:conflate:decision=match` on either member of any pair to get the same match explicitly. It needs exactly one mate left: in an N:M group, delete the other OSM members (deleted) and the other DBSN members (skipped), then set `match` on the pair you keep;
- set `note:conflate:decision` on a member (every decidable member is written with `undecided`; matching is case-insensitive). The values depend on the kind in `note:conflate`:

| Kind | Value | Effect |
|---|---|---|
| `near_miss`, `shape_match`, `ambiguous` | `keep` or `both` (set on either member) | OSM way untouched, DBSN feature imported beside it |
| `contained_by` (OSM feature inside a DBSN building) | `keep` | OSM way untouched |
| | `make_part` or `part` | `building=X` becomes `building:part=X` |
| | `delete` or `drop` | way deleted |
| `has_part` (matched DBSN feature with OSM features inside it) | `keep` or `both` (set on the DBSN feature or on its former OSM mate) | OSM ways untouched, DBSN feature imported beside them |
| `legacy_import`, `suggest=delete` | `keep` | the suggested delete is withdrawn |

A `has_part` group (the DBSN feature, its former OSM mate, the contained OSM features) is not conflated: nothing of it is in `_changes.osc` until you decide. Delete the DBSN feature to leave the OSM ways alone, or decide each member as above.

A decided member always keeps its own edits, whatever the kind (`geometry` WKT, `-key` tag deletions and per-node `note:conflate:unsafe` decisions are recorded in `_resolutions.json`, as in `_osm_only.osm`). A member counts as decided when it carries `keep`/`both`/`make_part`/`part`, when every mate was deleted, or when it has no mate (`geometry_unsafe`, `legacy_import`). So `part` plus a drawn shape keeps the new shape.

- draw the shape of **both** members of a near-miss or shape-match pair anew: that means keep both, each with its own edit;
- an edit of an undecided member is **not** absorbed (a warning says so): set a decision or delete a mate.

### Reviewing `_new.osm` in JOSM

New features are auto-created on upload. To suppress a new building (e.g. it was demolished and OSM is already correct), record `skip` interactively in `dbsn-triage` — look it up via `dbsn:<classid>` or batch-paste `ref:dbsn=…` values straight from JOSM. On the next conflation run, skipped DBSN IDs are excluded from `_new.osm`.

Alternatively edit the layer directly. `dbsn-conflate --josm` opens `_new_edited.osm`; edit it and save (Cmd+S), then re-run `dbsn-conflate` (`--apply-edits` is on by default). The edited layer is the desired end state:

- a feature you delete is skipped (not created);
- a feature whose tags or shape you change is created with your tags and shape (`geometry` WKT and `-key` tag deletions are recorded in `_resolutions.json`);
- features without changes need nothing. `note:*` tags are ignored.

### Reviewing `_conflated.osm` in JOSM

Each pair is one feature: DBSN shape, merged tags, `ref:dbsn`, and the OSM mate in `note:conflate=osm=wXXX`. Edit `_conflated_edited.osm`, save, re-run `dbsn-conflate`. The edited layer is the desired end state:

- a pair whose tags or shape you change gets your tags and shape on the OSM way's modify (`-key` deletions and `geometry` WKT recorded in `_resolutions.json` under `dbsn:<classid>`); a tag you set resolves a `note:conflate:<key>` conflict on that key;
- a pair you delete deletes its OSM way and the DBSN feature is not imported;
- to leave OSM alone instead, set `note:conflate:decision=keep` on the pair and leave it in the layer: OSM is untouched, the DBSN feature is not imported, and any other edit of that pair is ignored (`undecided` is no instruction; any other value warns and counts as keep). Do not delete it: JOSM drops deleted pairs from the saved file, tag included, so it reads as a plain removal;
- a pair merged from several DBSN features (`merged=a|b` in the note) is handled as a whole: none of its DBSN features reappear in `_new.osm`.

### Reviewing `_osm_only.osm`

Look features up in `dbsn-triage` (`osm:wXXX`) and record `delete` or `tag_update` interactively.

Alternatively edit `_osm_only_edited.osm`, save, re-run `dbsn-conflate`. A feature that stays in the layer is left alone in OSM; edit it only to change it:

- a feature you delete is deleted in OSM;
- a feature whose tags you change gets a tag-only modify (`-key` deletions recorded in `_resolutions.json`, keyed by `wXXX`);
- a feature whose shape you change is moved in place (same way id and history, `geometry` WKT recorded in `_resolutions.json`), and the changeset names no `source` for it. Relation shapes are not moved: the tag edit goes out, your shape appears in `_ambiguous.osm` with `note:conflate=geometry_unsafe`.

**Unsafe nodes.** A node of the way that is tagged, or shared with a highway, waterway, railway, natural, public transport or route way, is written as a node of its own with its OSM tags, `osm:id=nXXX`, `note:conflate:unsafe=detach` and `note:conflate:unsafe_reason` (`tagged`, `shared`, `tagged+shared`; `shared_node_ids` names the other ways). Find them in JOSM with `type:node "note:conflate:unsafe"=*`. What happens to such a node when your edit moves its vertex is the value of `note:conflate:unsafe`:

- `detach` (default): the way gets a new node there and the OSM node is left alone (it keeps its tags and its other ways). An unsafe node you did not move is never touched, whatever its value. A node you remove from the layer is detached too;
- `carry`: the OSM node stays in the way and moves to where you drew it, whatever other way it is shared with (the run prints a `[carry  ]` line for those). Its tags, as shown in the layer, are written with it;
- `delete`: the node is deleted in OSM (`<delete if-unused>`, so a node still used by another way survives), explicit only. The way keeps its shape with a new node at that vertex.

Two unsafe nodes at one coordinate stay standalone, with `overlap:nXXX` appended to `note:conflate:unsafe_reason` and a warning; neither is carried automatically. Legacy-import replacements detach unsafe nodes the same way. Only `_osm_only_edited.osm` shows these nodes for now.

### Re-running after triage

`_resolutions.json` is read automatically on the next `dbsn-conflate` run. Resolved pairs are excluded from `_ambiguous.osm`; `replace` decisions update the OSC; `skip` decisions suppress new-feature creation.

## Based on

- [Danysan1/dbsn-import](https://github.com/Danysan1/dbsn-import): province source list and download URLs
- [musuruan/osm_imports](https://github.com/musuruan/osm_imports): DBSN tag translation (`edifici.py`)
- [arcanma/Umbria_buildings_import](https://github.com/arcanma/Umbria_buildings_import): additional mappings and per-region override pattern
