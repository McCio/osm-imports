# osm-imports

Data preparation tools for Italian geographic datasets into OSM format. Pre-built output files are published to [GitHub Releases](../../releases).

| Tool | What it does | Guide |
|---|---|---|
| **DBSN** | Converts the [DBSN](https://igmi.esercito.difesa.it/servizi/database-di-sintesi-nazionale/) building database into OSM XML and conflates it against existing OSM buildings (`dbsn-*` commands) | [dbsn/README.md](dbsn/README.md) |
| **ICCU** | Conflates the [ICCU](https://opendata.anagrafe.iccu.sbn.it/) library catalogue with existing OSM objects (`iccu-*` commands) | [iccu/README.md](iccu/README.md) |

Both need [uv](https://docs.astral.sh/uv/) and `uv sync`; the DBSN tool also needs `osmium-tool` (see its guide).

To develop on the tools or add a new importer, read [CONTRIBUTING.md](CONTRIBUTING.md).
