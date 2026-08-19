#!/usr/bin/env python3
"""
Dual-Link Downloader
Splits ONE download across two network interfaces (e.g. WiFi + USB tethering)
using parallel HTTP range requests, so both connections contribute bandwidth
to a single file.

Requirements (run on your PC, not this sandbox):
    pip install requests psutil

Usage:
    # 1. See which local IP belongs to which interface
    python dual_link_downloader.py --list-interfaces

    # 2. Download using both
    python dual_link_downloader.py "https://example.com/file.zip" \
        --link1 192.168.1.23 --link2 192.168.42.100

Notes:
- Only works if the server supports HTTP Range requests (most file hosts,
  CDNs, and direct-download links do; some dynamic pages don't).
- If range requests aren't supported, it automatically falls back to a
  normal single-connection download over your default route.
- You can pass more than 2 links if you have more interfaces:
    --link1 IP --link2 IP --link3 IP ...
"""

import argparse
import os
import re
import sys
import threading
import time
from urllib.parse import urlparse, unquote

import requests
from requests.adapters import HTTPAdapter

try:
    import psutil
except ImportError:
    psutil = None


class SourceAddressAdapter(HTTPAdapter):
    """Forces all connections through this adapter to originate from a
    specific local IP address (i.e. a specific network interface)."""

    def __init__(self, source_ip, **kwargs):
        self.source_ip = source_ip
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["source_address"] = (self.source_ip, 0)
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["source_address"] = (self.source_ip, 0)
        return super().proxy_manager_for(*args, **kwargs)


def list_interfaces():
    if psutil is None:
        print("psutil isn't installed. Run: pip install psutil")
        print("Alternatively, run 'ipconfig' (Windows) and copy the IPv4 "
              "address of each adapter you want to use.")
        return

    print(f"{'Interface':<30} {'IP address':<18} Status")
    print("-" * 60)
    stats = psutil.net_if_stats()
    for name, addrs in psutil.net_if_addrs().items():
        up = stats[name].isup if name in stats else False
        for addr in addrs:
            if addr.family.name == "AF_INET":
                print(f"{name:<30} {addr.address:<18} {'up' if up else 'down'}")


def safe_filename_from_url(url, content_disposition=None):
    """Derive a filesystem-safe filename from a URL, ignoring query strings
    and stripping characters Windows won't allow in filenames."""
    name = None
    if content_disposition:
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition)
        if match:
            name = unquote(match.group(1))
    if not name:
        path = urlparse(url).path
        name = unquote(os.path.basename(path)) or "download"
    # Strip characters invalid in Windows filenames
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip()
    return name or "download"


def human(n):
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def parse_cookie_header(cookie_str):
    """Parse a 'name1=value1; name2=value2' cookie header string into a dict."""
    jar = {}
    if not cookie_str:
        return jar
    for part in cookie_str.split(";"):
        if "=" in part:
            name, _, value = part.strip().partition("=")
            jar[name] = value
    return jar


def download_chunk(url, start, end, source_ip, out_path, chunk_id, progress, errors, cookies=None):
    try:
        session = requests.Session()
        if cookies:
            session.cookies.update(cookies)
        if source_ip:
            adapter = SourceAddressAdapter(source_ip)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
        headers = {"Range": f"bytes={start}-{end}"}
        with session.get(url, headers=headers, stream=True, timeout=30) as r:
            r.raise_for_status()
            with open(out_path, "r+b") as f:
                f.seek(start)
                for data in r.iter_content(chunk_size=256 * 1024):
                    if not data:
                        continue
                    f.write(data)
                    progress[chunk_id] += len(data)
    except Exception as e:
        errors[chunk_id] = str(e)


