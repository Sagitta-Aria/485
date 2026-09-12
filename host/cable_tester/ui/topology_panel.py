"""Tk workspace for expandable cable scans; controller callbacks own hardware work."""

from __future__ import annotations

import csv
import math
import re
import shutil
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from cable_tester.analysis.topology_scan import Codebook, MAX_BINARY_REPEATS, MAX_MODULES, PORTS_PER_MODULE, TopologyPort, build_ports


PORT_PATTERN = re.compile(r"slave(\d+)-G(\d+)")
STATUS_LABELS = {
    "UNIQUE": "单一连接", "NO_CONTINUITY": "无跨端导通",
    "SHORT_CANDIDATES": "待补测", "UNKNOWN": "未确定",
    "INCONSISTENT": "结果不一致", "SHORT": "多端导通",
}
FLAG_LABELS = {"MISWIRE": "错接", "DUPLICATE_TARGET": "目标重复"}
RESISTANCE_LABELS = {
    "NOT_MEASURED": "未测", "MEASUREMENT_ERROR": "测量失败",
    "INVALID_NUMERIC_READING": "无效读数", "THRESHOLD_UNCERTAIN": "阈值区间",
    "HIGH_RESISTANCE": "高阻", "NO_CONTINUITY_OR_OVERRANGE": "OL / 无导通",
    "UNSTABLE_READING": "重复读数不一致",
}


def format_point_resistance(reading: dict[str, object], *, with_unit: bool = False) -> str:
    """Format point evidence without treating missing/error/overrange readings as zero."""
    value = reading.get("resistance_ohm")
    reason = str(reading.get("reason", "NOT_MEASURED"))
    label = RESISTANCE_LABELS.get(reason, reason)
    if value is None:
        return label
    text = f"{value:.3f}" + (" ohm" if with_unit else "")
    return text if reason == "CONTINUITY" else f"{text} ({label})"


def read_expected_mapping(path: Path, left_modules: int, right_modules: int) -> dict[TopologyPort, TopologyPort]:
    """Load source,target CSV labels such as slave1-G1 without inventing missing rows."""
    mapping: dict[TopologyPort, TopologyPort] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["source", "target"]:
            raise ValueError("CSV 表头必须为 source,target")
        for line_number, row in enumerate(reader, 2):
            ports = []
            for field, count in (("source", left_modules), ("target", right_modules)):
                match = PORT_PATTERN.fullmatch(str(row.get(field) or "").strip())
                if match is None:
                    raise ValueError(f"CSV 第 {line_number} 行 {field} 格式应为 slave1-G1")
                module, local = map(int, match.groups())
                if not 1 <= module <= count or not 1 <= local <= PORTS_PER_MODULE:
                    raise ValueError(f"CSV 第 {line_number} 行 {field} 超出计划端口范围")
                ports.append(TopologyPort(module - 1, local - 1))
            if ports[0] in mapping:
                raise ValueError(f"CSV 第 {line_number} 行源端口重复")
            mapping[ports[0]] = ports[1]
    if not mapping:
        raise ValueError("CSV 接线表为空")
    return mapping


