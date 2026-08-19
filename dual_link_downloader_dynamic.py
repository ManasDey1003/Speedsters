#!/usr/bin/env python3
"""
Dual-Link Downloader with Dynamic Chunk Scheduling + Simple GUI

Downloads one file using multiple network interfaces simultaneously.

Features:
- Splits the file into many small HTTP byte ranges.
- Dynamically gives the next range to whichever interface worker becomes free.
- Each interface has its own requests.Session bound to its local source IP.
- Live GUI showing:
    * overall progress
    * current chunk per interface
    * completed / queued chunks
    * per-interface instantaneous speed
    * combined instantaneous speed
    * recent speed graph
- Verifies that range requests return HTTP 206.
- Writes every chunk directly to its correct position in the output file.
- Retries failed chunks through the shared queue.
- Falls back to a normal single download when Range requests are unavailable.

Requirements:
    pip install requests psutil matplotlib

Usage:
    python dual_link_downloader.py --list-interfaces

    python dual_link_downloader.py "https://example.com/file.zip" \
        --link1 192.168.0.131 \
        --link2 10.164.141.46

Optional:
    --chunk-size-mb 8
    --max-retries 3
    --output myfile.zip
    --cookies "name=value; name2=value2"
"""

import argparse
import os
import queue
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlparse, unquote

import requests
from requests.adapters import HTTPAdapter

try:
    import psutil
except ImportError:
    psutil = None

import tkinter as tk
from tkinter import ttk, messagebox

try:
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
except ImportError:
    FigureCanvasTkAgg = None
    Figure = None


# -----------------------------
# Networking
# -----------------------------

class SourceAddressAdapter(HTTPAdapter):
    """Force sockets created through this adapter to use a local source IP."""

    def __init__(self, source_ip, **kwargs):
        self.source_ip = source_ip
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["source_address"] = (self.source_ip, 0)
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["source_address"] = (self.source_ip, 0)
        return super().proxy_manager_for(*args, **kwargs)


