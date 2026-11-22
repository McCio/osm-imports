# ICCU libraries import

Conflates [ICCU](https://opendata.anagrafe.iccu.sbn.it/) library catalogue data with existing OSM objects. Developer documentation is in [CONTRIBUTING.md](../CONTRIBUTING.md).

Releases are tagged `iccu-<date>`.

## Prerequisites

```bash
uv sync
```

No system tools required.

## Commands

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

## Pipeline steps

```
download → clean → [validate] → export → conflate
```

`iccu-validate` is a standalone check on `clean.csv`; it does not run automatically as part of `iccu-all`. Run it before `export`/`conflate` when troubleshooting data quality issues. Errors exit 1 (duplicate ISILs, malformed ISILs, swapped coordinates); warnings are logged to stderr but do not block the pipeline.

## Data layout

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
