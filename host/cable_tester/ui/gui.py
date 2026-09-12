"""线缆测试服务器上位机。

本程序同时承担两个职责：
1. 在电脑上监听 TCP 3333 端口，接收并管理多个 ESP32 节点。
2. 提供 Tkinter 图形界面，向指定设备发送测量命令并显示路由结果。

ESP32 必须先发送 ``HELLO ROLE <设备ID>`` 注册（ROLE 可为 MASTER 或 SLAVE）。界面发送命令时使用内置
控制端 ID ``GUI``，不需要再通过 127.0.0.1 连接一个独立服务器进程。
"""

from __future__ import annotations

import queue
import re
import socket
import socketserver
import threading
import time
import tkinter as tk
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from tkinter import messagebox, ttk

from cable_tester.worker.auxiliary_loop_test import AuxiliaryLoopTestController
from cable_tester.analysis.batch_calibration import BatchCalibrationController
from cable_tester.devices.calibration_store import (
    CalibrationCandidate,
    CalibrationProfileError,
    CalibrationStore,
    CorrectedMeasurement,
)
from cable_tester.worker.dual_node_measurement import DualNodeMeasurementController
from cable_tester.analysis.pairwise_resistance_test import PairwiseResistanceController
from cable_tester.worker.sequential_loop_test import SequentialLoopTestController
from cable_tester.worker.node_measurement import NodeMeasurementController, node_labels
from cable_tester.worker.all_matrix_reset import AllMatrixResetController
from cable_tester.analysis.topology_scan import TopologyScanController
from cable_tester.ui.topology_panel import TopologyPanel


