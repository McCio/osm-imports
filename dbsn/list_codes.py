"""Print province codes for a selector (used by the GH Actions workflow)."""

from dbsn.common import parse_args


def _fmt_size(size: int | None) -> str:
    if size is None:
        return "unknown"
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    return f"{size // 1024} KB"


def main() -> None:
    args = parse_args(
        "Print province codes for a selector (space-separated)",
        overwrite=False,
        setup=lambda p: p.add_argument("-v", "--verbose", action="count", default=0, help="Increase output detail (-v, -vv, -vvv)"),
    )
    v = args.verbose
    if v == 0:
        print(" ".join(p["code"] for p in args.provinces))
    else:
        for p in args.provinces:
            parts = [p["code"]]
            if v >= 2:
                parts.append(f"{p['province']:<30}")
            if v >= 3:
                parts.append(f"{p['region']:<20}")
            parts += [p["date"], _fmt_size(p["zip_size"])]
            print("  ".join(parts))


if __name__ == "__main__":
    main()
