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
| `uv run dbsn-discover` | Fetch latest DBSN links from IGM, write `data/dbsn/sources.json` |
| `uv run dbsn-download --province MI` | Download province ZIP archives |
| `uv run dbsn-extract  --province MI` | Extract buildings layer from GDB to FlatGeobuf |
| `uv run dbsn-convert  --province MI` | Convert FlatGeobuf to OSM XML |
| `uv run dbsn-convert  --province MI --format geojson` | Convert to GeoJSON instead |
| `uv run dbsn-convert  --province MI --compress` | Produce `.osm.bz2` (~10× smaller; JOSM accepts it natively) |
| `uv run dbsn-validate --province MI` | Validate OSM/OSM.BZ2 files with `osmium check-refs` |
| `uv run dbsn-all      --province all` | Run all steps in sequence |

Add `--overwrite` to reprocess existing output files.

### Data layout

```
data/dbsn/
  sources.json      # canonical per-province source list (committed)
  zips/             # downloaded province ZIPs (kept; re-extract without re-download)
  buildings/        # per-province FlatGeobuf
  osm/              # per-province OSM XML (or .osm.bz2 / .geojson)
```

`data/dbsn/unzipped/` is created during extract and deleted automatically once the FlatGeobuf is written.

### Based on

- [Danysan1/dbsn-import](https://github.com/Danysan1/dbsn-import): province source list and download URLs
- [musuruan/osm_imports](https://github.com/musuruan/osm_imports): DBSN tag translation (`edifici.py`)