def print_progress(progress, total_size, links, stop_event):
    start_time = time.time()
    while not stop_event.is_set():
        done = sum(progress.values())
        elapsed = max(time.time() - start_time, 0.01)
        speed = done / elapsed
        pct = (done / total_size * 100) if total_size else 0
        parts = "  ".join(f"link{i+1}={human(progress[i])}" for i in range(len(links)))
        sys.stdout.write(
            f"\r{pct:5.1f}%  {human(done)}/{human(total_size)}  "
            f"{human(speed)}/s  [{parts}]   "
        )
        sys.stdout.flush()
        time.sleep(0.5)


def fallback_single_download(url, output, cookies=None):
    print("Server doesn't support range requests — falling back to a normal "
          "single-connection download over your default route.")
    with requests.get(url, stream=True, timeout=30, cookies=cookies) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0))
        done = 0
        start = time.time()
        with open(output, "wb") as f:
            for data in r.iter_content(chunk_size=256 * 1024):
                f.write(data)
                done += len(data)
                elapsed = max(time.time() - start, 0.01)
                pct = (done / total * 100) if total else 0
                sys.stdout.write(
                    f"\r{pct:5.1f}%  {human(done)}/{human(total)}  "
                    f"{human(done/elapsed)}/s   "
                )
                sys.stdout.flush()
    print("\nDone.")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", nargs="?", help="URL of the file to download")
    parser.add_argument("-o", "--output", help="Output filename (default: taken from URL)")
    parser.add_argument("--list-interfaces", action="store_true",
                         help="List local network interfaces and their IPs, then exit")
    parser.add_argument("--link1", help="Local IP of first connection (e.g. WiFi)")
    parser.add_argument("--link2", help="Local IP of second connection (e.g. USB tether)")
    parser.add_argument("--link3", help="Local IP of an optional third connection")
    parser.add_argument("--link4", help="Local IP of an optional fourth connection")
    parser.add_argument("--cookies", help="Cookie header string 'name=value; name2=value2' "
                                           "for authenticated downloads")
    args = parser.parse_args()
    cookies = parse_cookie_header(args.cookies)

    if args.list_interfaces:
        list_interfaces()
        return

    if not args.url:
        parser.error("a URL is required unless using --list-interfaces")

    links = [ip for ip in [args.link1, args.link2, args.link3, args.link4] if ip]
    if len(links) < 2:
        parser.error("provide at least --link1 and --link2 (use --list-interfaces "
                      "to find the right IPs)")

    print(f"Checking {args.url} ...")
    head = requests.head(args.url, allow_redirects=True, timeout=15, cookies=cookies)
    size = int(head.headers.get("Content-Length", 0))
    accepts_ranges = head.headers.get("Accept-Ranges", "").lower() == "bytes"

    output = args.output or safe_filename_from_url(
        args.url, head.headers.get("Content-Disposition")
    )

    if not accepts_ranges or size == 0:
        fallback_single_download(args.url, output, cookies=cookies)
        return

    print(f"File size: {human(size)}  |  Splitting across {len(links)} connections: {links}")

    # Pre-allocate the output file so each thread can seek+write independently
    with open(output, "wb") as f:
        f.truncate(size)

    n = len(links)
    chunk_size = size // n
    ranges = []
    for i in range(n):
        start = i * chunk_size
        end = size - 1 if i == n - 1 else start + chunk_size - 1
        ranges.append((start, end))

    progress = {i: 0 for i in range(n)}
    errors = {}
    stop_event = threading.Event()

    monitor = threading.Thread(target=print_progress, args=(progress, size, links, stop_event))
    monitor.daemon = True
    monitor.start()

    threads = []
    for i, ((start, end), ip) in enumerate(zip(ranges, links)):
        t = threading.Thread(target=download_chunk,
                              args=(args.url, start, end, ip, output, i, progress, errors, cookies))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    stop_event.set()
    monitor.join(timeout=1)

    print()
    if errors:
        print("One or more chunks failed:")
        for chunk_id, msg in errors.items():
            print(f"  link{chunk_id + 1}: {msg}")
        print(f"Partial file saved as {output} — you may need to retry.")
    else:
        print(f"Done. Saved as {output}")


if __name__ == "__main__":
    main()