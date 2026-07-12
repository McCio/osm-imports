"""Download and extract ICCU opendata ZIP with etag-based caching."""

import sys
import zipfile

import httpx

from iccu.common import ETAG_FILE, SOURCE_DIR, SOURCE_URL, ZIP_FILE, http_client, parse_args


def run(overwrite: bool = False) -> None:
    print("=== Step 1: Download ===")
    if overwrite:
        ETAG_FILE.unlink(missing_ok=True)

    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    etag = ETAG_FILE.read_text(encoding="utf-8").strip() if ETAG_FILE.exists() else None
    headers = {"If-None-Match": etag} if etag else {}
    new_etag = ""

    with http_client() as client:
        try:
            with client.stream("GET", SOURCE_URL, headers=headers, follow_redirects=True, timeout=300) as resp:
                if resp.status_code == 304:
                    print("ICCU data up to date (etag match), skipping download.")
                    return
                resp.raise_for_status()
                new_etag = resp.headers.get("ETag", "")
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                with ZIP_FILE.open("wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1 << 20):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = downloaded * 100 // total
                            mb_done = downloaded // (1024 * 1024)
                            mb_total = total // (1024 * 1024)
                            print(f"\r  {pct}% ({mb_done}/{mb_total}MB)", end="", flush=True)
            if total:
                print()
        except httpx.HTTPError as exc:
            print(f"\nDownload failed: {exc}", file=sys.stderr)
            ZIP_FILE.unlink(missing_ok=True)
            sys.exit(1)

    if new_etag:
        ETAG_FILE.write_text(new_etag, encoding="utf-8")
    with zipfile.ZipFile(ZIP_FILE) as zf:
        zf.extractall(SOURCE_DIR)
    ZIP_FILE.unlink(missing_ok=True)
    print(f"Extracted ICCU data to {SOURCE_DIR}")


def main() -> None:
    args = parse_args("Step 1: download ICCU opendata (etag-cached)")
    run(args.overwrite)


if __name__ == "__main__":
    main()