def make_bound_session(source_ip):
    session = requests.Session()
    adapter = SourceAddressAdapter(
        source_ip,
        pool_connections=4,
        pool_maxsize=4,
        max_retries=0,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


# -----------------------------
# Helpers
# -----------------------------

def human(n):
    n = float(n or 0)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def human_speed(bytes_per_second):
    return f"{human(bytes_per_second)}/s"


def safe_filename_from_url(url, content_disposition=None):
    name = None
    if content_disposition:
        match = re.search(
            r"filename\*?=(?:UTF-8\'\')?\"?([^\";]+)\"?",
            content_disposition,
            re.IGNORECASE,
        )
        if match:
            name = unquote(match.group(1))

    if not name:
        path = urlparse(url).path
        name = unquote(os.path.basename(path)) or "download"

    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip()
    return name or "download"


def parse_cookie_header(cookie_str):
    jar = {}
    if not cookie_str:
        return jar

    for part in cookie_str.split(";"):
        if "=" in part:
            name, _, value = part.strip().partition("=")
            jar[name] = value
    return jar


def list_interfaces():
    if psutil is None:
        print("psutil isn't installed. Run: pip install psutil")
        return

    print(f"{'Interface':<30} {'IP address':<18} Status")
    print("-" * 60)

    stats = psutil.net_if_stats()

    for name, addrs in psutil.net_if_addrs().items():
        up = stats[name].isup if name in stats else False

        for addr in addrs:
            if getattr(addr.family, "name", "") == "AF_INET":
                print(
                    f"{name:<30} {addr.address:<18} "
                    f"{'up' if up else 'down'}"
                )


# -----------------------------
# Download state
# -----------------------------

@dataclass
class Chunk:
    chunk_id: int
    start: int
    end: int
    attempts: int = 0

    @property
    def size(self):
        return self.end - self.start + 1


class DownloadState:
    def __init__(self, links, total_size, total_chunks):
        self.lock = threading.Lock()

        self.links = links
        self.total_size = total_size
        self.total_chunks = total_chunks

        self.completed_chunks = 0
        self.completed_bytes = 0

        self.chunk_status = {
            i: {
                "status": "queued",
                "start": None,
                "end": None,
                "interface": None,
                "ip": None,
                "bytes": 0,
                "error": None,
            }
            for i in range(total_chunks)
        }

        self.link_stats = {
            i: {
                "ip": ip,
                "current_chunk": None,
                "current_chunk_start": None,
                "current_chunk_end": None,
                "bytes_total": 0,
                "bytes_since_tick": 0,
                "speed": 0.0,
                "last_error": None,
            }
            for i, ip in enumerate(links)
        }

        self.total_speed = 0.0
        self.history = deque(maxlen=120)
        self.error_messages = []

        self.started_at = time.time()
        self.finished = False
        self.failed = False

    def set_chunk_queued(self, chunk):
        with self.lock:
            self.chunk_status[chunk.chunk_id].update(
                status="queued",
                start=chunk.start,
                end=chunk.end,
                interface=None,
                ip=None,
                bytes=0,
                error=None,
            )

    def set_chunk_running(self, chunk, worker_id):
        with self.lock:
            self.chunk_status[chunk.chunk_id].update(
                status="downloading",
                start=chunk.start,
                end=chunk.end,
                interface=worker_id,
                ip=self.links[worker_id],
                bytes=0,
                error=None,
            )
            self.link_stats[worker_id].update(
                current_chunk=chunk.chunk_id,
                current_chunk_start=chunk.start,
                current_chunk_end=chunk.end,
                last_error=None,
            )

    def add_bytes(self, chunk, worker_id, amount):
        with self.lock:
            self.chunk_status[chunk.chunk_id]["bytes"] += amount
            self.link_stats[worker_id]["bytes_total"] += amount
            self.link_stats[worker_id]["bytes_since_tick"] += amount
            self.completed_bytes += amount

    def finish_chunk(self, chunk, worker_id):
        with self.lock:
            self.chunk_status[chunk.chunk_id]["status"] = "complete"
            self.chunk_status[chunk.chunk_id]["interface"] = worker_id
            self.completed_chunks += 1

            self.link_stats[worker_id].update(
                current_chunk=None,
                current_chunk_start=None,
                current_chunk_end=None,
            )

    def fail_chunk(self, chunk, worker_id, error_message):
        with self.lock:
            partial = self.chunk_status[chunk.chunk_id]["bytes"]

            # The partial bytes will be overwritten on retry, so remove them
            # from unique-download progress before re-queuing the chunk.
            self.completed_bytes = max(0, self.completed_bytes - partial)
            self.chunk_status[chunk.chunk_id]["bytes"] = 0

            self.chunk_status[chunk.chunk_id]["status"] = "retrying"
            self.chunk_status[chunk.chunk_id]["interface"] = worker_id
            self.chunk_status[chunk.chunk_id]["error"] = error_message

            self.link_stats[worker_id]["last_error"] = error_message
            self.error_messages.append(
                f"Chunk {chunk.chunk_id + 1} / {error_message}"
            )

            self.link_stats[worker_id].update(
                current_chunk=None,
                current_chunk_start=None,
                current_chunk_end=None,
            )

    def mark_final_failed(self, chunk, error_message):
        with self.lock:
            self.chunk_status[chunk.chunk_id]["status"] = "failed"
            self.chunk_status[chunk.chunk_id]["error"] = error_message
            self.failed = True
            self.error_messages.append(
                f"Chunk {chunk.chunk_id + 1} permanently failed: {error_message}"
            )

    def tick_speed(self, interval):
        with self.lock:
            total = 0.0

            for stats in self.link_stats.values():
                stats["speed"] = stats["bytes_since_tick"] / interval
                stats["bytes_since_tick"] = 0
                total += stats["speed"]

            self.total_speed = total
            self.history.append(
                (
                    time.time(),
                    [s["speed"] for s in self.link_stats.values()],
                    total,
                )
            )

    def snapshot(self):
        with self.lock:
            return {
                "completed_bytes": self.completed_bytes,
                "completed_chunks": self.completed_chunks,
                "total_chunks": self.total_chunks,
                "total_size": self.total_size,
                "total_speed": self.total_speed,
                "finished": self.finished,
                "failed": self.failed,
                "links": [
                    dict(v) for v in self.link_stats.values()
                ],
                "chunks": {
                    k: dict(v) for k, v in self.chunk_status.items()
                },
                "history": list(self.history),
                "errors": list(self.error_messages[-10:]),
            }


# -----------------------------
# Dynamic scheduler worker
# -----------------------------

class DownloadWorker(threading.Thread):
    def __init__(
        self,
        worker_id,
        source_ip,
        url,
        output_path,
        work_queue,
        state,
        cookies,
        stop_event,
        max_retries,
    ):
        super().__init__(daemon=True)

        self.worker_id = worker_id
        self.source_ip = source_ip
        self.url = url
        self.output_path = output_path
        self.work_queue = work_queue
        self.state = state
        self.cookies = cookies or {}
        self.stop_event = stop_event
        self.max_retries = max_retries

        self.session = make_bound_session(source_ip)

        if self.cookies:
            self.session.cookies.update(self.cookies)

    def run(self):
        while not self.stop_event.is_set():
            try:
                chunk = self.work_queue.get(timeout=0.25)
            except queue.Empty:
                return

            try:
                self.state.set_chunk_running(chunk, self.worker_id)
                self._download_chunk(chunk)
                self.state.finish_chunk(chunk, self.worker_id)
            except Exception as exc:
                message = str(exc)

                if chunk.attempts < self.max_retries:
                    chunk.attempts += 1
                    self.state.fail_chunk(chunk, self.worker_id, message)
                    self.state.set_chunk_queued(chunk)
                    self.work_queue.put(chunk)
                else:
                    self.state.mark_final_failed(chunk, message)
                    self.stop_event.set()
            finally:
                self.work_queue.task_done()

    def _download_chunk(self, chunk):
        headers = {
            "Range": f"bytes={chunk.start}-{chunk.end}",
            "Accept-Encoding": "identity",
        }

        with self.session.get(
            self.url,
            headers=headers,
            stream=True,
            timeout=(15, 60),
            allow_redirects=True,
        ) as response:

            if response.status_code != 206:
                content_type = response.headers.get("Content-Type", "")
                raise RuntimeError(
                    f"HTTP {response.status_code} instead of 206 "
                    f"(Content-Type: {content_type})"
                )

            content_range = response.headers.get("Content-Range", "")
            match = re.match(
                r"bytes\s+(\d+)-(\d+)/(\d+|\*)",
                content_range,
                re.IGNORECASE,
            )

            if not match:
                raise RuntimeError(
                    f"Missing/invalid Content-Range: {content_range!r}"
                )

            returned_start = int(match.group(1))
            returned_end = int(match.group(2))

            if returned_start != chunk.start or returned_end != chunk.end:
                raise RuntimeError(
                    "Server returned unexpected range: "
                    f"{content_range}; requested bytes "
                    f"{chunk.start}-{chunk.end}"
                )

            expected = chunk.size
            received = 0

            with open(self.output_path, "r+b") as output:
                output.seek(chunk.start)

                for data in response.iter_content(
                    chunk_size=256 * 1024
                ):
                    if self.stop_event.is_set():
                        return

                    if not data:
                        continue

                    remaining = expected - received

                    if len(data) > remaining:
                        data = data[:remaining]

                    output.write(data)
                    received += len(data)

                    self.state.add_bytes(
                        chunk,
                        self.worker_id,
                        len(data),
                    )

                    if received >= expected:
                        break

            if received != expected:
                raise RuntimeError(
                    f"Incomplete chunk: received {received} "
                    f"of {expected} bytes"
                )


# -----------------------------
# Download engine
# -----------------------------

class DownloadController(threading.Thread):
    def __init__(
        self,
        url,
        output_path,
        links,
        chunk_size,
        max_retries,
        cookies,
        state,
        status_callback=None,
    ):
        super().__init__(daemon=True)

        self.url = url
        self.output_path = output_path
        self.links = links
        self.chunk_size = chunk_size
        self.max_retries = max_retries
        self.cookies = cookies or {}
        self.state = state
        self.status_callback = status_callback

        self.stop_event = threading.Event()
        self.work_queue = queue.Queue()

        self.workers = []

    def run(self):
        try:
            self._run()
        except Exception as exc:
            with self.state.lock:
                self.state.failed = True
                self.state.error_messages.append(f"Fatal: {exc}")
            self.stop_event.set()

    def _run(self):
        if self.status_callback:
            self.status_callback("Checking server...")

        head = requests.head(
            self.url,
            allow_redirects=True,
            timeout=15,
            cookies=self.cookies,
        )

        size = int(head.headers.get("Content-Length", 0))
        accepts_ranges = (
            head.headers.get("Accept-Ranges", "").lower() == "bytes"
        )

        if size <= 0:
            raise RuntimeError(
                "Server did not provide a usable Content-Length. "
                "Dynamic range download currently requires a known size."
            )

        if not accepts_ranges:
            raise RuntimeError(
                "Server does not advertise Accept-Ranges: bytes. "
                "Use a direct file URL that supports HTTP Range requests."
            )

        if self.status_callback:
            self.status_callback(
                f"File size: {human(size)} | "
                f"chunk size: {human(self.chunk_size)} | "
                f"interfaces: {len(self.links)}"
            )

        with self.state.lock:
            self.state.total_size = size

        with open(self.output_path, "wb") as f:
            f.truncate(size)

        chunks = []
        chunk_id = 0
        start = 0

        while start < size:
            end = min(start + self.chunk_size, size) - 1

            chunk = Chunk(
                chunk_id=chunk_id,
                start=start,
                end=end,
            )

            chunks.append(chunk)

            chunk_id += 1
            start = end + 1

        with self.state.lock:
            self.state.total_chunks = len(chunks)
            self.state.chunk_status = {
                i: {
                    "status": "queued",
                    "start": chunk.start,
                    "end": chunk.end,
                    "interface": None,
                    "ip": None,
                    "bytes": 0,
                    "error": None,
                }
                for i, chunk in enumerate(chunks)
            }

        for chunk in chunks:
            self.work_queue.put(chunk)

        for worker_id, ip in enumerate(self.links):
            worker = DownloadWorker(
                worker_id=worker_id,
                source_ip=ip,
                url=self.url,
                output_path=self.output_path,
                work_queue=self.work_queue,
                state=self.state,
                cookies=self.cookies,
                stop_event=self.stop_event,
                max_retries=self.max_retries,
            )
            self.workers.append(worker)
            worker.start()

        while not self.stop_event.is_set():
            if self.work_queue.unfinished_tasks == 0:
                break
            time.sleep(0.1)

        for worker in self.workers:
            worker.join()

        with self.state.lock:
            if not self.state.failed and self.state.completed_chunks == len(chunks):
                self.state.finished = True

        if self.stop_event.is_set() and not self.state.finished:
            return

        if self.status_callback:
            self.status_callback(f"Done: {self.output_path}")


# -----------------------------
# GUI
# -----------------------------

class DownloaderGUI:
    POLL_MS = 250
    SPEED_INTERVAL = 0.5

    def __init__(self, root, args):
        self.root = root
        self.root.title("Dual-Link Downloader")
        self.root.geometry("1100x760")
        self.root.minsize(900, 650)

        self.controller = None
        self.speed_thread = None
        self.speed_stop = threading.Event()
        self.last_error_count = 0

        self.url_var = tk.StringVar(value=args.url or "")
        self.output_var = tk.StringVar(value=args.output or "")
        self.chunk_var = tk.StringVar(value=str(args.chunk_size_mb))
        self.retry_var = tk.StringVar(value=str(args.max_retries))
        self.cookies_var = tk.StringVar(value=args.cookies or "")

        self.link_vars = [
            tk.StringVar(value=args.link1 or ""),
            tk.StringVar(value=args.link2 or ""),
            tk.StringVar(value=args.link3 or ""),
            tk.StringVar(value=args.link4 or ""),
        ]

        self.status_var = tk.StringVar(value="Ready")
        self.progress_var = tk.DoubleVar(value=0)

        self._build_ui()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        config = ttk.LabelFrame(outer, text="Download")
        config.pack(fill="x", pady=(0, 8))

        ttk.Label(config, text="URL:").grid(
            row=0, column=0, sticky="w", padx=5, pady=5
        )
        ttk.Entry(
            config,
            textvariable=self.url_var,
            width=95,
        ).grid(
            row=0, column=1, columnspan=4, sticky="ew", padx=5, pady=5
        )

        ttk.Label(config, text="Output:").grid(
            row=1, column=0, sticky="w", padx=5, pady=5
        )
        ttk.Entry(
            config,
            textvariable=self.output_var,
            width=65,
        ).grid(
            row=1, column=1, columnspan=3, sticky="ew", padx=5, pady=5
        )

        ttk.Label(config, text="Chunk MB:").grid(
            row=1, column=4, sticky="e", padx=5, pady=5
        )
        ttk.Entry(
            config,
            textvariable=self.chunk_var,
            width=8,
        ).grid(
            row=1, column=5, sticky="w", padx=5, pady=5
        )

        ttk.Label(config, text="Retries:").grid(
            row=2, column=4, sticky="e", padx=5, pady=5
        )
        ttk.Entry(
            config,
            textvariable=self.retry_var,
            width=8,
        ).grid(
            row=2, column=5, sticky="w", padx=5, pady=5
        )

        ttk.Label(config, text="Cookies:").grid(
            row=3, column=0, sticky="w", padx=5, pady=5
        )
        ttk.Entry(
            config,
            textvariable=self.cookies_var,
            width=95,
        ).grid(
            row=3, column=1, columnspan=5, sticky="ew", padx=5, pady=5
        )

        ttk.Label(config, text="Interface IPs:").grid(
            row=2, column=0, sticky="w", padx=5, pady=5
        )

        available_ips = get_available_ips()
        
        for i, var in enumerate(self.link_vars):
            # Using ttk.Combobox instead of ttk.Entry
            cb = ttk.Combobox(
                config,
                textvariable=var,
                values=available_ips,
                width=16,
            )
            cb.grid(
                row=2, column=i + 1, sticky="w", padx=5, pady=5
            )

        for col in range(6):
            config.columnconfigure(col, weight=1)

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(0, 8))

        self.start_button = ttk.Button(
            buttons,
            text="Start Download",
            command=self.start_download,
        )
        self.start_button.pack(side="left")

        self.stop_button = ttk.Button(
            buttons,
            text="Stop",
            command=self.stop_download,
            state="disabled",
        )
        self.stop_button.pack(side="left", padx=6)

        ttk.Label(
            buttons,
            textvariable=self.status_var,
        ).pack(side="left", padx=15)

        progress_frame = ttk.Frame(outer)
        progress_frame.pack(fill="x", pady=(0, 8))

        ttk.Progressbar(
            progress_frame,
            variable=self.progress_var,
            maximum=100,
            mode="determinate",
        ).pack(fill="x")

        self.progress_text = ttk.Label(
            progress_frame,
            text="0.0% | 0 B / 0 B | 0 B/s",
        )
        self.progress_text.pack(anchor="w", pady=(4, 0))

        allocation_frame = ttk.LabelFrame(
            outer,
            text="Live Chunk Allocation",
        )
        allocation_frame.pack(fill="x", pady=(0, 8))

        self.worker_tree = ttk.Treeview(
            allocation_frame,
            columns=(
                "worker",
                "ip",
                "chunk",
                "range",
                "status",
                "speed",
            ),
            show="headings",
            height=6,
        )

        headings = {
            "worker": "Worker",
            "ip": "Local IP",
            "chunk": "Current Chunk",
            "range": "Byte Range",
            "status": "Status",
            "speed": "Speed",
        }

        widths = {
            "worker": 90,
            "ip": 160,
            "chunk": 100,
            "range": 260,
            "status": 120,
            "speed": 120,
        }

        for col in self.worker_tree["columns"]:
            self.worker_tree.heading(col, text=headings[col])
            self.worker_tree.column(col, width=widths[col], anchor="center")

        self.worker_tree.pack(fill="x", padx=5, pady=5)

        self.log = tk.Text(
            outer,
            height=7,
            state="disabled",
            wrap="word",
        )
        self.log.pack(fill="x", pady=(0, 8))

        if FigureCanvasTkAgg is not None:
            graph_frame = ttk.LabelFrame(
                outer,
                text="Network Speed",
            )
            graph_frame.pack(fill="both", expand=True)

            self.figure = Figure(figsize=(10, 3.8), dpi=100)
            self.ax = self.figure.add_subplot(111)
            self.ax.set_title("Download speed")
            self.ax.set_xlabel("Time (s)")
            self.ax.set_ylabel("MB/s")
            self.ax.grid(True, alpha=0.25)

            self.canvas = FigureCanvasTkAgg(
                self.figure,
                master=graph_frame,
            )
            self.canvas.get_tk_widget().pack(
                fill="both",
                expand=True,
            )

    def log_message(self, message):
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def start_download(self):
        if self.controller and self.controller.is_alive():
            return

        url = self.url_var.get().strip()

        links = []
        for var in self.link_vars:
            val = var.get().strip()
            if val:
                # Split by space and take the first item to strip away the "(Interface, up/down)" text
                clean_ip = val.split()[0]
                links.append(clean_ip)

        if not url:
            messagebox.showerror("Missing URL", "Enter a download URL.")
            return

        if len(links) < 2:
            messagebox.showerror(
                "Missing interfaces",
                "Enter at least two local interface IP addresses.",
            )
            return

        try:
            chunk_mb = float(self.chunk_var.get())
            retry_count = int(self.retry_var.get())

            if chunk_mb <= 0:
                raise ValueError

            if retry_count < 0:
                raise ValueError

        except ValueError:
            messagebox.showerror(
                "Invalid settings",
                "Chunk MB must be > 0 and retries must be >= 0.",
            )
            return

        chunk_size = int(chunk_mb * 1024 * 1024)

        output = self.output_var.get().strip()
        cookies = parse_cookie_header(self.cookies_var.get().strip())

        # URL filename will be discovered after HEAD if output is empty.
        # Temporary placeholder is used only until HEAD completes.
        if not output:
            path_name = unquote(
                os.path.basename(urlparse(url).path)
            )
            output = path_name or "download"

        self.output_var.set(output)

        state = DownloadState(
            links=links,
            total_size=0,
            total_chunks=0,
        )

        self.controller = DownloadController(
            url=url,
            output_path=output,
            links=links,
            chunk_size=chunk_size,
            max_retries=retry_count,
            cookies=cookies,
            state=state,
            status_callback=self.status_var.set,
        )

        # Expose state to GUI.
        self.state = state

        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("Starting...")
        self.log_message(f"Starting download: {url}")
        self.log_message(f"Interfaces: {', '.join(links)}")
        self.log_message(f"Chunk size: {human(chunk_size)}")

        self.controller.start()

        self.speed_stop.clear()
        self.speed_thread = threading.Thread(
            target=self.speed_loop,
            daemon=True,
        )
        self.speed_thread.start()

        self.poll_gui()

    def speed_loop(self):
        previous = time.time()

        while not self.speed_stop.is_set():
            time.sleep(self.SPEED_INTERVAL)

            now = time.time()
            interval = max(now - previous, 0.05)
            previous = now

            if self.controller is not None:
                self.state.tick_speed(interval)

    def stop_download(self):
        if self.controller:
            self.controller.stop_event.set()
            self.status_var.set("Stopping...")
            self.log_message("Stop requested.")
        self.stop_button.configure(state="disabled")

    def poll_gui(self):
        if not hasattr(self, "state"):
            return

        snapshot = self.state.snapshot()

        total = snapshot["total_size"]
        done = snapshot["completed_bytes"]
        pct = (done / total * 100) if total else 0

        self.progress_var.set(pct)

        self.progress_text.configure(
            text=(
                f"{pct:.1f}% | "
                f"{human(done)} / {human(total)} | "
                f"{human_speed(snapshot['total_speed'])}"
            )
        )

        for item in self.worker_tree.get_children():
            self.worker_tree.delete(item)

        for worker_id, stats in enumerate(snapshot["links"]):
            chunk_id = stats["current_chunk"]

            if chunk_id is None:
                chunk_text = "-"
                range_text = "-"
                status = "Waiting"
            else:
                chunk_text = str(chunk_id + 1)

                start = stats["current_chunk_start"]
                end = stats["current_chunk_end"]

                range_text = f"{start:,} - {end:,}"

                status = "Downloading"

            self.worker_tree.insert(
                "",
                "end",
                values=(
                    f"Worker {worker_id + 1}",
                    stats["ip"],
                    chunk_text,
                    range_text,
                    status,
                    human_speed(stats["speed"]),
                ),
            )

        errors = snapshot["errors"]

        if len(errors) > self.last_error_count:
            for error in errors[self.last_error_count:]:
                self.log_message("ERROR: " + error)
            self.last_error_count = len(errors)

        self.update_graph(snapshot)

        if snapshot["finished"]:
            self.speed_stop.set()
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self.status_var.set(
                f"Complete: {self.output_var.get()}"
            )
            self.log_message("Download completed successfully.")
            return

        if snapshot["failed"]:
            self.speed_stop.set()
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self.status_var.set("Download failed.")
            return

        self.root.after(self.POLL_MS, self.poll_gui)

    def update_graph(self, snapshot):
        if FigureCanvasTkAgg is None:
            return

        history = snapshot["history"]

        self.ax.clear()
        self.ax.set_title("Network Speed")
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("MB/s")
        self.ax.grid(True, alpha=0.25)

        if history:
            t0 = history[0][0]
            times = [item[0] - t0 for item in history]

            for i, stats in enumerate(snapshot["links"]):
                speeds = [
                    h[1][i] / (1024 * 1024)
                    for h in history
                ]
                self.ax.plot(
                    times,
                    speeds,
                    label=f"IP {stats['ip']}",
                )

            combined = [
                h[2] / (1024 * 1024)
                for h in history
            ]

            self.ax.plot(
                times,
                combined,
                linewidth=2.5,
                label="Combined",
            )

            self.ax.legend(loc="upper left")

        self.canvas.draw_idle()

    def on_close(self):
        self.speed_stop.set()

        if self.controller:
            self.controller.stop_event.set()

        self.root.destroy()

