"""Recover saved topology sessions through a temporary standalone local router.

Run after stopping the GUI server. This uses the same local controller identity
and recovery code as the GUI, preserves cached records, and probes slave1 after
cleanup. It does not flash firmware or start a measurement. --restore-gui opens
the updated GUI and starts its server after this command releases the port.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import queue
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cable_tester_gui import RouterService
from topology_scan import TopologyScanController


def main() -> None:
    """Own port 3333 only during recovery, then optionally restore the GUI server."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore-gui", action="store_true")
    args = parser.parse_args()
    log_dir = ROOT / "reports" / "manual_diagnostics"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / (datetime.now().strftime("recovery_%Y%m%d_%H%M%S") + ".jsonl")
    with log_path.open("x", encoding="utf-8") as log:
        def record(event: str, payload: object) -> None:
            """Save actual request/results so field recovery remains reviewable."""
            line = json.dumps({"time": datetime.now().astimezone().isoformat(),
                               "event": event, "payload": payload}, ensure_ascii=False)
            log.write(line + "\n")
            log.flush()
            print(line, flush=True)

        def result(target: str, request: str, payload: str) -> bool:
            """Forward correlated device results to the existing recovery controller."""
            record("result", {"target": target, "request": request, "reply": payload})
            return controller.feed_result(target, request, payload)

        router = RouterService(queue.Queue(), on_result=result)
        controller = TopologyScanController(router.send_from_controller, record,
                                            report_root=ROOT / "reports" / "topology")
        router.start("0.0.0.0", 3333)
        if not router.running:
            raise RuntimeError("Port 3333 unavailable; stop the GUI server first")
        try:
            deadline = time.monotonic() + 30
            while not {"master1", "master2"}.issubset(router.connected_ids()):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Waiting for master1 and master2 to reconnect")
                time.sleep(0.1)
            for master in ("master1", "master2"):
                info = controller._request_one(master, 1, "TOPO_INFO")
                if not info.startswith("OK TOPO_INFO "):
                    raise RuntimeError(info)
                capabilities = dict(field.split("=", 1) for field in info.split()[2:])
                controller._recover_previous_cache(master, capabilities)
                reply = controller._request_one(master, 1, "BUS slave1 PING")
                if reply != "OK PONG":
                    raise RuntimeError(f"{master}/slave1: {reply}")
                record("verified", f"{master}/slave1 RS485 OK; topology lock released")
            record("complete", str(log_path))
        finally:
            router.stop()
            if args.restore_gui:
                subprocess.Popen([sys.executable, "-c",
                    "import tkinter as tk; from cable_tester_gui import CableTesterApp; "
                    "root=tk.Tk(); app=CableTesterApp(root); app._start_server(); root.mainloop()"],
                    cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


if __name__ == "__main__":
    main()