class TopologyPanel:
    """Present scan events on Tk's thread, retaining unknown and partial results."""

    def __init__(self, parent: tk.Misc, start_scan: Callable[..., bool], cancel_scan: Callable[[], object]) -> None:
        self.window = tk.Toplevel(parent)
        self.window.title("线缆接线拓扑")
        self.window.geometry("1060x740")
        self.window.minsize(860, 640)
        self.window.protocol("WM_DELETE_WINDOW", self.window.withdraw)
        self._start_scan, self._cancel_scan = start_scan, cancel_scan
        self._running = False
        self._devices: tuple[str, ...] = ()
        self._rows: dict[str, dict[str, object]] = {}
        self._reports: dict[str, str] = {}
        self._recheck_parameters: dict[str, object] | None = None
        self._pending_recheck_count = 0
        self._saved_scan_controls: list[tuple[tk.Variable, str]] = []
        self._saved_expected_path: Path | None = None
        self._expected_path: Path | None = None
        self.left_master = tk.StringVar(self.window)
        self.right_master = tk.StringVar(self.window)
        self.left_modules = tk.StringVar(self.window, value="7")
        self.right_modules = tk.StringVar(self.window, value="7")
        self.on_threshold = tk.StringVar(self.window, value="50")
        self.off_threshold = tk.StringVar(self.window, value="100")
        self.settle_seconds = tk.StringVar(self.window, value="1.0")
        self.scan_method = tk.StringVar(self.window, value="coded")
        self.binary_repeats = tk.StringVar(self.window, value="1")
        self.expected_mode = tk.StringVar(self.window, value="直通")
        self.expected_file = tk.StringVar(self.window, value="未导入")
        self.plan_summary = tk.StringVar(self.window)
        self.left_discovery = tk.StringVar(self.window, value="左端：未验证")
        self.right_discovery = tk.StringVar(self.window, value="右端：未验证")
        self.status = tk.StringVar(self.window, value="未运行")
        self.report_status = tk.StringVar(self.window, value="报告：无")
        self.anomalies_only = tk.BooleanVar(self.window, value=False)
        self._inputs: list[ttk.Widget] = []
        self._build()
        for variable in (self.left_modules, self.right_modules, self.scan_method, self.binary_repeats):
            variable.trace_add("write", self._refresh_plan)
        self._refresh_plan()

    def _build(self) -> None:
        """Use compact parameter rows and a scrolling source-to-target result table."""
        frame = ttk.Frame(self.window, padding=16)
        frame.grid(sticky="nsew")
        self.window.rowconfigure(0, weight=1)
        self.window.columnconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(7, weight=1)
        ttk.Label(frame, text="线缆接线拓扑", style="Title.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))
        endpoints = ttk.Frame(frame)
        endpoints.grid(row=1, column=0, sticky="ew")
        endpoints.columnconfigure(1, weight=1)
        endpoints.columnconfigure(3, weight=1)
        for offset, title, master, count in (
            (0, "左端 Master", self.left_master, self.left_modules),
            (2, "右端 Master", self.right_master, self.right_modules),
        ):
            ttk.Label(endpoints, text=title).grid(row=0, column=offset, sticky="w", padx=(0, 8))
            selector = ttk.Combobox(endpoints, textvariable=master, state="readonly", width=20)
            selector.grid(row=0, column=offset + 1, sticky="ew", padx=(0, 16))
            self._inputs.append(selector)
            setattr(self, "left_selector" if offset == 0 else "right_selector", selector)
            ttk.Label(endpoints, text="计划从机数").grid(row=1, column=offset, sticky="w", padx=(0, 8), pady=(8, 0))
            spinner = ttk.Spinbox(endpoints, from_=1, to=MAX_MODULES, textvariable=count, width=8)
            spinner.grid(row=1, column=offset + 1, sticky="w", pady=(8, 0))
            self._inputs.append(spinner)
        ttk.Label(endpoints, text="扫描方法").grid(row=2, column=0, sticky="w", pady=(8, 0))
        methods = ttk.Frame(endpoints)
        methods.grid(row=2, column=1, columnspan=3, sticky="w", pady=(8, 0))
        for column, (value, label) in enumerate((("coded", "拓扑编码"), ("binary", "二分扫描"))):
            button = ttk.Radiobutton(methods, text=label, variable=self.scan_method, value=value)
            button.grid(row=0, column=column, padx=(0, 20))
            self._inputs.append(button)
        ttk.Label(methods, text="二分采样次数").grid(row=0, column=2, padx=(0, 8))
        self.binary_repeat_spinner = ttk.Spinbox(methods, from_=1, to=MAX_BINARY_REPEATS,
                                                textvariable=self.binary_repeats, width=5)
        self.binary_repeat_spinner.grid(row=0, column=3)
        self._inputs.append(self.binary_repeat_spinner)
        ttk.Label(frame, textvariable=self.plan_summary, wraplength=800).grid(row=2, column=0, sticky="w", pady=(10, 8))
        measurement = ttk.Frame(frame)
        measurement.grid(row=3, column=0, sticky="ew")
        for column, label, variable in (
            (0, "导通上限 (ohm)", self.on_threshold), (2, "断开下限 (ohm)", self.off_threshold),
            (4, "稳定时间 (s)", self.settle_seconds),
        ):
            ttk.Label(measurement, text=label).grid(row=0, column=column, padx=(0, 8))
            entry = ttk.Entry(measurement, textvariable=variable, width=9)
            entry.grid(row=0, column=column + 1, padx=(0, 16))
            self._inputs.append(entry)
        ttk.Label(measurement, text="预期接线").grid(row=1, column=0, sticky="w", pady=(8, 0))
        mode = ttk.Combobox(measurement, textvariable=self.expected_mode, values=("直通", "仅记录", "CSV"), state="readonly", width=9)
        mode.grid(row=1, column=1, sticky="w", pady=(8, 0))
        self._inputs.append(mode)
        import_button = ttk.Button(measurement, text="导入接线 CSV", command=self._import_expected)
        import_button.grid(row=1, column=2, sticky="w", pady=(8, 0))
        self._inputs.append(import_button)
        ttk.Label(measurement, textvariable=self.expected_file, wraplength=320).grid(row=1, column=3, columnspan=3, sticky="w", pady=(8, 0))
        discovery = ttk.Frame(frame)
        discovery.grid(row=4, column=0, sticky="ew", pady=(12, 8))
        discovery.columnconfigure(0, weight=1)
        discovery.columnconfigure(1, weight=1)
        ttk.Label(discovery, textvariable=self.left_discovery, wraplength=380).grid(row=0, column=0, sticky="nw")
        ttk.Label(discovery, textvariable=self.right_discovery, wraplength=380).grid(row=0, column=1, sticky="nw")
        actions = ttk.Frame(frame)
        actions.grid(row=5, column=0, sticky="ew")
        actions.columnconfigure(4, weight=1)
        self.start_button = ttk.Button(actions, text="开始扫描", command=self._start)
        self.start_button.grid(row=0, column=0, padx=(0, 8))
        self.stop_button = ttk.Button(actions, text="停止", command=self._cancel, state="disabled")
        self.stop_button.grid(row=0, column=1, padx=(0, 16))
        self.recheck_button = ttk.Button(actions, text="补测待确认", command=self._recheck, state="disabled")
        self.recheck_button.grid(row=0, column=2, padx=(0, 16))
        ttk.Checkbutton(actions, text="仅异常 / 未确定", variable=self.anomalies_only, command=self._refresh_rows).grid(row=0, column=3, sticky="w")
        for column, extension in ((5, "json"), (6, "csv")):
            button = ttk.Button(actions, text=f"导出 {extension.upper()}", state="disabled", command=lambda suffix=extension: self._export(suffix))
            button.grid(row=0, column=column, padx=(8, 0))
            setattr(self, f"{extension}_button", button)
        progress = ttk.Frame(frame)
        progress.grid(row=6, column=0, sticky="ew", pady=(10, 8))
        progress.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress, textvariable=self.status, wraplength=800).grid(row=1, column=0, sticky="w", pady=(4, 0))
        results = ttk.Frame(frame)
        results.grid(row=7, column=0, sticky="nsew")
        results.rowconfigure(0, weight=1)
        results.columnconfigure(0, weight=1)
        self.table = ttk.Treeview(results, columns=("source", "status", "targets", "resistance", "signature", "candidates"), show="headings", selectmode="browse", height=10)
        for name, label, width in (
            ("source", "左端口", 100), ("status", "状态", 160), ("targets", "已确认右端口", 150),
            ("resistance", "单点电阻 (ohm)", 210), ("signature", "编码结果", 100), ("candidates", "待确认右端口", 200),
        ):
            self.table.heading(name, text=label)
            self.table.column(name, width=width, minwidth=width, stretch=name in {"targets", "candidates"})
        self.table.tag_configure("unknown", foreground="#647067")
        self.table.tag_configure("fault", foreground="#a12d37")
        self.table.tag_configure("normal", foreground="#206440")
        vertical = ttk.Scrollbar(results, orient="vertical", command=self.table.yview)
        horizontal = ttk.Scrollbar(results, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.table.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.table.bind("<<TreeviewSelect>>", self._show_row)
        details = ttk.Frame(frame)
        details.grid(row=8, column=0, sticky="ew", pady=(8, 0))
        details.columnconfigure(0, weight=1)
        self.detail = tk.Text(details, height=5, wrap="word", state="disabled", relief="flat")
        self.detail.grid(row=0, column=0, sticky="ew")
        detail_scroll = ttk.Scrollbar(details, orient="vertical", command=self.detail.yview)
        detail_scroll.grid(row=0, column=1, sticky="ns")
        self.detail.configure(yscrollcommand=detail_scroll.set)
        ttk.Label(frame, textvariable=self.report_status, wraplength=800).grid(row=9, column=0, sticky="w", pady=(6, 0))

    def _counts(self) -> tuple[int, int]:
        """Validate editable module counts before planning any hardware request."""
        try:
            counts = int(self.left_modules.get()), int(self.right_modules.get())
        except ValueError as error:
            raise ValueError("左右计划从机数必须是 1 到 10 的整数") from error
        if any(not 1 <= count <= MAX_MODULES for count in counts):
            raise ValueError("左右计划从机数必须是 1 到 10 的整数")
        return counts

    def _binary_repeat_count(self) -> int:
        """Validate editable repeat counts before any scan is submitted."""
        try:
            count = int(self.binary_repeats.get())
        except ValueError as error:
            raise ValueError(f"二分采样次数必须是 1 到 {MAX_BINARY_REPEATS} 的整数") from error
        if not 1 <= count <= MAX_BINARY_REPEATS:
            raise ValueError(f"二分采样次数必须是 1 到 {MAX_BINARY_REPEATS} 的整数")
        return count

    def _refresh_plan(self, *_arguments: object) -> None:
        """Keep the initial screening count separate from fault-dependent extra tests."""
        binary = self.scan_method.get() == "binary"
        self.binary_repeat_spinner.state(["disabled"] if self._running or not binary else ["!disabled"])
        try:
            left, right = self._counts()
            repeats = self._binary_repeat_count() if binary else 1
        except ValueError as error:
            self.plan_summary.set(str(error))
            return
        if binary:
            self.plan_summary.set(f"计划：{left * 24} 对 {right * 24} 路   |   二分扫描   |   每项 {repeats} 次采样")
        else:
            rounds = Codebook.create(build_ports(range(right))).rounds
            self.plan_summary.set(f"计划：{left * 24} 对 {right * 24} 路   |   {rounds} 轮编码 / 左端口   |   初筛 {left * 24 * rounds} 次；异常补测、接线电阻另计")

    def set_devices(self, devices: tuple[str, ...]) -> None:
        """Refresh master choices while retaining the identities of an active scan."""
        self._devices = devices
        self.left_selector.configure(values=devices)
        self.right_selector.configure(values=devices)
        if self._running:
            return
        if self.left_master.get() not in devices:
            self.left_master.set(devices[0] if devices else "")
        if self.right_master.get() not in devices or self.right_master.get() == self.left_master.get():
            alternatives = [device for device in devices if device != self.left_master.get()]
            self.right_master.set(alternatives[0] if alternatives else "")
        self._refresh_recheck()

    def _import_expected(self) -> None:
        """Validate the chosen expected-map file before selecting CSV mode."""
        selected = filedialog.askopenfilename(parent=self.window, title="预期接线表", filetypes=[("CSV", "*.csv")])
        if not selected:
            return
        try:
            mapping = read_expected_mapping(Path(selected), *self._counts())
        except (OSError, UnicodeError, ValueError) as error:
            messagebox.showerror("接线表无效", str(error), parent=self.window)
            return
        self._expected_path = Path(selected)
        self.expected_mode.set("CSV")
        self.expected_file.set(f"{self._expected_path.name} ({len(mapping)} 路)")

    def _start(self) -> None:
        """Submit the plan through the app's interlock and let the worker verify roles."""
        try:
            left_count, right_count = self._counts()
            left, right = self.left_master.get(), self.right_master.get()
            if left not in self._devices or right not in self._devices or left == right:
                raise ValueError("请选择两个不同的在线 Master")
            expected = None
            if self.expected_mode.get() == "CSV":
                if self._expected_path is None:
                    raise ValueError("尚未导入预期接线 CSV")
                expected = read_expected_mapping(self._expected_path, left_count, right_count)
            elif self.expected_mode.get() == "直通":
                expected = {port: port for port in build_ports(range(min(left_count, right_count)))}
            on, off, settle = float(self.on_threshold.get()), float(self.off_threshold.get()), float(self.settle_seconds.get())
            if not all(math.isfinite(value) for value in (on, off, settle)):
                raise ValueError("阈值和稳定时间必须是有限数值")
            if not 0 <= on < off or not 0 <= settle <= 5:
                raise ValueError("阈值须满足 0 <= 导通上限 < 断开下限；稳定时间为 0 到 5 秒")
            repeats = self._binary_repeat_count() if self.scan_method.get() == "binary" else 1
            started = self._start_scan(left_master=left, right_master=right, left_modules=left_count, right_modules=right_count, on_threshold_ohm=on, off_threshold_ohm=off, settle_seconds=settle, expected_mapping=expected, scan_method=self.scan_method.get(), binary_repeats=repeats)
        except (OSError, UnicodeError, ValueError) as error:
            messagebox.showerror("扫描参数无效", str(error), parent=self.window)
            return
        if started:
            self._saved_scan_controls = [(variable, variable.get()) for variable in (
                self.left_master, self.right_master, self.left_modules, self.right_modules,
                self.on_threshold, self.off_threshold, self.settle_seconds, self.scan_method,
                self.binary_repeats, self.expected_mode, self.expected_file)]
            self._saved_expected_path = self._expected_path
            self._set_busy(True)
            self.status.set("正在验证 Master 和从机状态")
        else:
            self.status.set("未启动：设备忙、未连接或参数无效，详情见主窗口日志")

    def _refresh_recheck(self) -> None:
        """Only offer the saved scan's pending pairs after hardware ownership is released."""
        parameters = self._recheck_parameters
        available = bool(parameters and self._pending_recheck_count and not self._running
                         and all(parameters[name] in self._devices for name in ("left_master", "right_master")))
        self.recheck_button.configure(text=f"补测待确认 ({self._pending_recheck_count})" if self._pending_recheck_count else "补测待确认")
        self.recheck_button.state(["!disabled"] if available else ["disabled"])

    def _recheck(self) -> None:
        """Run one point sample per pending pair through the existing app interlock."""
        if self._running or not self._recheck_parameters or self.recheck_button.instate(["disabled"]):
            return
        try:
            started = self._start_scan(**self._recheck_parameters)
        except (OSError, ValueError, RuntimeError) as error:
            messagebox.showerror("补测未启动", str(error), parent=self.window)
            return
        if started:
            for variable, value in self._saved_scan_controls:
                variable.set(value)
            self._expected_path = self._saved_expected_path
            self._set_busy(True)
            self.status.set(f"正在验证原扫描设备 | 待补测 {self._pending_recheck_count} 对")
        else:
            self.status.set("补测未启动：设备忙或未连接，详情见主窗口日志")

    def _set_busy(self, busy: bool) -> None:
        """Freeze plan inputs so the shown configuration belongs to the active run."""
        self._running = busy
        for widget in self._inputs:
            widget.state(["disabled"] if busy else ["!disabled"])
        self.start_button.state(["disabled"] if busy else ["!disabled"])
        self.stop_button.state(["!disabled"] if busy else ["disabled"])
        self._refresh_plan()
        self._refresh_recheck()

    def _cancel(self) -> None:
        """Keep the panel responsive while masters acknowledge cancellation."""
        self._cancel_scan()
        self.stop_button.state(["disabled"])
        self.status.set("正在停止并复位矩阵")

    def handle_event(self, event_type: str, message: object) -> None:
        """Apply Tk-thread events without converting unmeasured rows into open circuits."""
        data = message if isinstance(message, dict) else {}
        if event_type == "topology_started":
            self.table.heading("signature", text="扫描方法" if data.get("scan_method") == "binary" else "编码结果")
            self._rows.clear()
            self._reports.clear()
            self._recheck_parameters = None
            self._pending_recheck_count = 0
            self.json_button.state(["disabled"])
            self.csv_button.state(["disabled"])
            self.report_status.set("报告：扫描结束后生成")
            self.left_discovery.set("左端：验证中")
            self.right_discovery.set("右端：验证中")
            self.progress.configure(maximum=max(int(data.get("total", 1)), 1), value=0)
            for index in range(int(data.get("left_ports", 0))):
                source = TopologyPort(index // 24, index % 24).label
                self._rows[source] = {"source": source, "status": "UNKNOWN"}
            self._refresh_rows()
            self._set_busy(True)
        elif event_type == "topology_discovered":
            count, online = int(data.get("count", 0)), int(data.get("online", 0))
            modules = [f"slave{module + 1}" for module in range(count) if online & (1 << module)]
            missing = [f"slave{module + 1}" for module in range(count) if not online & (1 << module)]
            summary = f"{data.get('master', '?')} | 在线 {len(modules)}/{count}: {','.join(modules) or '-'}"
            if missing:
                summary += " | 缺失 " + ",".join(missing)
            (self.left_discovery if data.get("side") == "left" else self.right_discovery).set(summary)
        elif event_type == "topology_progress":
            completed, total = int(data.get("completed", 0)), int(data.get("total", 1))
            self.progress.configure(maximum=max(total, 1), value=completed)
            phase = {"coded": "编码初筛", "point": "异常补测", "resistance": "接线电阻", "binary": "二分确认", "recheck": "待确认补测"}.get(str(data.get("phase")), "扫描")
            round_index = int(data.get("round", -1))
            suffix = f" | 轮次 {round_index + 1}" if round_index >= 0 else ""
            if data.get("target"):
                suffix += f" -> {data['target']}"
            self.status.set(f"{phase} | 测量 {completed}/{total} | {data.get('source', '-')}{suffix}")
        elif event_type == "topology_row":
            source = str(data.get("source", ""))
            if source:
                self._rows[source] = dict(data)
                self._render_row(source)
        elif event_type == "topology_transfer_mode":
            self.status.set("已启用缓存确认传输" if data.get("mode") == "durable_cached" else "兼容传输：连接中断后需重新扫描")
        elif event_type == "topology_salvaging":
            self.status.set(f"正在保存 {data.get('master', '')} 上次扫描的缓存数据")
        elif event_type == "topology_paused":
            reason = data.get("reason")
            if reason == "CACHE":
                self.status.set(f"已暂停采样 | 缓存占用 {data.get('used', '?')}% | 正在保存与确认数据")
            elif reason == "CONTROL":
                self.status.set("已暂停采样，等待恢复")
            else:
                self.status.set("连接中断，已暂停采样 | 等待主机重连后继续")
        elif event_type == "topology_resumed":
            self.status.set("连接正常，正在继续扫描与保存数据")
        elif event_type == "topology_recovering":
            self.stop_button.state(["disabled"])
            self.status.set("连接中断，正在等待主机重连并确认矩阵复位")
        elif event_type in {"topology_complete", "topology_stopped", "topology_error"}:
            for row in data.get("rows", []):
                self._rows[str(row["source"])] = dict(row)
            self._refresh_rows()
            self._recheck_parameters = data.get("recheck_parameters")
            self._pending_recheck_count = int(data.get("pending_recheck_count", 0))
            self._set_busy(False)
            self._reports = {extension: str(data[extension]) for extension in ("json", "csv") if data.get(extension)}
            for extension in self._reports:
                getattr(self, f"{extension}_button").state(["!disabled"])
            label = {"topology_complete": "扫描完成", "topology_stopped": "已停止", "topology_error": "扫描失败"}[event_type]
            summary = f"{label} | 已扫描 {data.get('completed', 0)}/{data.get('total', 0)} 路 | 实测 {data.get('measurements', 0)} 次"
            if data.get("recheck"):
                label = {"topology_complete": "补测完成", "topology_stopped": "补测已停止", "topology_error": "补测失败"}[event_type]
                summary = f"{label} | 已补测 {data.get('completed', 0)}/{data.get('total', 0)} 对 | 实测 {data.get('measurements', 0)} 次"
            if self._pending_recheck_count:
                summary += f" | 待补测 {self._pending_recheck_count} 对"
            if data.get("connection_error"):
                summary += " | 设备连接中断，连接恢复后请重新扫描"
            elif data.get("error"):
                summary += f" | {data['error']}"
            if data.get("cleanup_errors"):
                summary += " | 矩阵复位未确认"
            self.status.set(summary)
            self.progress.configure(maximum=max(int(data.get("total", 1)), 1), value=int(data.get("completed", 0)))
            self.report_status.set("报告：" + (self._reports.get("json") or "未生成"))

    def _render_row(self, source: str) -> None:
        """Update one source instead of rebuilding an expanding square matrix."""
        row = self._rows[source]
        status, flags = str(row.get("status", "UNKNOWN")), list(row.get("flags", []))
        readings = row.get("connection_resistances", [])
        pending = bool(row.get("targets")) and (not readings or any(item.get("reason") == "NOT_MEASURED" for item in readings))
        normal = status == "UNIQUE" and not flags and not pending
        if self.anomalies_only.get() and normal:
            if self.table.exists(source):
                self.table.delete(source)
                self._show_row()
            return
        label = STATUS_LABELS.get(status, status)
        if pending:
            label += " / 电阻未测"
        if flags:
            label += " / " + ",".join(FLAG_LABELS.get(str(flag), str(flag)) for flag in flags)
        tag = "normal" if normal else "unknown" if pending or status in {"UNKNOWN", "SHORT_CANDIDATES"} else "fault"
        candidates = row.get("candidates", []) if status not in {"UNIQUE", "SHORT", "NO_CONTINUITY"} else []
        resistance = "; ".join(f"{item['target']}: {format_point_resistance(item)}" for item in readings) or ("未测" if pending else "-")
        signature = "二分" if row.get("scan_method") == "binary" else row.get("signature") or "?"
        values = (source, label, ", ".join(row.get("targets", [])) or "-", resistance, signature, ", ".join(candidates) or "-")
        if self.table.exists(source):
            self.table.item(source, values=values, tags=(tag,))
        else:
            self.table.insert("", "end", iid=source, values=values, tags=(tag,))
        if self.table.selection() == (source,):
            self._show_row()

    def _refresh_rows(self) -> None:
        """Apply anomaly filtering across the bounded list of source ports."""
        selected = self.table.selection()
        children = self.table.get_children()
        if children:
            self.table.delete(*children)
        for source in self._rows:
            self._render_row(source)
        if selected and self.table.exists(selected[0]):
            self.table.selection_set(selected[0])
        self._show_row()

    def _show_row(self, _event: object = None) -> None:
        """Show complete endpoint lists when a cell is narrower than a complex fault."""
        selected = self.table.selection()
        text = ""
        if selected:
            row = self._rows[selected[0]]
            lines = [
                f"{row['source']} | {row.get('status', 'UNKNOWN')} | {', '.join(row.get('flags', []))}",
                f"已确认：{', '.join(row.get('targets', [])) or '-'}",
                "单点电阻（未校准）：",
            ]
            readings = row.get("connection_resistances", [])
            for item in readings:
                suffix = "" if item.get("bit") == 1 and item["target"] in row.get("targets", []) else " [未确认]"
                lines.append(f"{row['source']} -> {item['target']}: {format_point_resistance(item, with_unit=True)}{suffix}")
                if item.get("raw"):
                    lines.append(f"  {item['raw']}")
            if not readings:
                lines.append("未测" if row.get("targets") else "-")
            lines.append(f"候选 / 补测范围：{', '.join(row.get('candidates', [])) or '-'}")
            if "point_recheck_complete" in row:
                lines.append(f"逐点复核：已采样 {row.get('point_recheck_measured', 0)}/{row.get('point_recheck_total', 0)} 对")
                if row["point_recheck_complete"] and row.get("status") == "INCONSISTENT":
                    lines.append("编码与逐点复核结果不一致")
            if row.get("binary_conflicts"):
                lines.append(f"分组复核异常：{len(row['binary_conflicts'])} 项")
            text = "\n".join(lines)
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    def _export(self, extension: str) -> None:
        """Copy a finalized raw report to the chosen path without recomputing results."""
        source = self._reports.get(extension)
        if not source:
            return
        destination = filedialog.asksaveasfilename(parent=self.window, title=f"导出 {extension.upper()}", defaultextension=f".{extension}", initialfile=Path(source).name, filetypes=[(extension.upper(), f"*.{extension}")])
        if not destination:
            return
        try:
            if Path(source).resolve() != Path(destination).resolve():
                shutil.copyfile(source, destination)
        except OSError as error:
            messagebox.showerror("导出失败", str(error), parent=self.window)
            return
        self.report_status.set(f"已导出：{destination}")
