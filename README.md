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
  osm/                   # per-province OSM XML (or .osm.bz2 / .geojson)
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

`dbsn/translate.py` applies national tag mappings then optionally runs a per-region override (keyed by the `region` field in `sources.json`).  Add a function to `_REGION_OVERRIDES` to customise tagging for a specific region.

Current overrides:

| Region | Override |
|---|---|
| Umbria | `edifc_mon=01` → `historic=monument` + `fixme:building` |

### Based on

- [Danysan1/dbsn-import](https://github.com/Danysan1/dbsn-import): province source list and download URLs
- [musuruan/osm_imports](https://github.com/musuruan/osm_imports): DBSN tag translation (`edifici.py`)
- [arcanma/Umbria_buildings_import](https://github.com/arcanma/Umbria_buildings_import): additional mappings and per-region override pattern
