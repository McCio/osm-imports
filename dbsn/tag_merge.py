"""Tag merging for DBSN conflation."""

from __future__ import annotations


def merge_tags(
    dbsn_tags: dict[str, str],
    osm_tags: dict[str, str],
    specificity: dict[str, list[str]],
) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """Merge DBSN and OSM tags. More specific value wins per specificity ranking.

    Returns (merged_tags, conflicts) where conflicts maps key → (osm_val, dbsn_val).
    """
    merged: dict[str, str] = {}
    conflicts: dict[str, tuple[str, str]] = {}
    all_keys = set(dbsn_tags) | set(osm_tags)

    for key in all_keys:
        dbsn_val = dbsn_tags.get(key)
        osm_val = osm_tags.get(key)

        if dbsn_val is None:
            merged[key] = osm_val
        elif osm_val is None or dbsn_val == osm_val:
            merged[key] = dbsn_val
        else:
            ranking = specificity.get(key, [])
            dbsn_rank = ranking.index(dbsn_val) if dbsn_val in ranking else -1
            osm_rank = ranking.index(osm_val) if osm_val in ranking else -1

            if dbsn_rank > osm_rank:
                merged[key] = dbsn_val
            elif osm_rank > dbsn_rank:
                merged[key] = osm_val
            else:
                merged[key] = osm_val  # keep osm on tie/both-unlisted
                if key != "source":  # stripped from the osmChange, so it cannot conflict there
                    conflicts[key] = (osm_val, dbsn_val)

    return merged, conflicts