def get_available_ips():
    ips = []
    if psutil is None:
        return ips
    
    # Grab the up/down stats for the interfaces
    stats = psutil.net_if_stats()
    
    for name, addrs in psutil.net_if_addrs().items():
        # Check if the interface is up
        up = stats[name].isup if name in stats else False
        status = "up" if up else "down"
        
        for addr in addrs:
            if getattr(addr.family, "name", "") == "AF_INET":
                # Append the IP along with the name and status
                ips.append(f"{addr.address} ({name}, {status})")
    return ips

# -----------------------------
# CLI entry
# -----------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "url",
        nargs="?",
        help="URL of the file to download",
    )

    parser.add_argument(
        "-o",
        "--output",
        help="Output filename",
    )

    parser.add_argument(
        "--list-interfaces",
        action="store_true",
        help="List local IPv4 interfaces and exit",
    )

    parser.add_argument(
        "--link1",
        help="Local IP of first connection",
    )

    parser.add_argument(
        "--link2",
        help="Local IP of second connection",
    )

    parser.add_argument(
        "--link3",
        help="Local IP of optional third connection",
    )

    parser.add_argument(
        "--link4",
        help="Local IP of optional fourth connection",
    )

    parser.add_argument(
        "--chunk-size-mb",
        type=float,
        default=8,
        help="Dynamic chunk size in MB (default: 8)",
    )

    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Maximum retries per chunk (default: 3)",
    )

    parser.add_argument(
        "--cookies",
        help="Cookie header string: 'name=value; name2=value2'",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    if args.list_interfaces:
        list_interfaces()
        return

    links = [
        ip
        for ip in [
            args.link1,
            args.link2,
            args.link3,
            args.link4,
        ]
        if ip
    ]

    if not args.url:
        # Open GUI even without a URL.
        if FigureCanvasTkAgg is None:
            print(
                "matplotlib is required for the GUI. "
                "Install with: pip install matplotlib"
            )
            return

        root = tk.Tk()
        DownloaderGUI(root, args)
        root.mainloop()
        return

    if len(links) < 2:
        raise SystemExit(
            "Provide at least --link1 and --link2, or run without a URL "
            "to use the GUI."
        )

    # CLI arguments still open the GUI with fields populated.
    if FigureCanvasTkAgg is None:
        raise SystemExit(
            "matplotlib is required for the GUI. "
            "Install with: pip install matplotlib"
        )

    root = tk.Tk()
    DownloaderGUI(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()
