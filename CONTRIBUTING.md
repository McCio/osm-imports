# Contributing

## Development setup

```bash
git clone https://github.com/McCio/osm-imports.git
cd osm-imports
uv sync
```

### osmium-tool

Required for the `validate` step.

macOS:
```bash
brew install osmium-tool
```

Ubuntu / Debian:
```bash
sudo apt install osmium-tool
```

## Workflow

- Branch from `main`, open a PR back to `main`
- Use [Conventional Commits](https://www.conventionalcommits.org/) (`feat`, `fix`, `refactor`, `chore`, …)
- Before committing:
  ```bash
  uv run ruff format && uv run ruff check
  uv run pytest tests/
  ```

## DBSN internals

User documentation is in [dbsn/README.md](dbsn/README.md); this section is for changing the code.

### Pipeline model

Each step is a `utils/dag.py:Task` (`name`, `run()`, `skip_if()`, `dependencies()`, optional `weight` and `step`). `step` is the `--overwrite` step name: when that step is overwritten the DAG runs the task regardless of `skip_if`, so `skip_if` never checks overwrite itself. Tasks deduplicate by `name`. Entry points live next to the task (`main()` in each module, registered in `pyproject.toml`).

`ConvertTask`, `ValidateTask` and `ConflateTask` depend on the earlier steps, so a command that needs `--layers` or `--area` takes the option and hands it to the tasks it builds.

### Layers

A layer is a `LayerDef` exported by `dbsn/translate_<layer>.py` and registered in `dbsn/layers.py:_load()`. `LayerSpec` (picklable, no translate function) is used at extract time, `LayerDef` at convert time; keep them separate.

Which layers run is decided in one place, `dbsn/layers.py`, from `--layers` and `dbsn/layers.yaml`. Never hard-code a layer list in a step. Output names carry the layer set through `layer_slug()` (empty when every layer is selected); build every path through that helper so layer sets never shadow each other.

### Per-region tag overrides

`dbsn/translate_edifc.py` runs a per-region override keyed by the `region` field of `sources.json`. Add a function to `_REGION_OVERRIDES` in that file to customise tagging for a region.

| Region | Override |
|---|---|
| Umbria | `edifc_mon=01` → `historic=monument` + `fixme:building` |

### Extending the DBSN building translation

Tag mappings live in per-layer files: `dbsn/translate_edifc.py`, `dbsn/translate_edi_min.py`, etc. After adding a new tag key:

1. Add the key to `tag_keys` in the relevant layer's `LAYER: LayerDef` — this keeps the GeoJSON output schema in sync.
2. Smoke-test with `uv run dbsn-convert --province VE --format geojson --overwrite`.

A new layer also needs its name in `dbsn/layers.py:_load()`; add it to `defaults` in `dbsn/layers.yaml` only when it belongs in every default run.

### Conflation

Config: numeric parameters, region and province layering and legacy-import patterns in `dbsn/conflate_config.yaml`; ranked tag values in `dbsn/tag_specificity.yaml`. A new conflation step is a `Task` subclass in `dbsn/conflate.py`. Edited-layer absorption lives in `dbsn/edits.py`; the vocabulary (review file, decision, decided member) is in `GLOSSARY.md` and the design decisions in `docs/adr/` (both local, not committed).

## Adding a new importer

Each data source gets its own package alongside `dbsn/`:

```
<source>/
  __init__.py
  discover.py   # fetch source URLs
  download.py   # download archives
  extract.py    # GDB/Shapefile → FlatGeobuf (reuse fiona)
  convert.py    # FlatGeobuf → OSM XML / GeoJSON (reuse utils/writers.py)
  translate.py  # attribute → OSM tag mapping + TAG_KEYS
  validate.py   # osmium check-refs (reuse utils/validate.py)
  pipeline.py   # wire the steps
```

Register the new entry points in `pyproject.toml` under `[project.scripts]`.
