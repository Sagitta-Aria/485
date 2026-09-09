"""Run explicit diagnostic commands through the running GUI's local TCP router.

This does not own the GUI workflow lock: use only while automatic tests are idle.
Commands run once, sequentially; a failed or missing reply aborts the run without
retrying hardware mutations. Matrix connections remain as commanded afterwards.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import socket
import time
import uuid


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", action="append", required=True,
                        help="Explicit master1|COMMAND or master2|COMMAND")
    parser.add_argument("--label", required=True)
    parser.add_argument("--settle", type=float, default=1.2)
    args = parser.parse_args()
    requests = []
    for value in args.request:
        target, command = value.split("|", 1)
        if target not in {"master1", "master2"} or any(c in command for c in "\r\n"):
            parser.error("Invalid target or multiline command")
        local_command = command.split(" ", 2)[-1] if command.startswith("BUS slave1 ") else command
        if local_command not in {"STATUS", "RESET", "MEASURE", "PING"} and not local_command.startswith("SWITCH "):
            parser.error("Only slave1/local matrix diagnostics are supported")
        requests.append((target, command))
    if not 0 <= args.settle <= 10:
        parser.error("settle must be between 0 and 10 seconds")
    directory = Path(__file__).resolve().parents[1] / "reports" / "manual_diagnostics"
    directory.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    log_path = directory / (run_id + ".jsonl")
    peer = "DIAG-" + uuid.uuid4().hex[:8]
    print("LOG " + str(log_path), flush=True)
    with log_path.open("x", encoding="utf-8") as log:
        def record(event: str, **fields: object) -> None:
            entry = {"timestamp": datetime.now().astimezone().isoformat(),
                     "label": args.label, "event": event, **fields}
            line = json.dumps(entry, ensure_ascii=True)
            log.write(line + "\n")
            log.flush()
            print(line, flush=True)

        with socket.create_connection(("127.0.0.1", 3333), timeout=15) as connection:
            with connection.makefile("rb") as reader:
                def send(line: str) -> None:
                    record("send", frame=line)
                    connection.sendall((line + "\n").encode("utf-8"))

                def receive() -> str:
                    data = reader.readline(8193)
                    if not data or len(data) > 8192:
                        raise RuntimeError("Router closed or sent an oversized frame")
                    line = data.decode("utf-8").strip()
                    record("receive", frame=line)
                    return line

                try:
                    send(f"HELLO NODE {peer}")
                    if receive() != f"OK REGISTERED {peer}":
                        raise RuntimeError("Diagnostic client registration failed")
                    for index, (target, command) in enumerate(requests, 1):
                        if command == "MEASURE":
                            time.sleep(args.settle)
                        request_id = f"D{index}"
                        send(f"SEND {peer} {target} {request_id} {command}")
                        acknowledgement = False
                        payload = None
                        while not acknowledgement or payload is None:
                            line = receive()
                            if line == f"OK FORWARDED {target} {request_id}":
                                acknowledgement = True
                            elif line.startswith(f"RESULT {target} {peer} {request_id} "):
                                payload = line.split(" ", 4)[4]
                                if not payload.startswith("OK "):
                                    raise RuntimeError(payload)
                            else:
                                raise RuntimeError("Unexpected router frame: " + line)
                        record("result", target=target, command=command, payload=payload)
                        if payload.startswith("OK STATUS "):
                            fields = payload.split()
                            closed = {}
                            for bank, encoded in zip(fields[2::2], fields[3::2]):
                                raw = bytes.fromhex(encoded)
                                if len(raw) != 15:
                                    raise RuntimeError("Invalid matrix status length")
                                closed[bank] = [f"X{x}-Y{y}" for y in range(5) for x in range(24)
                                                if raw[(y * 24 + x) // 8] & (1 << ((y * 24 + x) % 8))]
                            record("matrix", target=target, command=command, closed=closed)
                except Exception as error:
                    record("error", detail=str(error))
                    raise


if __name__ == "__main__":
    main()
