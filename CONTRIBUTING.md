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
- Run `uv run ruff format && uv run ruff check` before committing

## Extending the DBSN building translation

Tag mappings live in `dbsn/translate.py`. After adding a new tag key:

1. Add the key to `TAG_KEYS` in the same file — this keeps the GeoJSON output schema in sync.
2. Smoke-test with `uv run dbsn-convert --province VE --format geojson --overwrite`.

## Adding a new importer

Each data source gets its own package alongside `dbsn/`:

```
<source>/
  __init__.py
  discover.py   # fetch source URLs
  download.py   # download archives
  extract.py    # GDB/Shapefile → FlatGeobuf (reuse fiona)
  convert.py    # FlatGeobuf → OSM XML / GeoJSON (reuse utils/convert.py)
  translate.py  # attribute → OSM tag mapping + TAG_KEYS
  validate.py   # osmium check-refs (reuse utils/validate.py)
  pipeline.py   # wire the steps
```

Register the new entry points in `pyproject.toml` under `[project.scripts]`.