DEFAULT_LISTEN_HOST = "0.0.0.0"  # 监听电脑所有 IPv4 网卡，包括热点和有线/无线网卡
DEFAULT_PORT = 3333               # 必须与 ESP32 的 WIFI_SERVER_TCP_PORT 一致
CONTROLLER_ID = "GUI"            # 上位机在路由协议中的固定源 ID
MASTER_WIFI_IDS = ("master1", "master2")
DEFAULT_MASTER_WIFI_ID = MASTER_WIFI_IDS[0]
RS485_SLAVE_IDS = tuple(f"slave{index}" for index in range(1, 11))
SCOPED_SLAVE_WIFI_IDS = tuple(
    f"m{side}-s{index}" for side in (1, 2) for index in range(1, 11)
)
# Legacy Wi-Fi IDs remain accepted while individual boards are being upgraded.
SLAVE_WIFI_IDS = SCOPED_SLAVE_WIFI_IDS + RS485_SLAVE_IDS
MODE_OPTIONS = ("master", "slave")
BROADCAST_TARGET = "broadcast"
MAX_LINE_LENGTH = 4096            # 电脑端允许接收的最大单行协议长度
MAX_ID_LENGTH = 31                # 与 ESP32 的 32 字节 ID 缓冲区保持一致，预留 '\0'
HEARTBEAT_TIMEOUT_SECONDS = 15.0  # ESP 每 2 秒发 PING；容忍短时 WiFi 延迟，持续静默才判离线
MAX_LOG_ENTRIES = 5000            # 日志只保留最近记录，避免长时间运行占满内存
LOG_LEVEL_LABELS = {
    "normal": "正常",
    "success": "成功",
    "warning": "警告",
    "error": "错误",
}
CONNECT_RESULT_PATTERN = re.compile(
    r"^OK CONNECT (?P<positive_bank>S[12]) (?P<positive_x>\d+) "
    r"(?P<negative_bank>S[12]) (?P<negative_x>\d+)$"
)
MEASURE_RESULT_PATTERN = re.compile(
    r"^OK MEASURE resistance=(?P<resistance>\d+(?:\.\d+)?) "
    r"raw=(?P<raw>\d+) range=(?P<range>\d+)$"
)
STATUS_HEX_LENGTH = ((24 * 5 + 7) // 8) * 2
MATRIX_CELL_TAG_PREFIX = "matrix-cell:"


def matrix_cell_tag(bank: str, x: int, y: int) -> str:
    """Encode one matrix coordinate as a private Tk text tag."""
    return f"{MATRIX_CELL_TAG_PREFIX}{bank}:{x}:{y}"


def parse_matrix_cell_tags(tags: tuple[str, ...]) -> tuple[str, int, int] | None:
    """Return the first valid matrix coordinate embedded in Tk text tags."""
    for tag in tags:
        if not tag.startswith(MATRIX_CELL_TAG_PREFIX):
            continue
        fields = tag[len(MATRIX_CELL_TAG_PREFIX):].split(":")
        if len(fields) != 3 or fields[0] not in {"S1", "S2"}:
            continue
        try:
            x, y = int(fields[1]), int(fields[2])
        except ValueError:
            continue
        if 0 <= x < 24 and 0 <= y < 5:
            return fields[0], x, y
    return None


def parse_status_payload(
    payload: str,
) -> dict[str, tuple[tuple[bool, ...], ...]] | None:
    """Decode ``OK STATUS S1 <hex> [S2 <hex>]`` into bank rows and columns."""
    fields = payload.strip().split()
    if (
        len(fields) < 4
        or (len(fields) - 2) % 2
        or fields[0].upper() != "OK"
        or fields[1].upper() != "STATUS"
    ):
        return None
    banks: dict[str, tuple[tuple[bool, ...], ...]] = {}
    for index in range(2, len(fields) - 1, 2):
        bank = fields[index].upper()
        encoded = fields[index + 1]
        if bank not in {"S1", "S2"} or len(encoded) != STATUS_HEX_LENGTH:
            return None
        try:
            raw = bytes.fromhex(encoded)
        except ValueError:
            return None
        if len(raw) != STATUS_HEX_LENGTH // 2:
            return None
        rows = []
        for y in range(5):
            rows.append(tuple(
                bool(raw[(y * 24 + x) // 8] & (1 << ((y * 24 + x) % 8)))
                for x in range(24)
            ))
        banks[bank] = tuple(rows)
    return banks or None


def classify_log_level(category: str, message: str) -> str:
    """Classify one complete log row as normal, success, warning, or error."""
    upper_message = message.upper()
    if (
        category == "错误"
        or re.search(r"(?:^|\s)ERR(?:\s|$)", upper_message)
        or "失败" in message
        or "异常" in message
    ):
        return "error"
    if (
        category == "警告"
        or "WARNING" in upper_message
        or "警告" in message
        or "超时" in message
        or "已断开" in message
        or "取消" in message
    ):
        return "warning"
    if is_normal_transport_log(category, message):
        return "normal"
    return "success"


def is_normal_transport_log(category: str, message: str) -> bool:
    """Return whether a row is routine transport chatter safe to hide by default.

    This only identifies protocol traffic that does not contain a measurement or
    hardware failure. It must not be used to discard records from automated test
    controllers, which still receive every RESULT frame for their own state.
    """
    if category == "发送":
        return True
    if category != "接收":
        return False

    stripped = message.strip().upper()
    if stripped.startswith(("OK FORWARDED ", "FROM ")):
        return True
    return bool(
        re.search(
            r"(?:^|\s)OK (?:PONG|RESET|SWITCH)(?:\s|$)",
            stripped,
        )
    )


@dataclass(frozen=True)
class LogEntry:
    """One rendered log record kept so the view can be filtered or redrawn."""

    timestamp: str
    category: str
    message: str
    level: str


def parse_connect_result(payload: str) -> tuple[str, str] | None:
    """Return the two independently calibrated port labels from OK CONNECT."""
    match = CONNECT_RESULT_PATTERN.fullmatch(payload.strip())
    if match is None:
        return None
    positive_x = int(match.group("positive_x"))
    negative_x = int(match.group("negative_x"))
    if not 0 <= positive_x < 24 or not 0 <= negative_x < 24:
        return None
    return (
        f"{match.group('positive_bank')}_X{positive_x}",
        f"{match.group('negative_bank')}_X{negative_x}",
    )


def format_calibrated_measurement(
    payload: str, correction: CorrectedMeasurement
) -> str:
    """Replace displayed resistance while retaining raw and endpoint details."""
    match = MEASURE_RESULT_PATTERN.fullmatch(payload.strip())
    if match is None:
        return payload
    return (
        f"OK MEASURE resistance={correction.corrected_ohm:.3f} calibrated=1 "
        f"raw_resistance={correction.raw_ohm:.3f} "
        f"positive={correction.positive_port}:{correction.positive_offset_ohm:.3f} "
        f"negative={correction.negative_port}:{correction.negative_offset_ohm:.3f} "
        f"raw={match.group('raw')} range={match.group('range')}"
    )


def master_fixed_route_error(target_id: str, command: str) -> str | None:
    """Protect master1 instrument pins for GUI and network clients before routing.

    BUS commands address slave matrices and keep their own policy. OFF remains
    available on nonfixed contacts so older firmware's residual bridges can clear.
    """
    if target_id != "master1":
        return None
    fields = command.upper().split()
    forbidden = False
    try:
        if len(fields) == 5 and fields[0] == "SWITCH" and fields[1] == "S1":
            x = int(fields[2])
            if 0 <= x < 4 and fields[3] in {"Y0", "Y1", "Y2", "Y3", "Y4"}:
                fixed = fields[3] == f"Y{x}"
                forbidden = (fixed and fields[4] == "OFF") or (not fixed and fields[4] == "ON")
        elif len(fields) == 5 and fields[0] == "CONNECT":
            forbidden = any(fields[bank] == "S1" and 0 <= int(fields[bank + 1]) < 4
                            for bank in (1, 3))
    except ValueError:
        return None  # Let firmware report malformed arguments.
    if forbidden:
        return "ERR FIXED_KELVIN master1 S1 X0~X3 为固定仪表端，不能改接或断开"
    return None


@dataclass
class Peer:
    """保存一个已经注册的 TCP 节点及其发送锁。

    该对象只表示网络连接，不拥有 ESP32 的业务状态。多个路由线程可能同时向
    同一节点发送数据，因此所有 sendall() 必须通过 send_line() 串行化。
    """

    peer_id: str
    role: str
    connection: socket.socket
    address: tuple[str, int]
    send_lock: threading.Lock = field(default_factory=threading.Lock)
    last_rx_at: float = field(default_factory=time.monotonic)
    last_rx_frame: str = "HELLO"

    def send_line(self, line: str) -> None:
        """发送一条自动添加换行符的 UTF-8 协议帧；失败时抛出 OSError。"""
        data = (line + "\n").encode("utf-8")
        with self.send_lock:
            self.connection.sendall(data)

    def close(self) -> None:
        """停止当前连接的收发并释放 socket；重复关闭产生的错误会被忽略。"""
        try:
            self.connection.shutdown(socket.SHUT_RDWR)  #停止通信  try发生报错会导致程序跳到except块中执行 
        except OSError:
            pass  #表示忽略这个错误，继续往下执行
        try:
            self.connection.close()  #关闭本地socket，释放相关资源  两次try-except是为了确保即使在关闭过程中发生错误，也不会导致程序崩溃
        except OSError:
            pass


class RouterState:
    """维护“设备 ID -> TCP 连接”映射并执行消息转发。

    每个客户端由独立线程处理，因此设备表由互斥锁保护。网络线程不能直接操作
    Tkinter 控件。可选控制器回调直接接收结果，其余状态交给 GUI 事件队列。
    """

    def __init__(
        self,
        events: queue.Queue[tuple[str, object]],
        *,
        on_result: Callable[[str, str, str], bool] | None = None,
        on_transport: Callable[[dict], None] | None = None,
    ) -> None:
        """Create routes; callbacks must be quick, thread-safe and avoid Tk widgets."""
        self._events = events
        self._on_result = on_result
        self._on_transport = on_transport
        self._lock = threading.Lock()
        self._peers: dict[str, Peer] = {}

    def register(self, peer: Peer) -> bool:
        """注册唯一设备 ID；重复 ID 返回 False，不替换已有在线设备。"""
        with self._lock:
            if peer.peer_id in self._peers:
                return False
            self._peers[peer.peer_id] = peer
            changed_at = time.monotonic()
        self._transport(
            "connected", peer, monotonic=changed_at,
            heartbeat_timeout_seconds=HEARTBEAT_TIMEOUT_SECONDS,
        )
        self._publish_devices()  #publish：发送，发布  
        self._events.put(  # #将消息放入队列
            ("network", f"{peer.role} {peer.peer_id} 已连接，地址={peer.address[0]}:{peer.address[1]}")
        )
        return True

    def unregister(
        self, peer: Peer, *, reason: str = "peer_closed", detail: str = ""
    ) -> None:
        """仅在 peer 仍拥有该 ID 时注销，防止旧线程删除后来的新连接。"""
        removed = False   #注销标志位
        with self._lock:
            if self._peers.get(peer.peer_id) is peer:
                del self._peers[peer.peer_id]
                removed = True
                changed_at = time.monotonic()
        if removed:
            self._disconnected(peer, reason, detail, changed_at)
            self._publish_devices()  #发布当前的在线设备列表
            self._events.put(("network", f"{peer.peer_id} 已断开"))

    def get(self, peer_id: str) -> Peer | None:
        """按 ID 查询当前在线连接；返回对象后发送仍可能因瞬时断线失败。"""
        with self._lock:
            return self._peers.get(peer_id)

    def connected_ids(self) -> tuple[str, ...]:
        """返回排序后的在线 ID 快照，用于刷新界面的目标设备列表。"""
        with self._lock:
            return tuple(sorted(self._peers))

    def close_all(self) -> None:
        """停止服务器时清空设备表并关闭所有节点连接。"""
        with self._lock:
            peers = tuple(self._peers.values())
            self._peers.clear()  #先清空设备表，设备表清空之后再有socket连接时，访问将返回False
            changed_at = time.monotonic()
        for peer in peers:
            self._disconnected(peer, "server_stopped", "", changed_at)
            peer.close()
        self._publish_devices()

    def route_send(   
        self,
        source: Peer,
        source_id: str,
        target_id: str,
        request_id: str,
        payload: str,
    ) -> str:
        """转发 ESP 发来的 SEND，并返回应答给源设备。

        source_id 必须与该 socket 注册 ID 一致，防止节点冒充其他设备。返回
        OK FORWARDED 只说明数据已写入目标 TCP，不表示目标业务执行成功。
        """
        if source_id != source.peer_id:
            return f"ERR SOURCE_ID_MISMATCH {source.peer_id}"
        self._transport(
            "request", source, source_id=source_id, target_id=target_id,
            request_id=request_id, payload=payload,
        )

        # ESP 可以把 GUI 作为目标，消息会直接进入上位机通信日志。
        if target_id == CONTROLLER_ID:
            self._events.put(
                ("received", f"FROM {source_id} {request_id} {payload}")
            )
            return f"OK FORWARDED {target_id} {request_id}"

        target = self.get(target_id)
        if target is None:
            return f"ERR TARGET_NOT_CONNECTED {target_id} {request_id}"

        route_error = master_fixed_route_error(target_id, payload)
        if route_error is not None:
            return route_error
        try:
            target.send_line(f"FROM {source_id} {request_id} {payload}")
        except OSError as error:
            self.unregister(target, reason="delivery_failed", detail=str(error))
            target.close()
            return f"ERR DELIVERY_FAILED {target_id} {request_id}"
        return f"OK FORWARDED {target_id} {request_id}"

    def route_result(
        self,
        target: Peer,
        target_id: str,
        source_id: str,
        request_id: str,
        payload: str,
    ) -> str | None:
        """把目标 ESP 的 RESULT 转发给原请求者。

        GUI 结果先交给线程安全的控制器回调，未被接收的结果进入 GUI 队列。
        节点间请求写入源 ESP 的 socket。成功返回 None，失败返回协议错误。
        """
        if target_id != target.peer_id:
            return f"ERR RESULT_ID_MISMATCH {target.peer_id}"
        self._transport(
            "result", target, source_id=source_id, target_id=target_id,
            request_id=request_id, payload=payload,
        )

        if source_id == CONTROLLER_ID:
            if self._on_result is not None:
                try:
                    if self._on_result(target_id, request_id, payload):
                        return None
                except Exception as error:
                    self._events.put(("error", f"Result callback failed: {error}"))
            self._events.put(("result_frame", (target_id, request_id, payload)))
            return None

        source = self.get(source_id)
        if source is None:
            return f"ERR SOURCE_NOT_CONNECTED {source_id} {request_id}"

        try:
            source.send_line(
                f"RESULT {target_id} {source_id} {request_id} {payload}"
            )
        except OSError as error:
            self.unregister(source, reason="delivery_failed", detail=str(error))
            source.close()
            return f"ERR DELIVERY_FAILED {source_id} {request_id}"
        return None

    def send_from_controller(
        self, target_id: str, request_id: str, payload: str
    ) -> str:
        """由内嵌 GUI 直接向目标节点发送 FROM 帧并返回服务器级回执。"""
        target = self.get(target_id)
        self._transport(
            "request", target, source_id=CONTROLLER_ID, target_id=target_id,
            request_id=request_id, payload=payload,
        )
        if target is None:
            return f"ERR TARGET_NOT_CONNECTED {target_id} {request_id}"
        route_error = master_fixed_route_error(target_id, payload)
        if route_error is not None:
            return route_error
        try:
            target.send_line(f"FROM {CONTROLLER_ID} {request_id} {payload}")
        except OSError as error:
            self.unregister(target, reason="delivery_failed", detail=str(error))
            target.close()
            return f"ERR DELIVERY_FAILED {target_id} {request_id}"
        return f"OK FORWARDED {target_id} {request_id}"

    def record_received(self, peer: Peer, line: str) -> None:
        """Track complete frames and expose PING timing to scan diagnostics."""
        peer.last_rx_at = time.monotonic()
        peer.last_rx_frame = line.split(maxsplit=1)[0].upper()
        if line.upper() == "PING":
            self._transport("heartbeat", peer, monotonic=peer.last_rx_at)

    def _disconnected(
        self, peer: Peer, reason: str, detail: str, changed_at: float
    ) -> None:
        """Report the first removal reason with the last complete received frame."""
        self._transport(
            "disconnected", peer, monotonic=changed_at, reason=reason, detail=detail,
            last_rx_age_seconds=max(0.0, changed_at - peer.last_rx_at),
            last_rx_frame=peer.last_rx_frame,
        )

    def _transport(self, event: str, peer: Peer | None = None, **fields: object) -> None:
        """Publish transport facts outside route locks; diagnostics cannot break routing."""
        if self._on_transport is None:
            return
        record = {
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "monotonic": time.monotonic(),
        }
        if peer is not None:
            record.update(peer_id=peer.peer_id, address=peer.address)
        record.update(fields)
        try:
            self._on_transport(record)
        except Exception as error:
            self._events.put(("error", f"Transport callback failed: {error}"))

    def _publish_devices(self) -> None:
        """把在线 ID 快照送入 GUI 队列；不得从网络线程直接修改控件。"""
        self._events.put(("devices", self.connected_ids())) 


class RouterRequestHandler(socketserver.StreamRequestHandler):
    """处理一个 TCP 客户端的注册和逐行协议帧。

    第一行必须是 HELLO ROLE ID，后续支持 SEND、RESULT 和 PING。该处理器运行在服务器
    创建的独立线程内，不能直接访问 Tkinter 控件。
    """

    def handle(self) -> None:
        """完成注册并持续读取协议帧，连接结束时自动注销设备。"""
        self.request.settimeout(10.0)  # 客户端连接后必须在 10 秒内发送 HELLO
        #self.request 是刚刚连接进来的客户端 Socket
        peer: Peer | None = None  #创建局部变量 peer，初始值是 None，表示客户端目前还没有完成注册
        state = self._state()  #取得这个服务器共同使用的 RouterState  其中保存state._peers state._events
        disconnect_reason = "peer_closed"
        disconnect_detail = ""
        reading = True
        try:
            first_line = self.rfile.readline(MAX_LINE_LENGTH + 1)
            reading = False
            #readline() 会等待并读取一行，如：收到换行符，达到读取长度，或超时。MAX_LINE_LENGTH + 1 是为了检测是否超过最大长度。
            if not first_line or len(first_line) > MAX_LINE_LENGTH:  #not first_line 表示没有收到数据
                return
            peer = self._register(first_line.decode("utf-8").strip()) #注册成功返回一个peer实例
            if peer is None:  #检查是否注册成功
                return

            # The timeout limits receive inactivity, including fragmented TCP frames.
            # Complete-frame times are tracked separately for scan diagnostics.
            self.request.settimeout(HEARTBEAT_TIMEOUT_SECONDS)
            while True:
                reading = True
                raw_line = self.rfile.readline(MAX_LINE_LENGTH + 1)
                reading = False
                if not raw_line:
                    break
                if len(raw_line) > MAX_LINE_LENGTH:
                    disconnect_reason = "line_too_long"
                    peer.send_line("ERR LINE_TOO_LONG")
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:  #如果处理后的字符串不是空字符串，就交给 _handle_line()
                    state.record_received(peer, line)
                    self._handle_line(peer, line)
        except TimeoutError as error:
            disconnect_reason = "heartbeat_timeout" if reading else "socket_error"
            disconnect_detail = str(error)
            reason_text = "心跳超时" if reading else "连接发送超时"
            state._events.put(
                (
                    "network",
                    f"{peer.peer_id if peer else self.client_address} {reason_text}，连接已移除",
                )
            )
        except (ConnectionError, OSError, UnicodeError) as error:
            disconnect_reason = "socket_error"
            disconnect_detail = str(error)
            state._events.put(
                ("network", f"{peer.peer_id if peer else self.client_address} 连接异常：{error}")
            )
        finally:  #循环结束，删除 peer 对象并关闭 socket，确保资源释放
            if peer is not None:
                state.unregister(peer, reason=disconnect_reason, detail=disconnect_detail)

    def _register(self, line: str) -> Peer | None:
        """解析 HELLO ROLE ID，校验角色与 ID 后加入共享设备表。"""
        fields = line.split(maxsplit=2)  #拆分消息 maxsplit=2表示最多切割两次
        if len(fields) != 3 or fields[0].upper() != "HELLO":  #.upper() 会转换成大写，所以大小写都能识别
            self.request.sendall(b"ERR EXPECTED_HELLO\n")
            return None

        role, peer_id = fields[1].upper(), fields[2]
        # GUI 是本进程的保留 ID；网络连接只允许注册真实 ESP 节点。
        role_id_matches = (
            role == "NODE"
            or (role == "MASTER" and peer_id in MASTER_WIFI_IDS)
            or (role == "SLAVE" and peer_id in SLAVE_WIFI_IDS)
        )
        if (
            not role_id_matches
            or peer_id == CONTROLLER_ID  #抢占服务器id
            or not self._valid_token(peer_id)  #检查peer_id是否有效
        ):
            self.request.sendall(b"ERR INVALID_ID\n")
            return None

        peer = Peer(peer_id, role, self.request, self.client_address)  #创建一个Peer对象，保存客户端的ID、角色、socket连接和地址
        if not self._state().register(peer):
            peer.send_line(f"ERR ID_IN_USE {peer_id}")
            return None
        try:
            peer.send_line(f"OK REGISTERED {peer_id}")
        except OSError as error:
            self._state().unregister(peer, reason="socket_error", detail=str(error))
            raise
        return peer

    def _handle_line(self, peer: Peer, line: str) -> None:
        """解析注册后的 SEND/RESULT/PING 帧并调用共享路由状态。"""
        fields = line.split(maxsplit=4)  # 第五项保留载荷中的所有后续空格
        command = fields[0].upper()
        state = self._state()

        #SEND 来源ID 目标ID 请求ID 载荷
        if command == "SEND" and len(fields) == 5:
            source_id, target_id, request_id, payload = fields[1:]  #fields[1:] 表示从下标 1 开始，取出后面的所有元素
            if not all(  #all表示是否全部为真
                self._valid_token(value)  #self._valid_token(value)会检查每个值是否有效，返回True或False
                for value in (source_id, target_id, request_id) 
            ):
                peer.send_line("ERR INVALID_SEND")
                return
            peer.send_line(  #send_line()将route_send()的返回值发送回源设备，表示消息已被转发或出现错误
                state.route_send( #route_send()方法会将消息从源设备转发到目标设备，并返回一个应答字符串
                    peer, source_id, target_id, request_id, payload
                )
            )
            return

        if command == "RESULT" and len(fields) == 5:
            #RESULT 执行设备ID 原请求者ID 请求ID 执行结果
            target_id, source_id, request_id, payload = fields[1:]
            if not all(
                self._valid_token(value)
                for value in (target_id, source_id, request_id)
            ):
                peer.send_line("ERR INVALID_RESULT")
                return
            error = state.route_result(
                peer, target_id, source_id, request_id, payload
            )
            if error is not None:
                peer.send_line(error)
            return

        if command == "PING" and len(fields) == 1:
            peer.send_line("OK PONG")
            return
        peer.send_line("ERR UNKNOWN_COMMAND")

    def _state(self) -> RouterState:
        """取得当前 RouterServer 拥有的共享状态。"""
        return self.server.state  # type: ignore[attr-defined]

    @staticmethod   #表示它是静态方法，不需要使用当前实例 self
    def _valid_token(value: str) -> bool:
        """检查协议 ID 字段非空、不超长且不包含任何空白字符。"""
        return (
            0 < len(value) <= MAX_ID_LENGTH
            and not any(character.isspace() for character in value)  #isspace() 方法用于检查字符串中的字符是否为空白字符（如空格、制表符、换行符等）。如果字符串中包含任何空白字符，any() 函数会返回 True，not any(...) 则会返回 False，表示该字符串不符合要求。
        )


class RouterServer(socketserver.ThreadingTCPServer):
    """支持多 ESP 并发连接的线程式 TCP 路由服务器。"""

    allow_reuse_address = True  # 上位机停止后允许尽快重新绑定相同端口
    daemon_threads = True       # 主程序退出时不等待残留客户端线程

    def __init__(
        self,
        address: tuple[str, int],
        events: queue.Queue[tuple[str, object]],
        *,
        on_result: Callable[[str, str, str], bool] | None = None,
        on_transport: Callable[[dict], None] | None = None,
    ) -> None:
        """绑定监听地址并创建属于本服务器实例的设备注册表。"""
        self.state = RouterState(events, on_result=on_result, on_transport=on_transport)
        super().__init__(address, RouterRequestHandler)  #调用父类的构造函数，传入监听地址和请求处理器类


class RouterService:
    """管理 GUI 内嵌路由服务器的启动、停止和控制端发送。

    Tkinter 主线程通过此类管理服务器生命周期。serve_forever() 在后台线程中
    运行；stop() 必须从非服务器线程调用，因为 shutdown() 会等待监听循环退出。
    """

    def __init__(
        self,
        events: queue.Queue[tuple[str, object]],
        *,
        on_result: Callable[[str, str, str], bool] | None = None,
        on_transport: Callable[[dict], None] | None = None,
    ) -> None:
        """保存 UI 事件队列；初始状态下尚未创建监听 socket。"""
        self._events = events
        self._on_result = on_result
        self._on_transport = on_transport
        self._lock = threading.Lock()
        self._server: RouterServer | None = None
        self._thread: threading.Thread | None = None  #保存运行 serve_forever() 的后台线程

    @property   #把一个方法变成“像属性一样访问”
    def running(self) -> bool:    #检查服务器当前是否被认为处于启动状态
        """线程安全地返回服务器是否已经成功绑定并启动。"""
        with self._lock:
            return self._server is not None

    def start(self, host: str, port: int) -> None:
        """绑定 host:port 并启动后台监听；错误通过 UI 事件队列报告。"""
        with self._lock:
            if self._server is not None:
                self._events.put(("error", "服务器已经启动"))
                return

        try:
            server = RouterServer(
                (host, port), self._events,
                on_result=self._on_result, on_transport=self._on_transport,
            )
        except OSError as error:
            self._events.put(("error", f"服务器启动失败：{error}"))
            self._events.put(("state", "stopped"))
            return

        thread = threading.Thread(
            target=server.serve_forever, #指定线程启动后要执行的方法->函数没有括号
            daemon=True,  #主程序退出时不必等待这个线程结束
            name="cable-router-server",
        )
        with self._lock:
            self._server = server
            self._thread = thread
        thread.start()  #启动线程
        actual_host, actual_port = server.server_address  #取得服务器最终绑定的地址
        self._events.put(("state", f"running:{actual_host}:{actual_port}"))

    def stop(self) -> None:
        """停止监听、断开全部 ESP 并释放端口；可以安全地重复调用。"""
        with self._lock:
            server = self._server
            self._server = None
            self._thread = None
        if server is None:
            self._events.put(("state", "stopped"))
            return

        server.shutdown()
        server.state.close_all()
        server.server_close()
        self._events.put(("state", "stopped"))

    def send_from_controller(  #GUI 控制端向 ESP32 发送消息，先检查自己的服务器是否启动，启动成功则通过设备查找进行发送
        self, target_id: str, request_id: str, payload: str
    ) -> str:
        """直接通过内嵌设备表发送 GUI 命令，不创建本机回环 TCP 连接。"""
        with self._lock:
            server = self._server
        if server is None:
            return "ERR SERVER_NOT_RUNNING"
        return server.state.send_from_controller(target_id, request_id, payload)

    def connected_ids(self) -> tuple[str, ...]:
        """Capture live Wi-Fi targets for a reset sweep independently of GUI mode."""
        with self._lock:
            server = self._server
        return server.state.connected_ids() if server is not None else ()


class CableTesterApp:
    """显示服务器状态、在线设备、硬件操作控件和通信日志。"""

    def __init__(self, root: tk.Tk) -> None:
        """创建界面状态和内嵌路由服务；服务器由用户点击按钮启动。"""
        self.root = root
        self.root.title("线缆矩阵测试 · WiFi / RS485")
        self.root.geometry("1180x760")
        self.root.minsize(980, 680)

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.router = RouterService(
            self.events,
            on_result=lambda target_id, request_id, payload: self.topology_scan.feed_result(
                target_id, request_id, payload
            ),
            on_transport=lambda event: self.topology_scan.feed_transport(event),
        )
        self.calibration_store = CalibrationStore()
        self.batch_calibration = BatchCalibrationController(
            self._send_via_mode,
            lambda event_type, message: self.events.put((event_type, message)),
        )
        self.auxiliary_loop_test = AuxiliaryLoopTestController(
            self._send_via_mode,
            lambda event_type, message: self.events.put((event_type, message)),
        )
        self.sequential_loop_test = SequentialLoopTestController(
            self._send_via_mode,
            lambda event_type, message: self.events.put((event_type, message)),
        )
        self.pairwise_resistance_test = PairwiseResistanceController(
            self._send_via_mode,
            lambda event_type, message: self.events.put((event_type, message)),
        )
        self.dual_node_measurement = DualNodeMeasurementController(
            self._send_via_mode,
            lambda event_type, message: self.events.put((event_type, message)),
        )
        self.node_measurement = NodeMeasurementController(
            self.router.send_from_controller,
            lambda event_type, message: self.events.put((event_type, message)),
        )
        self.all_matrix_reset = AllMatrixResetController(
            self.router.send_from_controller,
            lambda event_type, message: self.events.put((event_type, message)),
        )
        self._manual_operation = ""
        self.topology_scan = TopologyScanController(
            self.router.send_from_controller,
            lambda event_type, message: self.events.put((event_type, message)),
        )
        self.topology_panel: TopologyPanel | None = None
        self._topology_owned = False
        self._shutdown_pending = False
        self._closing = False
        self._pending_commands: dict[tuple[str, str], str] = {}
        self._request_routes: dict[tuple[str, str], str] = {}
        self._active_routes: dict[str, tuple[str, str]] = {}
        self._online_slave_ids: tuple[str, ...] = ()
        self._online_master_ids: tuple[str, ...] = ()

        self.host = tk.StringVar(value=DEFAULT_LISTEN_HOST)
        self.port = tk.StringVar(value=str(DEFAULT_PORT))
        self.mode = tk.StringVar(value="master")
        self._active_mode = "master"
        self.master_id = tk.StringVar(value=DEFAULT_MASTER_WIFI_ID)
        self.target_id = tk.StringVar(value="slave1")
        self.status = tk.StringVar(value="服务器未启动")
        self.online_devices = tk.StringVar(value="在线设备：无")
        self.batch_status = tk.StringVar(value="批量校准：未运行")
        self.auxiliary_test_status = tk.StringVar(value="辅助回路测试：未运行")
        self.sequential_test_status = tk.StringVar(value="Y0顺序测试：未运行")
        self.pairwise_test_status = tk.StringVar(value="全引脚阻值：未运行")
        self.dual_node_status = tk.StringVar(value="双节点四线闭合：未运行")
        self.node_measurement_status = tk.StringVar(value="节点测量：未运行")
        self.all_reset_status = tk.StringVar(value="全矩阵复位：未运行")
        self.calibration_status = tk.StringVar(
            value=f"显示校准：{DEFAULT_MASTER_WIFI_ID}-slave1 未加载"
        )
        self.calibration_choice = tk.StringVar()
        self.calibration_enabled = tk.BooleanVar(value=False)
        self._calibration_candidates: dict[str, CalibrationCandidate] = {}
        self._refreshing_calibration_controls = False
        self.positive_bank = tk.StringVar(value="S1")
        self.positive_x = tk.StringVar(value="0")
        self.negative_bank = tk.StringVar(value="S2")
        self.negative_x = tk.StringVar(value="0")
        self.dual_positive_bank = tk.StringVar(value="S1")
        self.dual_positive_x1 = tk.StringVar(value="0")
        self.dual_positive_x2 = tk.StringVar(value="1")
        self.dual_negative_bank = tk.StringVar(value="S2")
        self.dual_negative_x1 = tk.StringVar(value="0")
        self.dual_negative_x2 = tk.StringVar(value="1")
        self.node_left_master = tk.StringVar(value=DEFAULT_MASTER_WIFI_ID)
        self.node_right_master = tk.StringVar(value="master2")
        self.node_left_modules = tk.StringVar(value="1")
        self.node_right_modules = tk.StringVar(value="1")
        self.node_left = tk.StringVar(value="slave1-G1")
        self.node_right = tk.StringVar(value="slave1-G1")
        self.node_settle_seconds = tk.StringVar(value="0.1")
        self.switch_bank = tk.StringVar(value="S1")
        self.switch_x = tk.StringVar(value="0")
        self.switch_bus = tk.StringVar(value="Y4")
        self.custom_command = tk.StringVar()
        self.request_counter = 0
        self.filter_normal_logs = tk.BooleanVar(value=True)
        self.log_paused = tk.BooleanVar(value=False)
        self.log_status = tk.StringVar(value="实时显示")
        self._log_entries: deque[LogEntry] = deque(maxlen=MAX_LOG_ENTRIES)
        self._paused_log_count = 0
        self.matrix_status_target = tk.StringVar(value=DEFAULT_MASTER_WIFI_ID)
        self.matrix_status_summary = tk.StringVar(value="尚未查询矩阵状态")
        self._matrix_status: dict[
            str, tuple[str, dict[str, tuple[tuple[bool, ...], ...]]]
        ] = {}
        self._status_views: dict[str, tk.Text] = {}
        self._matrix_selected_cell: tuple[str, str, int, int] | None = None

        self._configure_style()
        self._build_interface()
        for variable in (self.node_left_modules, self.node_right_modules):
            variable.trace_add("write", self._refresh_node_selectors)
        self._refresh_node_selectors()
        self.mode.trace_add("write", self._mode_changed)
        self.master_id.trace_add("write", self._master_changed)
        self.target_id.trace_add("write", self._target_changed)
        self._apply_mode_state()
        self._refresh_calibration_status()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(50, self._poll_events)

    def _configure_style(self) -> None:
        """应用系统主题并定义标题、分区和状态标签样式。"""
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Status.TLabel", foreground="#9A3412")

    def _build_interface(self) -> None:
        """构建控制台和矩阵状态页。"""
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        notebook = ttk.Notebook(self.root)
        notebook.grid(row=0, column=0, sticky="nsew")
        self.notebook = notebook
        content = ttk.Frame(notebook, padding=16)
        status_page = ttk.Frame(notebook, padding=16)
        debug_page = ttk.Frame(notebook, padding=16)
        notebook.add(content, text="控制台")
        notebook.add(debug_page, text="调试")
        notebook.add(status_page, text="矩阵状态")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(7, weight=1)

        ttk.Label(content, text="线缆矩阵测试", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )

        server_controls = ttk.Frame(content)
        server_controls.grid(row=1, column=0, sticky="ew", pady=(14, 10))
        server_controls.columnconfigure(1, weight=1)
        ttk.Label(server_controls, text="监听地址").grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(server_controls, textvariable=self.host, width=16).grid(
            row=0, column=1, sticky="ew", padx=(0, 8)
        )
        ttk.Label(server_controls, text="端口").grid(row=0, column=2, padx=(0, 8))
        ttk.Entry(server_controls, textvariable=self.port, width=8).grid(
            row=0, column=3, padx=(0, 12)
        )
        ttk.Label(server_controls, text="工作模式").grid(row=0, column=4, padx=(0, 8))
        self.mode_selector = ttk.Combobox(
            server_controls,
            textvariable=self.mode,
            values=MODE_OPTIONS,
            state="readonly",
            width=8,
        )
        self.mode_selector.grid(row=0, column=5, padx=(0, 12))
        ttk.Label(server_controls, text="主机ID").grid(row=0, column=6, padx=(0, 8))
        self.master_selector = ttk.Combobox(
            server_controls,
            textvariable=self.master_id,
            values=MASTER_WIFI_IDS,
            state="readonly",
            width=8,
        )
        self.master_selector.grid(row=0, column=7, padx=(0, 12))
        ttk.Label(server_controls, text="矩阵目标").grid(row=0, column=8, padx=(0, 8))
        self.target_selector = ttk.Combobox(
            server_controls,
            textvariable=self.target_id,
            values=self._available_target_options(),
            width=10,
            state="readonly",
        )
        self.target_selector.grid(row=0, column=9, padx=(0, 12))
        ttk.Button(server_controls, text="启动服务器", command=self._start_server).grid(
            row=0, column=10, padx=(0, 6)
        )
        ttk.Button(server_controls, text="停止", command=self._stop_server).grid(
            row=0, column=11
        )

        status_area = ttk.Frame(content)
        status_area.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        status_area.columnconfigure(1, weight=1)
        self.status_label = ttk.Label(
            status_area, textvariable=self.status, style="Status.TLabel"
        )
        self.status_label.grid(row=0, column=0, sticky="w", padx=(0, 20))
        status_area.columnconfigure(1, weight=1)
        online_label = ttk.Label(status_area, textvariable=self.online_devices,
                                 width=1, wraplength=480)
        online_label.grid(row=0, column=1, sticky="ew")
        online_label.bind("<Configure>", lambda event: online_label.configure(
            wraplength=max(1, event.width)))
        ttk.Label(status_area, textvariable=self.node_measurement_status, width=1, wraplength=950).grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0)
        )

        ttk.Separator(content).grid(row=3, column=0, sticky="ew", pady=(0, 12))

        actions = ttk.Frame(content)
        actions.grid(row=4, column=0, sticky="ew")
        ttk.Label(actions, text="快速操作", style="Section.TLabel").grid(
            row=0, column=0, rowspan=2, padx=(0, 12)
        )
        ttk.Button(actions, text="连通测试", command=lambda: self._send("PING")).grid(
            row=0, column=1, padx=(0, 8)
        )
        ttk.Button(actions, text="矩阵复位", command=lambda: self._send("RESET")).grid(
            row=0, column=2, padx=(0, 8)
        )
        self.reset_all_button = ttk.Button(actions, text="全矩阵复位", command=self._reset_all_matrices)
        self.reset_all_button.grid(
            row=0, column=3, padx=(0, 8)
        )
        ttk.Button(actions, text="帮助", command=lambda: self._send("HELP")).grid(
            row=0, column=4, padx=(0, 8)
        )
        self.topology_button = ttk.Button(
            actions, text="拓扑扫描", command=self._open_topology_panel
        )
        self.topology_button.grid(row=0, column=5, padx=(8, 0))
        ttk.Label(actions, textvariable=self.all_reset_status).grid(row=1, column=1, columnspan=5, sticky="w", pady=(6, 0))
        node_measurement = ttk.LabelFrame(content, text="左右节点单次测量", padding=10)
        node_measurement.grid(row=5, column=0, sticky="ew", pady=(16, 12))
        node_measurement.columnconfigure(1, weight=1)
        node_measurement.columnconfigure(5, weight=1)
        ttk.Label(node_measurement, text="左端 Master").grid(row=0, column=0, padx=(0, 6), sticky="w")
        self.node_left_master_selector = ttk.Combobox(node_measurement, textvariable=self.node_left_master, state="readonly", width=12)
        self.node_left_master_selector.grid(row=0, column=1, padx=(0, 12), sticky="ew")
        ttk.Label(node_measurement, text="左侧从机数").grid(row=0, column=2, padx=(0, 6))
        self.node_left_modules_spinner = ttk.Spinbox(node_measurement, from_=1, to=10, textvariable=self.node_left_modules, width=5)
        self.node_left_modules_spinner.grid(row=0, column=3, padx=(0, 12))
        ttk.Label(node_measurement, text="左端节点").grid(row=0, column=4, padx=(0, 6))
        self.node_left_selector = ttk.Combobox(node_measurement, textvariable=self.node_left, state="readonly", width=16)
        self.node_left_selector.grid(row=0, column=5, padx=(0, 12), sticky="ew")
        ttk.Label(node_measurement, text="右端 Master").grid(row=1, column=0, padx=(0, 6), pady=(8, 0), sticky="w")
        self.node_right_master_selector = ttk.Combobox(node_measurement, textvariable=self.node_right_master, state="readonly", width=12)
        self.node_right_master_selector.grid(row=1, column=1, padx=(0, 12), pady=(8, 0), sticky="ew")
        ttk.Label(node_measurement, text="右侧从机数").grid(row=1, column=2, padx=(0, 6), pady=(8, 0))
        self.node_right_modules_spinner = ttk.Spinbox(node_measurement, from_=1, to=10, textvariable=self.node_right_modules, width=5)
        self.node_right_modules_spinner.grid(row=1, column=3, padx=(0, 12), pady=(8, 0))
        ttk.Label(node_measurement, text="右端节点").grid(row=1, column=4, padx=(0, 6), pady=(8, 0))
        self.node_right_selector = ttk.Combobox(node_measurement, textvariable=self.node_right, state="readonly", width=16)
        self.node_right_selector.grid(row=1, column=5, padx=(0, 12), pady=(8, 0), sticky="ew")
        ttk.Label(node_measurement, text="闭合后等待 (s)").grid(row=2, column=0, padx=(0, 6), pady=(8, 0), sticky="w")
        ttk.Entry(node_measurement, textvariable=self.node_settle_seconds, width=8).grid(row=2, column=1, padx=(0, 12), pady=(8, 0), sticky="w")
        self.node_measurement_button = ttk.Button(node_measurement, text="测量节点", command=self._run_node_measurement)
        self.node_measurement_button.grid(row=2, column=4, padx=(0, 8), pady=(8, 0), sticky="e")
        ttk.Label(node_measurement, text="等待 0～5 秒，可输入 0.001 秒", style="Status.TLabel").grid(row=2, column=5, pady=(8, 0), sticky="w")

        calibration = ttk.Frame(content)
        calibration.grid(row=6, column=0, sticky="ew", pady=(0, 12))
        calibration.columnconfigure(1, weight=1)
        ttk.Label(calibration, text="显示校准", style="Section.TLabel").grid(
            row=0, column=0, padx=(0, 12)
        )
        self.calibration_selector = ttk.Combobox(
            calibration,
            textvariable=self.calibration_choice,
            state="readonly",
            width=56,
        )
        self.calibration_selector.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        ttk.Button(
            calibration,
            text="应用所选",
            command=self._apply_selected_calibration,
        ).grid(row=0, column=2, padx=(0, 8))
        ttk.Checkbutton(
            calibration,
            text="启用",
            variable=self.calibration_enabled,
            command=self._toggle_calibration_enabled,
        ).grid(row=0, column=3, padx=(0, 8))
        ttk.Button(
            calibration,
            text="刷新",
            command=self._refresh_calibration_status,
        ).grid(row=0, column=4)

        log_area = ttk.Frame(content)
        log_area.grid(row=7, column=0, sticky="nsew")
        log_area.columnconfigure(0, weight=1)
        log_area.rowconfigure(1, weight=1)
        log_header = ttk.Frame(log_area)
        log_header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        log_header.columnconfigure(0, weight=1)
        ttk.Label(log_header, text="通信记录", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(log_header, textvariable=self.log_status).grid(
            row=0, column=1, padx=(0, 12)
        )
        ttk.Checkbutton(
            log_header,
            text="过滤正常收发",
            variable=self.filter_normal_logs,
            command=self._log_filter_changed,
        ).grid(row=0, column=2, padx=(0, 8))
        self.log_pause_button = ttk.Button(
            log_header, text="暂停日志", command=self._toggle_log_pause
        )
        self.log_pause_button.grid(row=0, column=3, padx=(0, 8))
        ttk.Button(log_header, text="清空", command=self._clear_log).grid(
            row=0, column=4
        )

        self.log = tk.Text(
            log_area,
            height=12,
            wrap="word",
            state="disabled",
            font=("Consolas", 10),
            background="#F8FAFC",
            foreground="#17202A",
            relief="solid",
            borderwidth=1,
        )
        scrollbar = ttk.Scrollbar(log_area, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.tag_configure("normal", foreground="#64748B")
        self.log.tag_configure("success", foreground="#166534")
        self.log.tag_configure("warning", foreground="#A16207")
        self.log.tag_configure("error", foreground="#B91C1C")
        self.log.grid(row=1, column=0, sticky="nsew")
        scrollbar.grid(row=1, column=1, sticky="ns")
        self._build_debug_page(debug_page)
        self._build_matrix_status_page(status_page)

    def _build_debug_page(self, page: ttk.Frame) -> None:
        """Retain the original manual paths and closures on the debug page."""
        page.columnconfigure(0, weight=1)
        target = ttk.Frame(page)
        target.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(target, text="调试目标（沿用控制台选择）：").grid(row=0, column=0)
        ttk.Label(target, textvariable=self.master_id).grid(row=0, column=1, padx=8)
        ttk.Label(target, textvariable=self.target_id).grid(row=0, column=2)

        actions = ttk.LabelFrame(page, text="测量与自动测试", padding=8)
        actions.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        for column, (name, label, command) in enumerate((
            ("measure_button", "读取电阻", lambda: self._send("MEASURE")),
            ("batch_calibration_button", "批量校准", self._run_batch_calibration),
            ("auxiliary_test_button", "辅助回路测试", self._run_auxiliary_loop_test),
            ("sequential_test_button", "Y0顺序测试", self._toggle_sequential_loop_test),
            ("pairwise_test_button", "全引脚阻值", self._toggle_pairwise_resistance_test),
        )):
            button = ttk.Button(actions, text=label, command=command)
            button.grid(row=0, column=column, padx=(0, 8))
            setattr(self, name, button)

        route = ttk.LabelFrame(page, text="四线测量路径", padding=8)
        route.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        for row, (label, bank, x) in enumerate((("正端", self.positive_bank, self.positive_x),
                                               ("负端", self.negative_bank, self.negative_x))):
            ttk.Label(route, text=label).grid(row=row, column=0, padx=(0, 8), pady=3)
            ttk.Combobox(route, textvariable=bank, values=("S1", "S2"), state="readonly", width=5).grid(row=row, column=1)
            ttk.Label(route, text="X").grid(row=row, column=2, padx=(8, 4))
            ttk.Spinbox(route, from_=0, to=23, textvariable=x, width=5).grid(row=row, column=3)
        ttk.Button(route, text="应用路径", command=self._apply_route).grid(row=0, column=4, rowspan=2, padx=16)

        dual = ttk.LabelFrame(page, text="双节点四线测量 / 路径闭合", padding=8)
        dual.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        for row, (label, bank, first, second) in enumerate((
            ("正端两个 X", self.dual_positive_bank, self.dual_positive_x1, self.dual_positive_x2),
            ("负端两个 X", self.dual_negative_bank, self.dual_negative_x1, self.dual_negative_x2),
        )):
            ttk.Label(dual, text=label).grid(row=row, column=0, padx=(0, 8), pady=3)
            ttk.Combobox(dual, textvariable=bank, values=("S1", "S2"), state="readonly", width=5).grid(row=row, column=1)
            ttk.Spinbox(dual, from_=0, to=23, textvariable=first, width=5).grid(row=row, column=2, padx=(8, 4))
            ttk.Label(dual, text="/").grid(row=row, column=3)
            ttk.Spinbox(dual, from_=0, to=23, textvariable=second, width=5).grid(row=row, column=4, padx=4)
        self.dual_node_measurement_button = ttk.Button(dual, text="执行双节点闭合", command=self._run_dual_node_measurement)
        self.dual_node_measurement_button.grid(row=0, column=5, padx=16)
        ttk.Label(dual, textvariable=self.dual_node_status).grid(row=1, column=5, padx=16, sticky="w")

        switch = ttk.LabelFrame(page, text="单个交叉点", padding=8)
        switch.grid(row=4, column=0, sticky="ew", pady=(0, 12))
        ttk.Combobox(switch, textvariable=self.switch_bank, values=("S1", "S2"), state="readonly", width=5).grid(row=0, column=0)
        ttk.Spinbox(switch, from_=0, to=23, textvariable=self.switch_x, width=5).grid(row=0, column=1, padx=8)
        ttk.Combobox(switch, textvariable=self.switch_bus, values=("Y0", "Y1", "Y2", "Y3", "Y4"), state="readonly", width=5).grid(row=0, column=2)
        ttk.Button(switch, text="闭合节点", command=lambda: self._set_crosspoint(True)).grid(row=0, column=3, padx=8)
        ttk.Button(switch, text="断开节点", command=lambda: self._set_crosspoint(False)).grid(row=0, column=4)
        ttk.Button(switch, text="目标矩阵复位", command=lambda: self._send("RESET")).grid(row=0, column=5, padx=8)

        custom = ttk.Frame(page)
        custom.grid(row=5, column=0, sticky="ew", pady=(0, 12))
        custom.columnconfigure(1, weight=1)
        ttk.Label(custom, text="自定义指令").grid(row=0, column=0, padx=(0, 8))
        entry = ttk.Entry(custom, textvariable=self.custom_command)
        entry.grid(row=0, column=1, sticky="ew")
        entry.bind("<Return>", lambda _event: self._send_custom())
        ttk.Button(custom, text="发送", command=self._send_custom).grid(row=0, column=2, padx=8)
        for row, variable in enumerate((self.batch_status, self.auxiliary_test_status,
                                        self.sequential_test_status, self.pairwise_test_status), start=6):
            ttk.Label(page, textvariable=variable).grid(row=row, column=0, sticky="w", pady=2)

    def _build_matrix_status_page(self, page: ttk.Frame) -> None:
        """Build a paged device view for the five Y buses of each matrix bank."""
        page.columnconfigure(0, weight=1)
        page.rowconfigure(2, weight=1)
        ttk.Label(page, text="矩阵状态", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )

        toolbar = ttk.Frame(page)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(14, 10))
        toolbar.columnconfigure(3, weight=1)
        ttk.Label(toolbar, text="设备").grid(row=0, column=0, padx=(0, 8))
        self.matrix_status_selector = ttk.Combobox(
            toolbar,
            textvariable=self.matrix_status_target,
            values=self._available_status_target_options(),
            state="readonly",
            width=12,
        )
        self.matrix_status_selector.grid(row=0, column=1, padx=(0, 10))
        self.matrix_status_selector.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._render_matrix_status(self.matrix_status_target.get()),
        )
        ttk.Button(
            toolbar, text="刷新当前", command=self._refresh_matrix_status
        ).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(
            toolbar, text="刷新全部", command=self._refresh_all_matrix_status
        ).grid(row=0, column=3, sticky="w")
        ttk.Label(
            toolbar, textvariable=self.matrix_status_summary, style="Status.TLabel"
        ).grid(row=0, column=4, sticky="e")

        bank_notebook = ttk.Notebook(page)
        bank_notebook.grid(row=2, column=0, sticky="nsew")
        self.matrix_bank_notebook = bank_notebook
        for bank in ("S1", "S2"):
            bank_page = ttk.Frame(bank_notebook, padding=(8, 8, 8, 4))
            bank_page.columnconfigure(0, weight=1)
            bank_page.rowconfigure(0, weight=1)
            bank_notebook.add(bank_page, text=bank)
            # Text tags can color individual status words; Treeview tags only
            # color complete rows and cannot highlight one closed crosspoint.
            view = tk.Text(
                bank_page,
                height=24,
                wrap="none",
                state="disabled",
                font=("Consolas", 10),
                background="#F8FAFC",
                foreground="#334155",
                relief="solid",
                borderwidth=1,
                padx=8,
                pady=6,
                tabs=("0.85i", "1.8i", "2.75i", "3.7i", "4.65i", "5.6i"),
            )
            view.tag_configure("header", foreground="#0F172A", font=("Consolas", 10, "bold"))
            view.tag_configure("closed", foreground="#B91C1C", font=("Consolas", 10, "bold"))
            view.tag_configure("open", foreground="#334155")
            view.tag_configure("unknown", foreground="#94A3B8")
            view.tag_configure("selected", background="#FEE2E2")
            view.bind(
                "<Button-1>",
                lambda event, status_view=view: self._select_matrix_cell(
                    status_view, event
                ),
            )
            view.bind(
                "<Double-Button-1>",
                lambda event, status_view=view: self._toggle_matrix_cell(
                    status_view, event
                ),
            )
            view.bind(
                "<Motion>",
                lambda event, status_view=view: self._update_matrix_cursor(
                    status_view, event
                ),
            )
            view.bind("<Leave>", lambda _event, status_view=view: status_view.configure(cursor="arrow"))
            scrollbar = ttk.Scrollbar(bank_page, orient="vertical", command=view.yview)
            view.configure(yscrollcommand=scrollbar.set)
            view.grid(row=0, column=0, sticky="nsew")
            scrollbar.grid(row=0, column=1, sticky="ns")
            self._status_views[bank] = view

        self._refresh_status_target_selector()
        self._render_matrix_status(self.matrix_status_target.get())

    def _available_status_target_options(self) -> tuple[str, ...]:
        """Return concrete device IDs that can answer a status request."""
        return tuple(
            target for target in self._available_target_options()
            if target.casefold() != BROADCAST_TARGET
        )

    def _refresh_status_target_selector(self) -> None:
        """Keep the status-page device selector aligned with online peers."""
        selector = getattr(self, "matrix_status_selector", None)
        if selector is None:
            return
        options = self._available_status_target_options()
        selector.configure(values=options)
        if self.matrix_status_target.get() not in options and options:
            self.matrix_status_target.set(options[0])
        self._render_matrix_status(self.matrix_status_target.get())

    def _refresh_matrix_status(self) -> None:
        """Request the selected device's software matrix state."""
        target = self.matrix_status_target.get().strip()
        if target:
            self._send("STATUS", target_id=target)

    def _refresh_all_matrix_status(self) -> None:
        """Request status from every concrete device currently known to the GUI."""
        for target in self._available_status_target_options():
            self._send("STATUS", target_id=target)

    def _render_matrix_status(self, target_id: str) -> None:
        """Render one cached snapshot, leaving unavailable banks explicitly unknown."""
        logical_target = self._logical_target_id(target_id)
        snapshot = self._matrix_status.get(logical_target)
        banks = snapshot[1] if snapshot is not None else {}
        for bank, view in self._status_views.items():
            rows = banks.get(bank)
            view.configure(state="normal")
            view.delete("1.0", "end")
            view.insert("end", "X端口\tY0\tY1\tY2\tY3\tY4\n", "header")
            for x in range(24):
                view.insert("end", f"X{x}\t")
                for y in range(5):
                    if rows is None:
                        value, tag = "未知", "unknown"
                    else:
                        value, tag = ("闭合", "closed") if rows[y][x] else ("断开", "open")
                    start = view.index("end-1c")
                    view.insert("end", value)
                    end = view.index("end-1c")
                    coordinate_tag = matrix_cell_tag(bank, x, y)
                    view.tag_add(tag, start, end)
                    view.tag_add(coordinate_tag, start, end)
                    if self._matrix_selected_cell == (logical_target, bank, x, y):
                        view.tag_add("selected", start, end)
                    view.insert("end", "\t" if y < 4 else "\n")
            view.configure(state="disabled")
        if snapshot is None:
            self.matrix_status_summary.set(f"{logical_target or '-'}：尚未查询")
        else:
            self.matrix_status_summary.set(
                f"{logical_target}：{snapshot[0]}，软件状态"
            )

    @staticmethod
    def _matrix_cell_at_pointer(
        view: tk.Text, event: tk.Event
    ) -> tuple[str, int, int] | None:
        """Resolve a pointer event over a rendered matrix status word."""
        index = view.index(f"@{event.x},{event.y}")
        return parse_matrix_cell_tags(tuple(view.tag_names(index)))

    def _matrix_cell_state(
        self, target_id: str, bank: str, x: int, y: int
    ) -> bool | None:
        """Read one cached software state, returning None when not queried."""
        snapshot = self._matrix_status.get(self._logical_target_id(target_id))
        if snapshot is None:
            return None
        rows = snapshot[1].get(bank)
        if rows is None:
            return None
        return rows[y][x]

    def _select_matrix_cell(self, view: tk.Text, event: tk.Event) -> None:
        """Select one status cell and show its cached state."""
        cell = self._matrix_cell_at_pointer(view, event)
        if cell is None:
            return
        bank, x, y = cell
        target_id = self._logical_target_id(self.matrix_status_target.get())
        state = self._matrix_cell_state(target_id, bank, x, y)
        self._matrix_selected_cell = (target_id, bank, x, y)
        for status_view in self._status_views.values():
            status_view.tag_remove("selected", "1.0", "end")
        ranges = view.tag_ranges(matrix_cell_tag(bank, x, y))
        if len(ranges) >= 2:
            view.tag_add("selected", ranges[0], ranges[1])
        state_text = "未知" if state is None else ("闭合" if state else "断开")
        self.matrix_status_summary.set(
            f"{target_id} · {bank} X{x}/Y{y}：{state_text}"
        )

    def _toggle_matrix_cell(self, view: tk.Text, event: tk.Event) -> str | None:
        """Toggle one known crosspoint through the existing routed SWITCH command."""
        cell = self._matrix_cell_at_pointer(view, event)
        if cell is None:
            return None
        bank, x, y = cell
        target_id = self._logical_target_id(self.matrix_status_target.get())
        self._select_matrix_cell(view, event)
        if target_id in MASTER_WIFI_IDS and bank == "S2":
            self._append_log("错误", f"{target_id} 只配置了 S1 矩阵，不能操作 S2")
            return "break"
        state = self._matrix_cell_state(target_id, bank, x, y)
        if state is None:
            self._append_log("状态", f"{target_id} {bank} X{x}/Y{y} 状态未知，正在刷新")
            self._send("STATUS", target_id=target_id)
            return "break"
        next_state = "OFF" if state else "ON"
        self.matrix_status_summary.set(
            f"{target_id} · {bank} X{x}/Y{y}：操作中"
        )
        self._send(
            f"SWITCH {bank} {x} Y{y} {next_state}",
            target_id=target_id,
        )
        return "break"

    def _update_matrix_cursor(self, view: tk.Text, event: tk.Event) -> None:
        """Use a hand cursor only while the pointer is over an actionable cell."""
        cursor = "hand2" if self._matrix_cell_at_pointer(view, event) else "arrow"
        view.configure(cursor=cursor)

    def _refresh_matrix_status_later(self, target_id: str) -> None:
        """Refresh a device shortly after a successful matrix-changing command."""
        scoped_target = self._split_scoped_master_target(target_id)
        bus_target = scoped_target[1] if scoped_target is not None else target_id
        if bus_target.casefold() == BROADCAST_TARGET:
            return
        root = getattr(self, "root", None)
        if root is not None:
            root.after(80, lambda: self._send("STATUS", target_id=target_id))

    def _target_changed(self, *_arguments: object) -> None:
        """Refresh the visible profile state when the selected ESP ID changes."""
        self._refresh_calibration_status()

    def _refresh_calibration_status(self) -> None:
        """Refresh selected profile state and dated candidates for one ESP ID."""
        target_id = self._logical_target_id(self.target_id.get())
        if not target_id:
            self.calibration_status.set("显示校准：未选择设备")
            self.calibration_selector.configure(values=())
            self.calibration_choice.set("")
            self.calibration_enabled.set(False)
            return
        self._refreshing_calibration_controls = True
        try:
            calibration = self.calibration_store.load_selection(target_id)
            candidates = self.calibration_store.list_candidates(target_id)
        except CalibrationProfileError as error:
            self.calibration_status.set(f"显示校准：{target_id} 配置错误")
            self._append_log("错误", str(error))
            return
        finally:
            self._refreshing_calibration_controls = False

        self._calibration_candidates = {
            candidate.display_name: candidate for candidate in candidates
        }
        labels = tuple(self._calibration_candidates)
        self.calibration_selector.configure(values=labels)
        current_choice = self.calibration_choice.get()
        selected_label = next(
            (
                label
                for label, candidate in self._calibration_candidates.items()
                if calibration is not None
                and candidate.report_id == calibration.source_report_id
            ),
            current_choice if current_choice in self._calibration_candidates else "",
        )
        if not selected_label and labels:
            selected_label = labels[0]
        self.calibration_choice.set(selected_label)

        self._refreshing_calibration_controls = True
        try:
            self.calibration_enabled.set(
                calibration is not None and calibration.enabled
            )
        finally:
            self._refreshing_calibration_controls = False

        if calibration is None:
            self.calibration_status.set(f"显示校准：{target_id} 未配置")
            return
        source = calibration.source_report_id or "历史配置"
        state = "已启用" if calibration.enabled else "已停用"
        warning_note = (
            f"，来源含{calibration.quality_warning_count}项警告"
            if calibration.quality_warning_count
            else ""
        )
        self.calibration_status.set(
            f"显示校准：{target_id} {state}（{source}{warning_note}）"
        )

    def _apply_selected_calibration(self) -> None:
        """Persist and enable the report candidate selected for the current ESP."""
        target_id = self._logical_target_id(self.target_id.get())
        candidate = self._calibration_candidates.get(self.calibration_choice.get())
        if candidate is None:
            self._append_log("错误", f"{target_id or '当前设备'}没有可选择的校准报告")
            return
        if not candidate.usable:
            self._append_log(
                "错误",
                f"{candidate.report_id}不可应用：{candidate.validation_error}",
            )
            return
        if (candidate.warning_count or candidate.negative_count) and not messagebox.askyesno(
            "应用含警告的校准",
            f"{candidate.report_id}包含{candidate.warning_count}项质量警告，"
            f"其中有{candidate.negative_count}个负值。\n"
            "仍要把它用于当前设备的显示校准吗？",
            parent=self.root,
        ):
            self._refresh_calibration_status()
            return
        try:
            profile_path = self.calibration_store.activate_candidate(
                target_id, candidate
            )
        except CalibrationProfileError as error:
            self._append_log("错误", str(error))
            self._refresh_calibration_status()
            return
        self._append_log(
            "状态",
            f"{target_id}已应用校准 {candidate.report_id}：{profile_path}",
        )
        self._refresh_calibration_status()

    def _toggle_calibration_enabled(self) -> None:
        """Persist the user's enable switch for the current device profile."""
        if self._refreshing_calibration_controls:
            return
        target_id = self._logical_target_id(self.target_id.get())
        enabled = self.calibration_enabled.get()
        try:
            calibration = self.calibration_store.set_enabled(target_id, enabled)
        except CalibrationProfileError as error:
            self._append_log("错误", str(error))
            self._refresh_calibration_status()
            return
        state = "启用" if calibration.enabled else "停用"
        self._append_log(
            "状态",
            f"{target_id}显示校准已{state}："
            f"{calibration.source_report_id or calibration.profile_path.name}",
        )
        self._refresh_calibration_status()

    def _start_server(self) -> None:
        """校验监听参数并启动内嵌服务器；绑定过程放在工作线程中。"""
        if getattr(self, "_shutdown_pending", False):
            self._append_log("错误", "服务器正在停止，请等待测试清理完成")
            return
        host = self.host.get().strip()
        if not host:
            self._append_log("错误", "监听地址不能为空")
            return
        try:
            port = int(self.port.get())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            self._append_log("错误", "端口必须是 1 到 65535 之间的整数")
            return

        self.status.set("服务器正在启动...")
        threading.Thread(
            target=self.router.start,
            args=(host, port),
            daemon=True,
            name="cable-router-start",
        ).start()

    def _stop_server(self) -> None:
        """在工作线程停止监听和客户端连接，避免阻塞 Tkinter 事件循环。"""
        if getattr(self, "_shutdown_pending", False):
            return
        self._shutdown_pending = True
        self._cancel_hardware_controllers()
        self._finish_server_stop()

    def _finish_server_stop(self) -> None:
        """Keep result routing alive until controller cleanup has finished."""
        if self._hardware_running():
            self.status.set("正在停止测试并复位矩阵...")
            self.root.after(50, self._finish_server_stop)
            return
        self.status.set("服务器正在停止...")
        threading.Thread(
            target=self.router.stop,
            daemon=True,
            name="cable-router-stop",
        ).start()  # 负责停止服务器的工作线程 这个start主要看target是哪个函数，target是router.stop()，所以这个线程启动后会执行router.stop()方法

    def _hardware_controllers(self) -> tuple[object, ...]:
        """Collect controller owners, including optional tools in older test fixtures."""
        names = (
            "batch_calibration", "auxiliary_loop_test", "sequential_loop_test",
            "pairwise_resistance_test", "dual_node_measurement", "topology_scan",
            "node_measurement", "all_matrix_reset",
        )
        return tuple(
            controller for name in names
            if (controller := getattr(self, name, None)) is not None
        )

    def _hardware_running(self) -> bool:
        """Check workers, including their reset and report finalization phase."""
        return any(controller.running for controller in self._hardware_controllers())

    def _cancel_hardware_controllers(self) -> None:
        """Request cancellation without closing the sockets needed for RESET."""
        for controller in self._hardware_controllers():
            if controller.running:
                controller.cancel()

    def _topology_running(self) -> bool:
        """Retain exclusive ownership until the final topology event is consumed."""
        controller = getattr(self, "topology_scan", None)
        return bool(
            getattr(self, "_topology_owned", False)
            or (controller is not None and controller.running)
        )

    def _topology_blocks_hardware(self) -> bool:
        """Gate legacy commands and test entry points during topology or shutdown."""
        if getattr(self, "_shutdown_pending", False):
            self._append_log("错误", "服务器正在停止，暂不允许硬件命令")
            return True
        if self._topology_running():
            self._append_log("错误", "拓扑扫描运行中，暂不允许其他硬件命令")
            return True
        if self._manual_operation_running():
            self._append_log("错误", "节点测量或全矩阵复位运行中，暂不允许其他硬件命令")
            return True
        return False

    def _manual_operation_running(self) -> bool:
        """Include queued completion events so controls stay locked through cleanup."""
        return bool(getattr(self, "_manual_operation", "") or any(
            controller is not None and controller.running
            for name in ("node_measurement", "all_matrix_reset")
            for controller in (getattr(self, name, None),)
        ))

    def _refresh_node_selectors(self, *_arguments: object) -> None:
        """Populate G1..G24 per slave and offer only currently online masters."""
        for side in ("left", "right"):
            getattr(self, f"node_{side}_master_selector").configure(values=self._online_master_ids)
            try:
                labels = node_labels(int(getattr(self, f"node_{side}_modules").get()))
            except ValueError:
                labels = ()
            getattr(self, f"node_{side}_selector").configure(values=labels)
            variable = getattr(self, f"node_{side}")
            if labels and variable.get() not in labels:
                variable.set(labels[0])

    def _can_start_console_operation(self) -> bool:
        """Reject overlap with scans, debug commands and server shutdown."""
        if self._topology_blocks_hardware():
            return False
        if self._hardware_running() or getattr(self, "_pending_commands", {}):
            self._append_log("错误", "已有测试或手动命令等待完成，暂不能开始新操作")
            return False
        if not self.router.running:
            self._append_log("错误", "服务器尚未启动")
            return False
        return True

    def _invalidate_matrix_views(self) -> None:
        """Invalidate cached routing and status after operations affecting multiple boards."""
        self._active_routes.clear()
        self._matrix_status.clear()
        self._render_matrix_status(self.matrix_status_target.get())

    def _run_node_measurement(self) -> None:
        """Measure one selected pair using firmware-controlled settling time."""
        if self.node_measurement.running:
            self.node_measurement.cancel()
            self.node_measurement_button.state(["disabled"])
            self.node_measurement_status.set("节点测量：正在停止并复位")
            return
        if not self._can_start_console_operation():
            return
        if any(variable.get() not in self._online_master_ids
               for variable in (self.node_left_master, self.node_right_master)):
            self._append_log("错误", "左右端必须选择当前在线的主机")
            return
        try:
            started = self.node_measurement.start(
                self.node_left_master.get(), self.node_right_master.get(),
                int(self.node_left_modules.get()), int(self.node_right_modules.get()),
                self.node_left.get(), self.node_right.get(), float(self.node_settle_seconds.get()),
            )
        except (TypeError, ValueError) as error:
            self._append_log("错误", f"节点测量参数无效：{error}")
            return
        if started:
            self._manual_operation = "node"
            self._invalidate_matrix_views()
            self._set_topology_interlock(True)
            self.node_measurement_button.state(["!disabled"])
            self.node_measurement_button.configure(text="停止测量")
            self.node_measurement_status.set("节点测量：正在检查主从机")

    def _reset_all_matrices(self) -> None:
        """Reset all connected masters, their buses and directly connected slaves."""
        if not self._can_start_console_operation():
            return
        devices = self.router.connected_ids()
        masters = tuple(device for device in devices if device in MASTER_WIFI_IDS)
        slaves = tuple(device for device in devices if device in SLAVE_WIFI_IDS)
        try:
            started = self.all_matrix_reset.start(masters, slaves)
        except ValueError as error:
            self._append_log("错误", str(error))
            return
        if started:
            self._manual_operation = "reset"
            self._invalidate_matrix_views()
            self._set_topology_interlock(True)
            self.all_reset_status.set("全矩阵复位：正在执行")

    def _finish_console_operation(self) -> None:
        """Release ownership on Tk's thread after controllers finish cleanup."""
        self._manual_operation = ""
        self._invalidate_matrix_views()
        self._set_topology_interlock(False)
        self.node_measurement_button.configure(text="测量节点")

    def _handle_console_operation_event(self, event_type: str, message: object) -> None:
        """Present confirmed measurements and resets without treating lost replies as success."""
        result = message if isinstance(message, dict) else {}
        if event_type == "node_measure_started":
            text = (f"{result['left_master']}/{result['left']} → {result['right_master']}/{result['right']}；"
                    f"闭合后等待 {result['settle_ms']} ms")
            self._append_log("节点测量", text)
        elif event_type == "node_measure_progress":
            self.node_measurement_status.set("节点测量：固件正在闭合、等待和读取")
        elif event_type == "node_measure_cleanup_warning":
            self._append_log("警告", str(message))
        elif event_type in {"node_measure_complete", "node_measure_error", "node_measure_stopped"}:
            self._finish_console_operation()
            if event_type == "node_measure_complete":
                reading = MEASURE_RESULT_PATTERN.fullmatch(str(result['sample']))
                resistance = f"{reading['resistance']} Ω" if reading else str(result['sample'])
                text = f"节点测量：{result['left']} → {result['right']}；{resistance}；等待 {result['settle_ms']} ms"
                self._append_log("节点测量", f"{result['left_master']} → {result['right_master']} {result['sample']}")
            else:
                text = f"节点测量：{message}"
            self.node_measurement_status.set(text)
            self._append_log("错误" if event_type == "node_measure_error" else "节点测量", text)
        elif event_type == "all_reset_progress":
            self.all_reset_status.set(f"全矩阵复位：{result['done']}/{result['total']} {result['target']}")
            self._append_log("全矩阵复位", f"{result['target']}：{result['reply']}")
        elif event_type == "all_reset_complete":
            self._finish_console_operation()
            unknown = result.get("unconfirmed", [])
            text = (f"全矩阵复位：{'已停止' if result.get('cancelled') else '完成'}；"
                    f"已确认 {len(result.get('confirmed', []))} 个目标，"
                    f"广播已发送 {len(result.get('broadcast', []))} 条总线，未确认 {len(unknown)} 个目标")
            self.all_reset_status.set(text)
            self._append_log("警告" if unknown else "全矩阵复位", text)
            if unknown:
                self._append_log("警告", "未响应的从机地址可能未接入；广播不提供逐台确认，请核对接入清单。")

    def _set_topology_interlock(self, active: bool) -> None:
        """Reflect topology ownership without enabling measurements in slave debug mode."""
        state = ["disabled"] if active else ["!disabled"]
        for name in (
            "measure_button", "batch_calibration_button", "auxiliary_test_button",
            "sequential_test_button", "pairwise_test_button", "dual_node_measurement_button",
            "node_measurement_button", "reset_all_button",
        ):
            button = getattr(self, name, None)
            if button is not None:
                button.state(state)
        if not active:
            self._apply_mode_state()

    def _open_topology_panel(self) -> None:
        """Open the independent topology workspace without starting hardware work."""
        panel = getattr(self, "topology_panel", None)
        if panel is None or not panel.window.winfo_exists():
            panel = TopologyPanel(
                self.root, self._start_topology_scan, self.topology_scan.cancel
            )
            self.topology_panel = panel
        panel.set_devices(getattr(self, "_online_master_ids", ()))
        panel.window.deiconify()
        panel.window.lift()

    def _start_topology_scan(self, **parameters: object) -> bool:
        """Wait for legacy workflows and pending manual frames before acquiring both masters."""
        if self._topology_blocks_hardware():
            return False
        if self._hardware_running():
            self._append_log("错误", "已有测试运行中，不能启动拓扑扫描")
            return False
        if getattr(self, "_pending_commands", {}):
            self._append_log("错误", "仍有手动命令等待设备结果，暂不能启动拓扑扫描")
            return False
        if not self.router.running:
            self._append_log("错误", "服务器尚未启动")
            return False
        devices = getattr(self, "_online_master_ids", ())
        if any(parameters.get(name) not in devices for name in ("left_master", "right_master")):
            self._append_log("错误", "左右 Master 必须选择当前在线主机")
            return False
        self._topology_owned = True
        try:
            started = self.topology_scan.start(**parameters)
        except (TypeError, ValueError, RuntimeError) as error:
            self._topology_owned = False
            self._append_log("错误", str(error))
            return False
        if not started:
            self._topology_owned = False
            return False
        self._active_routes.clear()
        self._matrix_status.clear()
        self._render_matrix_status(self.matrix_status_target.get())
        self._set_topology_interlock(True)
        return True

    def _handle_topology_event(self, event_type: str, message: object) -> None:
        """Apply progress on the Tk thread and release ownership only after cleanup."""
        panel = getattr(self, "topology_panel", None)
        if panel is not None and panel.window.winfo_exists():
            panel.handle_event(event_type, message)
        if event_type in {"topology_complete", "topology_stopped", "topology_error"}:
            self._topology_owned = False
            self._active_routes.clear()
            self._matrix_status.clear()
            self._render_matrix_status(self.matrix_status_target.get())
            self._set_topology_interlock(False)
            result = message if isinstance(message, dict) else {}
            self._append_log(
                "错误" if event_type == "topology_error" else "拓扑扫描",
                str(result.get("error") or (
                    f"{event_type}: {result.get('completed', 0)}/{result.get('total', 0)}, "
                    f"测量 {result.get('measurements', 0)} 次"
                )),
            )
            if result.get("connection_error"):
                self._append_log(
                    "警告",
                    "扫描期间设备连接中断，原扫描已结束。请在两台主机重新上线后重新扫描。",
                )
            for error in result.get("cleanup_errors", []):
                self._append_log("警告", f"矩阵复位未确认：{error}")
            for extension in ("json", "csv"):
                if result.get(extension):
                    self._append_log("拓扑扫描", f"{extension.upper()}：{result[extension]}")
            if result.get("durable_session"):
                self._append_log("拓扑扫描", f"已确认数据：{result['durable_session']}")
            for recovered in result.get("recovered_sessions", []):
                for extension in ("sqlite", "json"):
                    if recovered.get(extension):
                        self._append_log(
                            "拓扑扫描",
                            f"上次扫描缓存 {recovered['session_id']} {extension.upper()}：{recovered[extension]}",
                        )

    def _dual_node_running(self) -> bool:
        """Return whether the optional dual-node controller owns the path."""
        controller = getattr(self, "dual_node_measurement", None)
        return bool(controller is not None and controller.running)

    def _set_dual_node_button_state(self, state: list[str]) -> None:
        """Update the dual-node action button when the full GUI is available."""
        button = getattr(self, "dual_node_measurement_button", None)
        if button is not None:
            button.state(state)

    def _mode_changed(self, *_arguments: object) -> None:
        """Apply role-specific controls after switching between master and slave views."""
        self._active_mode = self.mode.get()
        self._apply_mode_state()
        self._refresh_target_selector()
        self._refresh_status_target_selector()
        self._refresh_calibration_status()

    def _master_changed(self, *_arguments: object) -> None:
        """Refresh device-scoped views after selecting the other RS485 master."""
        self._refresh_target_selector()
        self._refresh_status_target_selector()
        self._refresh_calibration_status()

    def _is_master_mode(self) -> bool:
        """Return whether the selected GUI role addresses the RS485 master.

        The visible selector uses the protocol modes ``master``/``slave``. The
        Chinese labels are accepted as a compatibility path for callers and
        older tests that set ``_active_mode`` directly.
        """
        active_mode = getattr(self, "_active_mode", "slave")
        return active_mode.strip().casefold() in {"master", "主机"}

    def _selected_master_id(self) -> str:
        """Return the configured master selected for the current RS485 operation."""
        value = getattr(self, "master_id", None)
        selected = value.get().strip() if value is not None else ""
        return selected if selected in MASTER_WIFI_IDS else DEFAULT_MASTER_WIFI_ID

    @staticmethod
    def _split_scoped_master_target(target_id: str) -> tuple[str, str] | None:
        """Split an internal ``masterN-slaveN`` key into transport and bus IDs."""
        target = target_id.strip()
        for master_id in MASTER_WIFI_IDS:
            prefix = f"{master_id}-"
            if target.startswith(prefix) and len(target) > len(prefix):
                return master_id, target[len(prefix):]
        return None

    def _logical_target_id(self, target_id: str) -> str:
        """Build a collision-free state key while preserving legacy slave mode IDs."""
        target = target_id.strip()
        if (
            target in MASTER_WIFI_IDS
            or target in SCOPED_SLAVE_WIFI_IDS
            or self._split_scoped_master_target(target) is not None
        ):
            return target
        if self._is_master_mode():
            return f"{self._selected_master_id()}-{target}"
        return target

    def _available_target_options(self) -> tuple[str, ...]:
        """Return target IDs valid for the selected transport role.

        A master can address itself, a named slave, or the RS485 broadcast
        address. A GUI in slave mode only connects to a concrete WiFi slave
        endpoint.
        """
        if self._is_master_mode():
            return (
                self._selected_master_id(),
                *RS485_SLAVE_IDS,
                BROADCAST_TARGET,
            )
        return self._online_slave_ids or SLAVE_WIFI_IDS

    def _refresh_target_selector(self) -> None:
        """Refresh target choices without discarding an active broadcast test."""
        selector = getattr(self, "target_selector", None)
        if selector is None:
            return
        options = self._available_target_options()
        selector.configure(values=options)
        if self.target_id.get() not in options:
            self.target_id.set(options[0])
        self._refresh_status_target_selector()

    def _apply_mode_state(self) -> None:
        """Disable measurement-only controls while leaving matrix closure available."""
        is_master = self._is_master_mode() and not self._topology_running() and not self._manual_operation_running()
        measurement_buttons = (
            self.measure_button,
            self.batch_calibration_button,
            self.auxiliary_test_button,
            self.sequential_test_button,
            self.pairwise_test_button,
        )
        for button in measurement_buttons:
            button.state(["!disabled"] if is_master else ["disabled"])
        master_selector = getattr(self, "master_selector", None)
        if master_selector is not None:
            master_selector.configure(state="readonly" if is_master else "disabled")

    def _restore_controls_after_dual_node(self) -> None:
        """Restore mode-specific measurement controls and the closure command."""
        self._apply_mode_state()
        self._set_dual_node_button_state(["!disabled"])

    def _map_mode_request(
        self, target_id: str, command: str
    ) -> tuple[str, str]:
        """Map a logical matrix target to the WiFi endpoint and wire command."""
        target = target_id.strip()
        # A captured Wi-Fi endpoint stays direct even if the user changes mode.
        if target in SCOPED_SLAVE_WIFI_IDS:
            return target, command
        scoped_target = self._split_scoped_master_target(target)
        uses_master = (
            self._is_master_mode()
            or target in MASTER_WIFI_IDS
            or scoped_target is not None
        )
        if uses_master:
            if scoped_target is not None:
                master_id, bus_target = scoped_target
            elif target in MASTER_WIFI_IDS:
                master_id, bus_target = target, target
            else:
                master_id, bus_target = self._selected_master_id(), target
            if bus_target == master_id:
                return master_id, command
            normalized = command.strip().upper()
            if normalized in {"MEASURE", "HELP"}:
                return master_id, command
            if normalized.startswith("BUS "):
                return master_id, command
            return master_id, f"BUS {bus_target} {command}"
        return target, command

    def _send_via_mode(self, target_id: str, request_id: str, command: str) -> str:
        """Send one logical command through the active master/slave transport path."""
        if self._topology_running():
            return "ERR TOPOLOGY_BUSY"
        if self._manual_operation_running():
            return "ERR CONSOLE_OPERATION_BUSY"
        logical_target = self._logical_target_id(target_id)
        uses_master = (
            logical_target in MASTER_WIFI_IDS
            or self._split_scoped_master_target(logical_target) is not None
        )
        if not uses_master and command.strip().upper() == "MEASURE":
            return "ERR MEASURE UNSUPPORTED_ON_SLAVE"
        if not uses_master and logical_target.casefold() == BROADCAST_TARGET:
            return "ERR BROADCAST_MASTER_ONLY"
        transport_target, wire_command = self._map_mode_request(
            logical_target, command
        )
        route_key = (transport_target, request_id)
        self._request_routes[route_key] = logical_target
        acknowledgement = self.router.send_from_controller(
            transport_target, request_id, wire_command
        )
        if not acknowledgement.startswith("OK FORWARDED "):
            self._request_routes.pop(route_key, None)
        return acknowledgement

    def _logical_result_target(self, transport_target: str, request_id: str) -> str:
        """Translate a master RESULT source back to its collision-free device key."""
        return self._request_routes.pop(
            (transport_target, request_id), transport_target
        )

    def _send(self, command: str, *, target_id: str | None = None) -> None:
        """为本地 GUI 命令生成请求 ID，并交给内嵌路由服务发送。"""
        if self._topology_blocks_hardware():
            return
        if self.batch_calibration.running:
            self._append_log("错误", "批量校准运行中，暂不允许插入其他硬件命令")
            return
        if self.auxiliary_loop_test.running:
            self._append_log("错误", "辅助回路测试运行中，暂不允许插入其他硬件命令")
            return
        if self.sequential_loop_test.running:
            self._append_log("错误", "Y0顺序测试运行中，暂不允许插入其他硬件命令")
            return
        if self.pairwise_resistance_test.running:
            self._append_log("错误", "全引脚阻值测试运行中，暂不允许插入其他硬件命令")
            return
        if self._dual_node_running():
            self._append_log("错误", "双节点四线闭合运行中，暂不允许插入其他硬件命令")
            return
        target_id = self._logical_target_id(
            target_id if target_id is not None else self.target_id.get()
        )
        if not target_id or any(character.isspace() for character in target_id):
            self._append_log("错误", "目标ID不能为空且不能包含空格")
            return
        if not self.router.running:
            self._append_log("错误", "服务器尚未启动")
            return
        if "\n" in command or "\r" in command:
            self._append_log("错误", "单条指令不能包含换行符")
            return

        self.request_counter += 1
        request_id = f"GUI-{self.request_counter}"
        self._pending_commands[(target_id, request_id)] = command
        threading.Thread(
            target=self._send_worker,
            args=(target_id, request_id, command),
            daemon=True,
            name="cable-router-send",
        ).start() 

    def _send_worker(self, target_id: str, request_id: str, command: str) -> None:
        """在后台线程写目标 socket，并把发送记录和服务器回执投递给 GUI。"""
        self.events.put(("sent", f"{CONTROLLER_ID} -> {target_id} {request_id} {command}"))
        acknowledgement = self._send_via_mode(target_id, request_id, command)
        self.events.put(
            ("controller_ack", (target_id, request_id, acknowledgement))
        )

    def _send_custom(self) -> None:
        """发送用户输入的单行自定义命令，并在提交后清空输入框。"""
        command = self.custom_command.get().strip()
        if command:
            self._send(command)
            self.custom_command.set("")
        else:
            self._append_log("错误", "指令不能为空")

    def _selected_pair_command(self, command_name: str) -> str | None:
        """校验两个 X 编号并生成指定的双端口命令。"""
        try:
            positive_x = int(self.positive_x.get())
            negative_x = int(self.negative_x.get())
            if not 0 <= positive_x <= 23 or not 0 <= negative_x <= 23:
                raise ValueError
        except ValueError:
            self._append_log("错误", "通道编号必须在 0 到 23 之间")
            return None

        return (
            f"{command_name} {self.positive_bank.get()} {positive_x} "
            f"{self.negative_bank.get()} {negative_x}"
        )

    def _apply_route(self) -> None:
        """发送当前界面选择的四线 CONNECT 命令。"""
        command = self._selected_pair_command("CONNECT")
        if command is not None:
            self._send(command)

    def _set_crosspoint(self, closed: bool) -> None:
        """单独切换一个 X-Y 交叉点，不改变矩阵中的其他节点。"""
        try:
            x = int(self.switch_x.get())
            if not 0 <= x <= 23:
                raise ValueError
        except ValueError:
            self._append_log("错误", "单节点通道编号必须在 0 到 23 之间")
            return

        state = "ON" if closed else "OFF"
        self._send(
            f"SWITCH {self.switch_bank.get()} {x} "
            f"{self.switch_bus.get()} {state}"
        )

    def _run_batch_calibration(self) -> None:
        """启动外部短接批量校准；运行期间独占硬件命令通路。"""
        if self._topology_blocks_hardware():
            return
        if self.auxiliary_loop_test.running:
            self._append_log("错误", "辅助回路测试运行中，不能同时启动批量校准")
            return
        if self.sequential_loop_test.running:
            self._append_log("错误", "Y0顺序测试运行中，不能同时启动批量校准")
            return
        if self.pairwise_resistance_test.running:
            self._append_log("错误", "全引脚阻值测试运行中，不能同时启动批量校准")
            return
        if self._dual_node_running():
            self._append_log("错误", "双节点四线闭合运行中，不能同时启动批量校准")
            return
        target_id = self._logical_target_id(self.target_id.get())
        if not target_id or any(character.isspace() for character in target_id):
            self._append_log("错误", "目标ID不能为空且不能包含空格")
            return
        if not self.router.running:
            self._append_log("错误", "服务器尚未启动")
            return
        try:
            started = self.batch_calibration.start(target_id)
        except RuntimeError as error:
            self._append_log("错误", str(error))
            return
        if not started:
            self._append_log("错误", "已有批量校准正在运行")
            return
        self.batch_calibration_button.state(["disabled"])
        self.auxiliary_test_button.state(["disabled"])
        self.sequential_test_button.state(["disabled"])
        self.pairwise_test_button.state(["disabled"])
        self._set_dual_node_button_state(["disabled"])
        self.batch_status.set("批量校准：正在准备")

    def _run_auxiliary_loop_test(self) -> None:
        """启动固定零欧目标的辅助回路扫描；运行期间独占硬件命令通路。"""
        if self._topology_blocks_hardware():
            return
        if self.batch_calibration.running:
            self._append_log("错误", "批量校准运行中，不能同时启动辅助回路测试")
            return
        if self.sequential_loop_test.running:
            self._append_log("错误", "Y0顺序测试运行中，不能同时启动辅助回路测试")
            return
        if self.pairwise_resistance_test.running:
            self._append_log("错误", "全引脚阻值测试运行中，不能同时启动辅助回路测试")
            return
        if self._dual_node_running():
            self._append_log("错误", "双节点四线闭合运行中，不能同时启动辅助回路测试")
            return
        target_id = self._logical_target_id(self.target_id.get())
        if not target_id or any(character.isspace() for character in target_id):
            self._append_log("错误", "目标ID不能为空且不能包含空格")
            return
        if not self.router.running:
            self._append_log("错误", "服务器尚未启动")
            return
        confirmed = messagebox.askokcancel(
            "辅助回路测试",
            "将固定测量 S1_X0 - S2_X0（目标 0 ohm），并依次闭合 "
            "X1 到 X23 辅助回路。测试期间不要操作接线或发送其它命令。\n\n"
            "是否开始？",
            parent=self.root,
        )
        if not confirmed:
            return
        if not self.auxiliary_loop_test.start(target_id):
            self._append_log("错误", "已有辅助回路测试正在运行")
            return
        self.batch_calibration_button.state(["disabled"])
        self.auxiliary_test_button.state(["disabled"])
        self.sequential_test_button.state(["disabled"])
        self.pairwise_test_button.state(["disabled"])
        self._set_dual_node_button_state(["disabled"])
        self.auxiliary_test_status.set("辅助回路测试：正在准备")

    def _toggle_sequential_loop_test(self) -> None:
        """Start or stop the repeating Y0 traversal while owning matrix control."""
        if self._topology_blocks_hardware():
            return
        if self.sequential_loop_test.running:
            self.sequential_loop_test.cancel()
            self.sequential_test_button.state(["disabled"])
            self.sequential_test_status.set("Y0顺序测试：正在停止")
            return
        if self.batch_calibration.running:
            self._append_log("错误", "批量校准运行中，不能启动Y0顺序测试")
            return
        if self.auxiliary_loop_test.running:
            self._append_log("错误", "辅助回路测试运行中，不能启动Y0顺序测试")
            return
        if self.pairwise_resistance_test.running:
            self._append_log("错误", "全引脚阻值测试运行中，不能启动Y0顺序测试")
            return
        if self._dual_node_running():
            self._append_log("错误", "双节点四线闭合运行中，不能启动Y0顺序测试")
            return
        target_id = self._logical_target_id(self.target_id.get())
        if not target_id or any(character.isspace() for character in target_id):
            self._append_log("错误", "目标ID不能为空且不能包含空格")
            return
        if not self.router.running:
            self._append_log("错误", "服务器尚未启动")
            return
        confirmed = messagebox.askokcancel(
            "Y0顺序测试",
            "Y0 固定作为公共起点，将依次打开 Y0→S1_X0、Y0→S2_X0、"
            "Y0→S1_X1、Y0→S2_X1，直到 Y0→S2_X23，并循环运行。"
            "任一时刻只打开一个端口，再次点击按钮可停止。\n\n"
            "测试期间不要发送其它硬件命令，是否开始？",
            parent=self.root,
        )
        if not confirmed:
            return
        if not self.sequential_loop_test.start(target_id):
            self._append_log("错误", "Y0顺序测试已经在运行")
            return
        self.batch_calibration_button.state(["disabled"])
        self.auxiliary_test_button.state(["disabled"])
        self.pairwise_test_button.state(["disabled"])
        self._set_dual_node_button_state(["disabled"])
        self.sequential_test_button.configure(text="停止顺序测试")
        self.sequential_test_status.set("Y0顺序测试：正在准备")

    def _toggle_pairwise_resistance_test(self) -> None:
        """Start or stop both-direction measurements for all port pairs."""
        if self._topology_blocks_hardware():
            return
        if self.pairwise_resistance_test.running:
            self.pairwise_resistance_test.cancel()
            self.pairwise_test_button.state(["disabled"])
            self.pairwise_test_status.set("全引脚阻值：正在停止并生成表格")
            return
        if self.batch_calibration.running:
            self._append_log("错误", "批量校准运行中，不能启动全引脚阻值测试")
            return
        if self.auxiliary_loop_test.running:
            self._append_log("错误", "辅助回路测试运行中，不能启动全引脚阻值测试")
            return
        if self.sequential_loop_test.running:
            self._append_log("错误", "Y0顺序测试运行中，不能启动全引脚阻值测试")
            return
        if self._dual_node_running():
            self._append_log("错误", "双节点四线闭合运行中，不能启动全引脚阻值测试")
            return
        target_id = self._logical_target_id(self.target_id.get())
        if not target_id or any(character.isspace() for character in target_id):
            self._append_log("错误", "目标ID不能为空且不能包含空格")
            return
        if not self.router.running:
            self._append_log("错误", "服务器尚未启动")
            return
        confirmed = messagebox.askokcancel(
            "全引脚阻值测试",
            "将按 S1_X0～S1_X23、S2_X0～S2_X23 的顺序，对 48 个端口进行"
            "全部两两组合的正反向测量，共 1128 对、2256 个方向。每个方向 CONNECT 成功后"
            "固定等待 1 秒，再读取一次电阻；预计至少 37 分 36 秒，通信时间另计。\n\n"
            "测试只记录设备返回的阻值和原始响应，不做校准或合格判断；"
            "若首次测量失败或阻值大于 20 ohm，会保持当前矩阵路径不复位，追加测量 3 次并取三次平均；"
            "正向和反向分别保存，不互相镜像；表格颜色继承各自方向的首次结果：失败标红，超 20 ohm 标黄。"
            "完成或停止后会生成 Excel 阻值矩阵和原始记录。\n\n"
            "测试期间不要操作接线或发送其它硬件命令，是否开始？",
            parent=self.root,
        )
        if not confirmed:
            return
        try:
            started = self.pairwise_resistance_test.start(target_id)
        except RuntimeError as error:
            self._append_log("错误", str(error))
            return
        if not started:
            self._append_log("错误", "全引脚阻值测试已经在运行")
            return
        self.batch_calibration_button.state(["disabled"])
        self.auxiliary_test_button.state(["disabled"])
        self.sequential_test_button.state(["disabled"])
        self._set_dual_node_button_state(["disabled"])
        self.pairwise_test_button.configure(text="停止阻值测试")
        self.pairwise_test_status.set("全引脚阻值：正在准备")

    def _run_dual_node_measurement(self) -> None:
        """Start or stop one fixed-mapping dual-node Kelvin closure."""
        if self._topology_blocks_hardware():
            return
        controller = getattr(self, "dual_node_measurement", None)
        if controller is None:
            self._append_log("错误", "双节点闭合控制器未初始化")
            return
        if controller.running:
            controller.cancel()
            self._set_dual_node_button_state(["disabled"])
            self.dual_node_status.set("双节点四线闭合：正在停止，保持已闭合节点")
            return
        if self.batch_calibration.running:
            self._append_log("错误", "批量校准运行中，不能启动双节点四线闭合")
            return
        if self.auxiliary_loop_test.running:
            self._append_log("错误", "辅助回路测试运行中，不能启动双节点四线闭合")
            return
        if self.sequential_loop_test.running:
            self._append_log("错误", "Y0顺序测试运行中，不能启动双节点四线闭合")
            return
        if self.pairwise_resistance_test.running:
            self._append_log("错误", "全引脚阻值测试运行中，不能启动双节点四线闭合")
            return
        target_id = self._logical_target_id(self.target_id.get())
        if not target_id or any(character.isspace() for character in target_id):
            self._append_log("错误", "目标ID不能为空且不能包含空格")
            return
        if not self.router.running:
            self._append_log("错误", "服务器尚未启动")
            return
        try:
            values = (
                self.dual_positive_bank.get(),
                int(self.dual_positive_x1.get()),
                int(self.dual_positive_x2.get()),
                self.dual_negative_bank.get(),
                int(self.dual_negative_x1.get()),
                int(self.dual_negative_x2.get()),
            )
            started = controller.start(target_id, *values)
        except (TypeError, ValueError) as error:
            self._append_log("错误", str(error) or "双节点 X 参数无效")
            return
        if not started:
            self._append_log("错误", "双节点四线闭合已经在运行")
            return
        self.batch_calibration_button.state(["disabled"])
        self.auxiliary_test_button.state(["disabled"])
        self.sequential_test_button.state(["disabled"])
        self.pairwise_test_button.state(["disabled"])
        self._set_dual_node_button_state(["!disabled"])
        button = getattr(self, "dual_node_measurement_button", None)
        if button is not None:
            button.configure(text="停止双节点闭合")
        self.dual_node_status.set("双节点四线闭合：正在准备")

    def _handle_result_frame(
        self, target_id: str, request_id: str, payload: str
    ) -> None:
        """Route automated raw data and calibrate only human-triggered results."""
        for name in ("node_measurement", "all_matrix_reset"):
            controller = getattr(self, name, None)
            if controller is not None and controller.feed_result(target_id, request_id, payload):
                return
        topology_controller = getattr(self, "topology_scan", None)
        if topology_controller is not None and topology_controller.feed_result(
            target_id, request_id, payload
        ):
            return
        handled_by_batch = self.batch_calibration.feed_result(
            target_id, request_id, payload
        )
        handled_by_auxiliary_test = self.auxiliary_loop_test.feed_result(
            target_id, request_id, payload
        )
        handled_by_sequence = self.sequential_loop_test.feed_result(
            target_id, request_id, payload
        )
        handled_by_pairwise = self.pairwise_resistance_test.feed_result(
            target_id, request_id, payload
        )
        dual_node_controller = getattr(self, "dual_node_measurement", None)
        handled_by_dual_node = bool(
            dual_node_controller is not None
            and dual_node_controller.feed_result(target_id, request_id, payload)
        )
        command = self._pending_commands.pop((target_id, request_id), None)
        display_payload = payload

        if (
            handled_by_batch
            or handled_by_auxiliary_test
            or handled_by_sequence
            or handled_by_pairwise
            or handled_by_dual_node
        ):
            if payload == "OK RESET":
                self._active_routes.pop(target_id, None)
        elif command is not None:
            command_name = command.split(maxsplit=1)[0].upper()
            if command_name == "CONNECT":
                self._active_routes.pop(target_id, None)
                route = parse_connect_result(payload)
                if route is not None:
                    self._active_routes[target_id] = route
                    self._refresh_matrix_status_later(target_id)
            elif command_name == "RESET" and payload == "OK RESET":
                self._active_routes.pop(target_id, None)
                self._refresh_matrix_status_later(target_id)
            elif command_name == "SWITCH" and payload.startswith("OK SWITCH "):
                self._active_routes.pop(target_id, None)
                self._refresh_matrix_status_later(target_id)
            elif command_name == "STATUS":
                snapshot = parse_status_payload(payload)
                if snapshot is not None:
                    self._matrix_status[target_id] = (
                        datetime.now().astimezone().strftime("%H:%M:%S"),
                        snapshot,
                    )
                    self._render_matrix_status(target_id)
            elif command_name == "MEASURE":
                measurement = MEASURE_RESULT_PATTERN.fullmatch(payload.strip())
                if measurement is not None:
                    route = self._active_routes.get(target_id)
                    if route is None:
                        self._append_log(
                            "警告",
                            f"{target_id}没有已确认的四线CONNECT路径，本次显示未校准",
                        )
                    else:
                        try:
                            correction = self.calibration_store.correct(
                                target_id,
                                route[0],
                                route[1],
                                float(measurement.group("resistance")),
                            )
                        except CalibrationProfileError as error:
                            self._append_log("错误", str(error))
                        else:
                            if correction is None:
                                self._append_log(
                                    "警告",
                                    f"{target_id}没有活动校准配置，本次显示原始阻值",
                                )
                            else:
                                display_payload = format_calibrated_measurement(
                                    payload, correction
                                )
                                if correction.corrected_ohm < 0.0:
                                    self._append_log(
                                        "警告",
                                        f"{target_id}校准后阻值为"
                                        f"{correction.corrected_ohm:.3f} ohm，请检查校准或接线",
                                    )

        self._append_log(
            "接收",
            f"{target_id}  {display_payload}",
        )

    def _poll_events(self) -> None:
        """在 Tkinter 主线程消费网络事件并安全更新控件。"""
        while True:
            try:
                event_type, message = self.events.get_nowait()
            except queue.Empty:
                break

            if event_type in {
                "batch_complete",
                "batch_error",
                "aux_test_complete",
                "aux_test_error",
                "sequence_stopped",
                "sequence_error",
                "pairwise_complete",
                "pairwise_stopped",
                "pairwise_error",
            }:
                self._set_dual_node_button_state(["!disabled"])

            if event_type.startswith(("node_measure_", "all_reset_")):
                self._handle_console_operation_event(event_type, message)
            elif event_type == "state":
                state = str(message)
                if state.startswith("running:"):
                    _, host, port = state.split(":", 2)
                    self.status.set(f"服务器监听 {host}:{port}")
                    self.status_label.configure(foreground="#166534")
                    self._append_log("状态", f"服务器已启动 {host}:{port}")
                else:
                    self._shutdown_pending = False
                    self._pending_commands.clear()
                    self._request_routes.clear()
                    self.status.set("服务器未启动")
                    self.status_label.configure(foreground="#9A3412")
                    self._append_log("状态", "服务器已停止")
            elif event_type == "devices":
                devices = tuple(message) if isinstance(message, tuple) else ()
                self._online_master_ids = tuple(
                    str(device) for device in devices
                    if str(device) in MASTER_WIFI_IDS
                )
                self._online_slave_ids = tuple(
                    str(device) for device in devices
                    if str(device).lower() in SLAVE_WIFI_IDS
                )
                self._refresh_node_selectors()
                panel = getattr(self, "topology_panel", None)
                if panel is not None and panel.window.winfo_exists():
                    panel.set_devices(self._online_master_ids)
                for key in tuple(self._pending_commands):
                    scoped = self._split_scoped_master_target(key[0])
                    transport = scoped[0] if scoped else key[0]
                    if transport not in devices:
                        del self._pending_commands[key]
                for key in tuple(self._request_routes):
                    if key[0] not in devices:
                        del self._request_routes[key]
                if (
                    self._online_master_ids
                    and self._selected_master_id() not in self._online_master_ids
                ):
                    self.master_id.set(self._online_master_ids[0])
                self._refresh_target_selector()
                self.online_devices.set(
                    "在线设备：" + (", ".join(devices) if devices else "无")
                )
            elif event_type.startswith("topology_"):
                self._handle_topology_event(event_type, message)
            elif event_type == "sent":
                self._append_log("发送", str(message))
            elif event_type == "received":
                self._append_log("接收", str(message))
            elif event_type == "controller_ack":
                target_id, request_id, acknowledgement = message  # type: ignore[misc]
                acknowledgement_text = str(acknowledgement)
                if not acknowledgement_text.startswith("OK FORWARDED "):
                    self._pending_commands.pop(
                        (str(target_id), str(request_id)), None
                    )
                self._append_log("接收", acknowledgement_text)
            elif event_type == "result_frame":
                transport_target, request_id, payload = message  # type: ignore[misc]
                transport_target = str(transport_target)
                request_id = str(request_id)
                target_id = self._logical_result_target(
                    transport_target, request_id
                )
                self._handle_result_frame(
                    target_id, request_id, str(payload)
                )
            elif event_type == "batch_started":
                self.batch_status.set("批量校准：运行中")
                self._append_log("批量校准", str(message))
            elif event_type == "batch_progress":
                self.batch_status.set(f"批量校准：{message}")
            elif event_type == "batch_complete":
                result = message if isinstance(message, dict) else {}
                self.batch_calibration_button.state(["!disabled"])
                self.auxiliary_test_button.state(["!disabled"])
                self.sequential_test_button.state(["!disabled"])
                self.pairwise_test_button.state(["!disabled"])
                target_id = str(result.get("target_id", "-"))
                self.batch_status.set(
                    f"批量校准：{target_id}完成，已解"
                    f"{result.get('solved', 0)}/48路"
                )
                self._append_log("批量校准", f"Word报告：{result.get('docx', '-')}")
                self._append_log("批量校准", f"测量数据：{result.get('json', '-')}")
                self._append_log(
                    "批量校准",
                    f"{target_id}新配置已加入候选，请选择是否应用",
                )
                self._refresh_calibration_status()
                warning_count = int(result.get("warnings", 0))
                self._append_log(
                    "警告" if warning_count else "批量校准",
                    f"质量警告：{warning_count}项",
                )
            elif event_type == "batch_error":
                self.batch_calibration_button.state(["!disabled"])
                self.auxiliary_test_button.state(["!disabled"])
                self.sequential_test_button.state(["!disabled"])
                self.pairwise_test_button.state(["!disabled"])
                self.batch_status.set("批量校准：失败或取消")
                self._append_log("错误", str(message))
            elif event_type == "batch_reset_warning":
                self._append_log("警告", str(message))
            elif event_type == "aux_test_started":
                self.auxiliary_test_status.set("辅助回路测试：运行中")
                self._append_log("辅助回路测试", str(message))
            elif event_type == "aux_test_progress":
                progress = str(message)
                self.auxiliary_test_status.set(
                    f"辅助回路测试：{progress[:60]}"
                )
                self._append_log("辅助回路测试", progress)
            elif event_type == "aux_test_complete":
                result = message if isinstance(message, dict) else {}
                self.batch_calibration_button.state(["!disabled"])
                self.auxiliary_test_button.state(["!disabled"])
                self.sequential_test_button.state(["!disabled"])
                self.pairwise_test_button.state(["!disabled"])
                candidate_x = result.get("candidate_x", "-")
                baseline = result.get("baseline_median_ohm")
                assisted = result.get("assisted_median_ohm")
                improvement = result.get("improvement_ohm")
                confirmed = bool(result.get("improvement_confirmed"))
                self.auxiliary_test_status.set(
                    f"辅助回路测试：完成，最佳 X{candidate_x}"
                )
                summary = (
                    f"最佳回路 S1_X{candidate_x}-S2_X{candidate_x}；"
                    f"基线={self._format_optional_ohm(baseline)}，"
                    f"闭合后={self._format_optional_ohm(assisted)}，"
                    f"改善={self._format_optional_ohm(improvement, signed=True)}"
                )
                self._append_log(
                    "辅助回路测试" if confirmed else "警告", summary
                )
                self._append_log(
                    "辅助回路测试", f"JSON报告：{result.get('json', '-')}"
                )
                self._append_log(
                    "辅助回路测试", f"CSV数据：{result.get('csv', '-')}"
                )
                messagebox.showinfo(
                    "辅助回路测试完成",
                    summary
                    + ("\n改善已超过观测波动。" if confirmed else "\n改善未超过观测波动。"),
                    parent=self.root,
                )
            elif event_type == "aux_test_error":
                self.batch_calibration_button.state(["!disabled"])
                self.auxiliary_test_button.state(["!disabled"])
                self.sequential_test_button.state(["!disabled"])
                self.pairwise_test_button.state(["!disabled"])
                self.auxiliary_test_status.set("辅助回路测试：失败或取消")
                self._append_log("错误", str(message))
            elif event_type == "aux_test_reset_warning":
                self._append_log("警告", str(message))
            elif event_type == "sequence_started":
                self.sequential_test_status.set("Y0顺序测试：运行中")
                self._append_log("Y0顺序测试", str(message))
            elif event_type == "sequence_progress":
                progress = message if isinstance(message, dict) else {}
                progress_text = (
                    f"第 {progress.get('cycle', '-')} 轮 "
                    f"{progress.get('index', '-')}/{progress.get('total', 48)} "
                    f"{progress.get('point', '-')}"
                )
                self.sequential_test_status.set(f"Y0顺序测试：{progress_text}")
            elif event_type == "sequence_stopped":
                result = message if isinstance(message, dict) else {}
                self.batch_calibration_button.state(["!disabled"])
                self.auxiliary_test_button.state(["!disabled"])
                self.sequential_test_button.state(["!disabled"])
                self.pairwise_test_button.state(["!disabled"])
                self.sequential_test_button.configure(text="Y0顺序测试")
                summary = (
                    f"已停止，完成 {result.get('completed_cycles', 0)} 轮、"
                    f"{result.get('completed_steps', 0)} 个点"
                )
                self.sequential_test_status.set(f"Y0顺序测试：{summary}")
                self._append_log("Y0顺序测试", summary)
            elif event_type == "sequence_error":
                self.batch_calibration_button.state(["!disabled"])
                self.auxiliary_test_button.state(["!disabled"])
                self.sequential_test_button.state(["!disabled"])
                self.pairwise_test_button.state(["!disabled"])
                self.sequential_test_button.configure(text="Y0顺序测试")
                self.sequential_test_status.set("Y0顺序测试：失败")
                self._append_log("错误", str(message))
            elif event_type == "sequence_reset_warning":
                self._append_log("警告", str(message))
            elif event_type == "pairwise_started":
                self.pairwise_test_status.set("全引脚阻值：运行中")
                self._append_log("全引脚阻值", str(message))
            elif event_type == "pairwise_progress":
                progress = message if isinstance(message, dict) else {}
                self.pairwise_test_status.set(
                    "全引脚阻值："
                    f"{progress.get('index', '-')}/{progress.get('total', 2256)} "
                    f"{progress.get('pair', '-')}"
                )
            elif event_type in {"pairwise_complete", "pairwise_stopped"}:
                result = message if isinstance(message, dict) else {}
                self.batch_calibration_button.state(["!disabled"])
                self.auxiliary_test_button.state(["!disabled"])
                self.sequential_test_button.state(["!disabled"])
                self.pairwise_test_button.state(["!disabled"])
                self.pairwise_test_button.configure(text="全引脚阻值")
                prefix = "完成" if event_type == "pairwise_complete" else "已停止"
                summary = (
                    f"{prefix} {result.get('completed', 0)}/"
                    f"{result.get('total', 2256)} 个方向，"
                    f"成功 {result.get('successful', 0)} 个方向"
                )
                self.pairwise_test_status.set(f"全引脚阻值：{summary}")
                self._append_log("全引脚阻值", summary)
                self._append_log(
                    "全引脚阻值", f"Excel表格：{result.get('xlsx', '-')}"
                )
            elif event_type == "pairwise_error":
                self.batch_calibration_button.state(["!disabled"])
                self.auxiliary_test_button.state(["!disabled"])
                self.sequential_test_button.state(["!disabled"])
                self.pairwise_test_button.state(["!disabled"])
                self.pairwise_test_button.configure(text="全引脚阻值")
                self.pairwise_test_status.set("全引脚阻值：失败")
                self._append_log("错误", str(message))
            elif event_type == "pairwise_reset_warning":
                self._append_log("警告", str(message))
            elif event_type == "dual_node_started":
                details = message if isinstance(message, dict) else {}
                self.dual_node_status.set("双节点四线闭合：运行中")
                self._append_log(
                    "双节点四线闭合",
                    f"正端 {details.get('positive', '-')}；负端 {details.get('negative', '-')}；"
                    f"{details.get('mapping', '')}",
                )
            elif event_type == "dual_node_progress":
                progress = message if isinstance(message, dict) else {}
                phase = "闭合中" if progress.get("phase") == "connecting" else "稳定中"
                self.dual_node_status.set(
                    "双节点四线闭合："
                    f"{phase} {progress.get('index', '-')}/{progress.get('total', 4)}"
                )
            elif event_type in {"dual_node_complete", "dual_node_stopped"}:
                result = message if isinstance(message, dict) else {}
                self._restore_controls_after_dual_node()
                button = getattr(self, "dual_node_measurement_button", None)
                if button is not None:
                    button.configure(text="执行双节点闭合")
                if event_type == "dual_node_complete":
                    summary = (
                        f"完成：{result.get('positive', '-')} -> {result.get('negative', '-')}; "
                        "四个节点已闭合并保持"
                    )
                else:
                    summary = "已停止，已发送的节点保持闭合"
                self.dual_node_status.set(f"双节点四线闭合：{summary}")
                self._append_log("双节点四线闭合", summary)
            elif event_type == "dual_node_error":
                self._restore_controls_after_dual_node()
                button = getattr(self, "dual_node_measurement_button", None)
                if button is not None:
                    button.configure(text="执行双节点闭合")
                self.dual_node_status.set("双节点四线闭合：失败")
                self._append_log("错误", str(message))
            elif event_type == "network":
                self._append_log("网络", str(message))
            else:
                self._append_log("错误", str(message))

        if self._topology_running():
            self._set_topology_interlock(True)
        self.root.after(50, self._poll_events)

    @staticmethod
    def _format_optional_ohm(value: object, *, signed: bool = False) -> str:
        """Format an optional numeric result for the GUI completion summary."""
        if value is None:
            return "-"
        number = float(value)
        return f"{number:+.3f} ohm" if signed else f"{number:.3f} ohm"

    def _entry_visible(self, entry: LogEntry) -> bool:
        """Return whether one stored row should currently appear in the text view."""
        return not (self.filter_normal_logs.get() and entry.level == "normal")

    def _render_log(self) -> None:
        """Redraw the filtered log history and keep the newest row in view."""
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        for entry in self._log_entries:
            self._insert_log_entry(entry)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _insert_log_entry(self, entry: LogEntry) -> None:
        """Insert one visible row while the text widget is writable."""
        if not self._entry_visible(entry):
            return
        level_label = LOG_LEVEL_LABELS.get(entry.level, entry.level)
        self.log.insert(
            "end",
            f"[{entry.timestamp}] [{level_label}] "
            f"{entry.category}: {entry.message}\n",
            (entry.level,),
        )

    def _append_log(self, category: str, message: str) -> None:
        """Store one timestamped row, filtering routine traffic at the view layer."""
        entry = LogEntry(
            timestamp=datetime.now().strftime("%H:%M:%S"),
            category=category,
            message=message,
            level=classify_log_level(category, message),
        )
        was_full = len(self._log_entries) == self._log_entries.maxlen
        self._log_entries.append(entry)

        if self.log_paused.get():
            self._paused_log_count += 1
            self.log_status.set(f"已暂停，缓存 {self._paused_log_count} 条")
            return
        if was_full:
            self._render_log()
        elif self._entry_visible(entry):
            self.log.configure(state="normal")
            self._insert_log_entry(entry)
            self.log.see("end")
            self.log.configure(state="disabled")

    def _log_filter_changed(self) -> None:
        """Apply the normal-traffic filter without disturbing a paused view."""
        if not self.log_paused.get():
            self._render_log()

    def _toggle_log_pause(self) -> None:
        """Freeze or resume log rendering while network and test events continue."""
        paused = not self.log_paused.get()
        self.log_paused.set(paused)
        if paused:
            self._paused_log_count = 0
            self.log_status.set("已暂停")
            self.log_pause_button.configure(text="继续日志")
            return

        self._render_log()
        self._paused_log_count = 0
        self.log_status.set("实时显示")
        self.log_pause_button.configure(text="暂停日志")

    def _clear_log(self) -> None:
        """Clear stored and visible logs without affecting network connections."""
        self._log_entries.clear()
        self._paused_log_count = 0
        self._render_log()
        if self.log_paused.get():
            self.log_status.set("已暂停")

    def _close(self) -> None:
        """关闭窗口前停止服务器并断开所有 ESP，避免监听端口残留。"""
        if getattr(self, "_closing", False):
            return
        self._closing = True
        self._shutdown_pending = True
        self._cancel_hardware_controllers()
        self._finish_close()

    def _finish_close(self) -> None:
        """Allow in-flight RESET replies to reach workers before destroying Tk."""
        if self._hardware_running():
            self.status.set("正在停止测试并复位矩阵...")
            self.root.after(50, self._finish_close)
            return
        self.router.stop()
        self.root.destroy()


def main() -> None:
    """创建 Tkinter 主窗口并进入 GUI 事件循环。"""
    root = tk.Tk()
    CableTesterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
