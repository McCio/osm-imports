"""Print province codes for a selector (used by the GH Actions workflow)."""

from dbsn.common import parse_args


def main() -> None:
    args = parse_args("Print province codes for a selector (space-separated)", overwrite=False)
    print(" ".join(p["code"] for p in args.provinces))


if __name__ == "__main__":
    main()
