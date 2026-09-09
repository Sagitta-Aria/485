"""Field experiment: hold G1 or the first coded group, then read at 1/3/5/10 s.

Run only with both slave matrices initially open and the GUI tests idle. Uses
manual SWITCH requests to reproduce the final mask, not the firmware's exact
GPIO timing. Restores both slave matrices to open after every run.
"""
import json
from pathlib import Path
import socket
import time
import uuid
from datetime import datetime


def main():
    """Compare point/group/point readings while preserving every request and reply."""
    directory = Path(__file__).resolve().parent
    output = directory / (datetime.now().strftime('settling_%Y%m%d_%H%M%S') + '.jsonl')
    peer = 'DIAG-' + uuid.uuid4().hex[:8]
    index = 0
    with output.open('x', encoding='utf-8') as log, socket.create_connection(('127.0.0.1', 3333), timeout=5) as connection:
        connection.settimeout(15)
        with connection.makefile('rb') as reader:
            def record(event, **fields):
                line = json.dumps(dict(time=datetime.now().astimezone().isoformat(), event=event, **fields))
                log.write(line + '\n')
                log.flush()
                if event in {'sample', 'phase', 'complete', 'cleanup_error'}:
                    print(line, flush=True)

            def request(target, command, expected=None):
                nonlocal index
                index += 1
                rid = f'SETTLE{index}'
                record('request', target=target, command=command)
                connection.sendall(f'SEND {peer} {target} {rid} {command}\n'.encode())
                acknowledged, reply = False, None
                while not acknowledged or reply is None:
                    line = reader.readline(8193).decode().strip()
                    record('receive', frame=line)
                    if line == f'OK FORWARDED {target} {rid}':
                        acknowledged = True
                    elif line.startswith(f'RESULT {target} {peer} {rid} '):
                        reply = line.split(' ', 4)[4]
                    else:
                        raise RuntimeError('Unexpected frame: ' + line)
                if expected is not None and reply != expected:
                    raise RuntimeError(f'{target} {command}: {reply}')
                return reply

            connection.sendall(f'HELLO NODE {peer}\n'.encode())
            if reader.readline().decode().strip() != f'OK REGISTERED {peer}':
                raise RuntimeError('Diagnostic client registration failed')
            opened = 'OK STATUS S1 ' + '0' * 30 + ' S2 ' + '0' * 30
            for master in ('master1', 'master2'):
                request(master, 'BUS slave1 STATUS', opened)
            request('master1', 'STATUS', 'OK STATUS S1 010000020000040000080000000000')
            request('master2', 'STATUS', 'OK STATUS S1 ' + '0' * 30)
            try:
                for phase, groups in [('point_before', [0]), ('coded_round_1', list(range(15))), ('point_after', [0])]:
                    for master in ('master1', 'master2'):
                        request(master, 'BUS slave1 RESET', 'OK RESET')
                    # Match the firmware's voltage-first/current-second group ordering.
                    for current in (False, True):
                        for group in groups:
                            bank = 'S1' if group < 12 else 'S2'
                            x = (group % 12) * 2 + (0 if current else 1)
                            y = 0 if current else 1
                            command = f'SWITCH {bank} {x} Y{y} ON'
                            request('master2', 'BUS slave1 ' + command, 'OK ' + command)
                    for command in ('SWITCH S1 1 Y2 ON', 'SWITCH S1 0 Y3 ON'):
                        request('master1', 'BUS slave1 ' + command, 'OK ' + command)
                    closed_at = time.monotonic()
                    record('phase', phase=phase, right_groups=[g+1 for g in groups])
                    for deadline in (1, 3, 5, 10):
                        time.sleep(max(0, closed_at + deadline - time.monotonic()))
                        requested_at = time.monotonic() - closed_at
                        reply = request('master1', 'MEASURE')
                        record('sample', phase=phase, planned_seconds=deadline,
                               requested_seconds=round(requested_at, 3),
                               received_seconds=round(time.monotonic()-closed_at, 3), reply=reply)
                    request('master1', 'BUS slave1 STATUS')
                    request('master2', 'BUS slave1 STATUS')
            finally:
                for master in ('master1', 'master2'):
                    try:
                        request(master, 'BUS slave1 RESET', 'OK RESET')
                        request(master, 'BUS slave1 STATUS', opened)
                    except Exception as error:
                        record('cleanup_error', target=master, error=str(error))
                        raise
            record('complete', log=str(output))


if __name__ == '__main__':
    main()
