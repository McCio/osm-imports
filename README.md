# osm-imports

Data preparation tools for Italian geographic datasets into OSM format.

Pre-built output files are published to [GitHub Releases](../../releases). See [CONTRIBUTING.md](CONTRIBUTING.md) to set up a development environment or add a new importer.

## DBSN Buildings Import Pipeline

Converts [DBSN](https://igmi.esercito.difesa.it/servizi/database-di-sintesi-nazionale/) building data into OSM XML for the [DBSN buildings import](https://wiki.openstreetmap.org/wiki/Import/Catalogue/DBSN/Buildings).

Releases are tagged `dbsn-<date>` (e.g. `dbsn-2025-07-28`). Download the `.osm.bz2` for your province and load it into JOSM. To decompress: `bzip2 -d file.osm.bz2`.

### Prerequisites

```bash
uv sync
```

No system GDAL required (`fiona` ships bundled GDAL wheels).

`osmium-tool` is required for the `validate` step:

macOS:
```bash
brew install osmium-tool
```

Ubuntu / Debian:
```bash
sudo apt install osmium-tool
```

### Usage

All commands accept `--province CODE|NAME|REGION|all` (comma-separated for multiple).

| Command | Description |
|---|---|
| `uv run dbsn-discover`   | Fetch latest DBSN links from IGM, write `data/dbsn/sources.json` |
| `uv run dbsn-neighbours` | Populate province adjacency in `sources.json` from OSM (run once) |
| `uv run dbsn-download --province MI` | Download province ZIP archives (also pre-fetches neighbour ZIPs for cross-province extension) |
| `uv run dbsn-download --province MI --no-neighbours` | Download only the requested province, skip neighbour pre-fetch |
| `uv run dbsn-extract  --province MI` | Extract buildings layer from GDB to FlatGeobuf, extend cross-province buildings |
| `uv run dbsn-extract  --province MI --no-extend` | Extract only, skip cross-province extension |
| `uv run dbsn-convert  --province MI` | Convert FlatGeobuf to OSM XML |
| `uv run dbsn-convert  --province MI --format geojson` | Convert to GeoJSON instead |
| `uv run dbsn-convert  --province MI --compress` | Produce `.osm.bz2` (~10× smaller; JOSM accepts it natively) |
| `uv run dbsn-convert  --province VE --area Marghera` | Convert only features intersecting the named `loc_sg` locality |
| `uv run dbsn-validate --province MI` | Validate OSM/OSM.BZ2 files with `osmium check-refs` |
| `uv run dbsn-all      --province all` | Run all steps in sequence |
| `uv run dbsn-all      --province all --neighbours` | Force re-run of the neighbours step |
| `uv run dbsn-all      --province all --no-extend` | Run pipeline but skip cross-province extension |

Add `--overwrite` to reprocess existing output files.

### Pipeline steps

```
discover → neighbours → download → extract (+extend) → convert → validate
```

`dbsn-all` skips `discover` if `sources.json` already exists and a specific province is
requested. It skips `neighbours` if every province in `sources.json` already has a `neighbours`
field. Pass `--neighbours` to force re-running the adjacency step.

### Cross-province buildings

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

### Data layout

```
data/dbsn/
  sources.json           # canonical per-province source list with adjacency (committed)
  zips/                  # downloaded province ZIPs (kept; re-extract without re-download)
  buildings/
    {CODE}_{date}.fgb    # per-province raw FlatGeobuf (one code segment)
    {C1}_{C2}_{C1date}_{C2date}.fgb   # cross-province ext file (two code segments, alphabetical)
  osm/                   # per-province OSM XML (or .osm.bz2 / .geojson); area-filtered runs append _{locality} to the stem
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
    "neighbours": ["RO", "TV", "VE", "VI", "VR"]
  }
}
```

`neighbours` is populated by `dbsn-neighbours` and committed. It lists the codes of all
provinces that share a boundary, used by `extract` to locate cross-province buildings.

### Per-region tag overrides

Each `dbsn/translate_<layer>.py` applies national tag mappings. `dbsn/translate_edifc.py` additionally runs a per-region override (keyed by the `region` field in `sources.json`). Add a function to `_REGION_OVERRIDES` in that file to customise tagging for a specific region.

Current overrides:

| Region | Override |
|---|---|
| Umbria | `edifc_mon=01` → `historic=monument` + `fixme:building` |

### Conflation

The conflation step matches DBSN building footprints against existing OSM features in the same area, produces a change file ready for upload, and flags cases that need human review.

#### Running

```bash
uv run dbsn-conflate --province VE --area Marghera
```

The PBF for the province is downloaded automatically if not already present. Output goes to `data/dbsn/conflate/{CODE}_{date}_{area}/`.

#### Output files

| File | Contents |
|---|---|
| `_changes.osc` | OSC change file: `create` for new buildings, `modify` for tag updates. Deletes are only emitted when a triage decision explicitly requests one (see below). |
| `_new.osm` | DBSN features with no OSM match — will be created on upload |
| `_ambiguous.osm` | Feature pairs that need human review (see below) |
| `_osm_only.osm` | OSM features with no DBSN match (kept for reference; not in the change file) |
| `_resolutions.json` | Append-only decision log written by `dbsn-triage` |

#### What goes to `_ambiguous.osm`

- **Near-miss pairs**: IoU below the match threshold but above `near_miss_iou` — geometrically close but not confident enough to auto-match.
- **N:M pairs**: multiple DBSN features match multiple OSM features (complex overlap).
- **Shared-node cases**: the matched OSM way shares nodes with adjacent ways — replacing its geometry would break connectivity.

Every OSM feature in `_ambiguous.osm` carries `osm:id=<original_osm_id>` (e.g. `osm:id=w138864508`) so decisions survive the file-level ID renumbering. Every DBSN feature carries `ref:dbsn=<classid>`.

Shared-node cases also carry:
- `note:conflate` — `osm_ref=<id>,shared_nodes:<count>` summary
- `note:conflate:shared_node_ids` — comma-separated node IDs that are shared

#### Reviewing `_ambiguous.osm` in JOSM

1. Open `_ambiguous.osm` and the area PBF as separate layers.
2. For shared-node cases: look up the `note:conflate:shared_node_ids` values in the PBF layer to see which adjacent ways use those nodes.
3. Run `uv run dbsn-triage <path>/_ambiguous.osm` and decide each feature interactively (paste `ref:dbsn=…` values straight from JOSM for batch triage):

| Decision | Effect on next run's `.osc` |
|---|---|
| `keep_osm` | Pair excluded from future ambiguous; no new OSC action (initial tag modify already emitted) |
| `replace` | Delete old OSM way + create DBSN geometry |
| `delete` | Delete old OSM way |
| `tag_update` | Modify OSM way with your chosen tags (overrides auto-merge) |

On save, decisions are written to `_resolutions.json` and conflation re-runs automatically.

#### Reviewing `_new.osm` in JOSM

New features are auto-created on upload. To suppress a new building (e.g. it was demolished and OSM is already correct), record `skip` interactively in `dbsn-triage` — look it up via `dbsn:<classid>` or batch-paste `ref:dbsn=…` values straight from JOSM. On the next conflation run, skipped DBSN IDs are excluded from `_new.osm`.

#### Reviewing `_osm_only.osm`

Look features up in `dbsn-triage` (`osm:wXXX`) and record `delete` or `tag_update` interactively.

#### Re-running after triage

`_resolutions.json` is read automatically on the next `dbsn-conflate` run. Resolved pairs are excluded from `_ambiguous.osm`; `replace` decisions update the OSC; `skip` decisions suppress new-feature creation.

### Based on

- [Danysan1/dbsn-import](https://github.com/Danysan1/dbsn-import): province source list and download URLs
- [musuruan/osm_imports](https://github.com/musuruan/osm_imports): DBSN tag translation (`edifici.py`)
- [arcanma/Umbria_buildings_import](https://github.com/arcanma/Umbria_buildings_import): additional mappings and per-region override pattern

---

## ICCU Libraries Import Pipeline

Conflates [ICCU](https://opendata.anagrafe.iccu.sbn.it/) library catalogue data with existing OSM objects.

Releases are tagged `iccu-<date>`.

### Prerequisites

Same as DBSN (`uv sync`). No additional system tools required.

### Usage

All commands accept `--region CODE|NAME|REGION|all` (comma-separated for multiple).

| Command | Description |
|---|---|
| `uv run iccu-download`               | Download ICCU opendata ZIP (etag-cached; skips if unchanged) |
| `uv run iccu-clean`                  | Clean and deduplicate the raw CSV into `data/iccu/clean.csv` |
| `uv run iccu-validate`               | Validate `clean.csv` for data quality (duplicate ISILs, bad coordinates, …) |
| `uv run iccu-export --region VE`     | Export dataset as OSM XML + GeoJSON for a region |
| `uv run iccu-conflate --region VE`   | Conflate against OSM via Overpass; produce change files |
| `uv run iccu-all --region all`       | Run all steps in sequence for one or more regions |
| `uv run iccu-list-labels`            | List available region labels |

Add `--overwrite` to reprocess existing output files.

### Pipeline steps

```
download → clean → [validate] → export → conflate
```

`iccu-validate` is a standalone check on `clean.csv`; it does not run automatically as part of `iccu-all`. Run it before `export`/`conflate` when troubleshooting data quality issues. Errors exit 1 (duplicate ISILs, malformed ISILs, swapped coordinates); warnings are logged to stderr but do not block the pipeline.

### Data layout

```
data/iccu/
  source/                  # raw CSV files extracted from the ICCU ZIP
  iccu.etag                # etag for conditional re-download
  clean.csv                # cleaned, geocoded library records
  overpass/
    {region}.osm           # cached Overpass query result per region
  osm/
    {region}/
      dataset.osm(.bz2)    # full library dataset for conflation input
      dataset.geojson
      changes.osm          # additions / modifications vs existing OSM
      changes.osc          # same in OSC format
      changes.geojson      # for visual review in JOSM / umap
```
