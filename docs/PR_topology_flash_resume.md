# 拓扑扫描 Flash 缓存与断线续传

日期：2026-09-07。工程仅为 `D:\esp32\cable_tester_rs485`。本目录不是 Git 仓库，本文是本地变更和交接说明。

后续新增的“拓扑编码 / 二分扫描”选择和区间测量协议见 [二分扫描变更](PR_topology_binary_scan.md)。两种方法共用本文的持久确认与 70%/50% 暂停恢复机制；本文下方的 227 项测试及固件大小保留为缓存功能完成时的历史记录。

## 用户确认与实现范围

用户确认：固件先缓存测量数据，上位机核对保存后确认释放；缺失数据恢复网络后补传；缓存占用达到 70% 作为警戒线，暂停采集。实现采用 70% 暂停、50% 以下恢复的滞回规则，避免边传边测时反复启停。

当前实现包括 Master 固件、Flash 分区、上位机接收存储和扫描界面。没有烧录、访问串口、发送实物测量命令或重启正在运行的上位机。GPIO、仪表路由、WiFi 身份和配置、校准、从机固件及历史报告保持原样。

## 数据流程

1. 上位机建立会话，保存任务配置和任务命令，向 master2 下发分组计划，启动 master1 编码扫描。异常补测和接线电阻仍由上位机按初筛结果安排。
2. master1 选择本侧源端，通过上位机路由请求 master2 准备目标通路，等待稳定后读取 XD31H。
3. 读数提交前核对 master2 的通路令牌。右端重连、复位、清除、重新选择或恢复操作使令牌失效，当前点重测，不把通路已经变化的读数计入结果。
4. master1 将原始测量响应、会话、序号、任务号及 CRC32 提交到 Flash。终止记录也进入同一日志。
5. 上位机每批读取至多 16 条，检查序号、会话、任务、内容和 CRC，提交到本地 SQLite 后才累计 ACK。
6. master1 持久化 ACK，再回收已经全部确认的完整数据扇区。部分尾扇区保留，避免每个样本都擦一次 Flash。

SQLite 使用 WAL 和 `synchronous=FULL`，文件位于 `reports/topology/sessions/`。只有连续提交的数据可推进确认位置；完全相同的重复记录只核对，不重复计数。CRC 错误或缺号从连续确认位置补取，三次持续损坏则停止并保留未确认缓存；同一序号返回不同有效内容直接报错。

ACK 表示数据已在上位机本地持久保存，不表示界面已经刷新，也不要求整次扫描的最终 JSON/CSV 已经导出。ACK 回复丢失时可以重复确认相同序号与 CRC。固件 RESET 不删除未确认数据，新会话不能覆盖旧会话尚未确认的记录。

## 容量与暂停

当前主机配置为 2 MiB Flash；没有读取实物芯片容量。自定义分区保持旧地址，新增剩余空间：

| 分区 | 地址 | 大小 |
|---|---:|---:|
| nvs | `0x9000` | 24 KiB |
| phy_init | `0xf000` | 4 KiB |
| factory | `0x10000` | 1 MiB |
| topology | `0x110000` | 960 KiB |

拓扑分区使用两个 4 KiB 元数据扇区，剩余 3808 个固定 256 字节记录槽，每条最多保存 224 个 ASCII 载荷字符。占用以实际使用的记录槽计算，包含已确认但尚未擦除的尾扇区槽位。

- 占用达到 70% 后，在采样安全边界暂停；30% 余量用于当前操作和终止记录，不能继续无界采样。
- 上位机持续取回、保存、确认数据。固件回收后，实际占用低于 50% 才解除缓存暂停。
- 网络或控制连接尚未恢复时，即使缓存降到低水位也不继续测量。
- 70%/50% 的暂停状态保存在元数据中，重启不会丢失高水位滞回状态。

每侧 10 台、240 路正常一一接线约 2640 次测量，另外还需要任务终止记录；异常逐点补测可能达到 60000 次测量，不能依靠一次完整大包驻留 Flash。此次采用边缓存、边确认、边回收的循环日志。

## 断线与重启

| 场景 | 行为 |
|---|---|
| FETCH 丢失部分帧或回复 | 不确认缺失数据，下次从本地连续保存位置补取 |
| 开始任务或 ACK 回复丢失 | 使用原会话及任务号重试，不重复启动同一任务 |
| 任一 Master TCP 断开后重连 | 暂停并保留计划；先恢复右端再恢复左端，继续未完成位置 |
| 缓存达到 70% | 暂停采样，继续上传；低于 50% 且连接恢复后继续 |
| Master MCU 重启 | 恢复已提交缓存，当前物理扫描需要结束并重新开始 |
| 上位机进程重启或发现旧缓存 | 停止旧任务，先另存剩余数据并确认回收，再启动新扫描 |
| 磁盘提交失败或缓存损坏 | 不推进未保存数据的 ACK，停止任务并报告错误 |
| 用户停止扫描 | 请求停止，尽量取回并确认已产生记录，再复位；离线清理等待有时限 |

Master 间命令仍通过上位机 WiFi 路由，当前硬件拓扑不能在完全断网后自主完成双端通路切换。缓存保证已完成数据不会因普通 TCP 断开而丢失；网络恢复之前暂停采集。

MCU 重启不会自动恢复物理测量位置。源端报告 `RECOVERED` 后只取回数据；右端重启丢失 RAM 分组计划时会话校验失败，当前任务停止，未确认缓存留待恢复。RS485 实物失联、仪表错误或真实 Flash 损坏仍可能导致扫描失败，不能通过网络重传修复。

旧会话优先复用对应的 SQLite 文件；找不到时创建 `recovered_<会话>.sqlite3`。另存的 JSON 包含原始记录，旧数据不会混入新扫描行。若更早已确认部分的本地文件丢失，`acknowledged_prefix_without_local_records` 明确标记已无法从 MCU 回收区恢复的前缀。主窗口日志显示恢复文件路径，新报告也在 `recovered_sessions` 中记录它们。

## 协议

`TOPO_INFO` 新增 `reliable=1 cache_ready=1 cache_high=70 cache_low=50`，以及 `cache_session`、`cache_ack`、`cache_next`、`plan_session`。两端均支持时使用可靠模式；任一端是旧固件时仍支持旧协议，并显示兼容模式。若新版固件报告 `cache_ready=0`，上位机明确报 `CACHE_UNAVAILABLE`，不能静默降级。

```text
TOPO_OPEN <session>
TOPO_BEGIN2 <session> <right_modules> <rounds>
TOPO_MASK <session> <round> <module> <mask_hex>
TOPO_SEAL <session>
TOPO_RUN2 <session> <job> <right_id> <left_modules> <rounds> <settle_ms>
TOPO_POINT2 <session> <job> <right_id> <left_modules> <source> <destination> <settle_ms>
TOPO_FETCH <session> <next_wanted_sequence> <limit>
TOPO_ACK <session> <sequence> <crc_hex>
TOPO_RESUME <session>
TOPO_ABORT <session>
TOPO_RESET <session>
```

任务号从 1 递增，相同任务号只接受完全相同的命令重试。FETCH 的每条记录及末尾状态为：

```text
TOPO_DATA <session> <sequence> <job> <crc_hex> <legacy_payload>
OK TOPO_FETCH session=<session> first=<first_unacked> next=<tail_next> ack=<ack> used=<percent> job=<job> state=<state> reason=<reason> high=70 low=50
```

CRC 是 IEEE/zlib CRC32，输入为精确 ASCII `"<session> <sequence> <job> <legacy_payload>"`，数字使用规范十进制。原始载荷仍为 `TOPO_SAMPLE`、`TOPO_POINT_SAMPLE`、`TOPO_DONE`、`TOPO_STOPPED` 或 `TOPO_FAILED`。

状态为 `IDLE`、`RUNNING`、`PAUSED`、`DONE`、`FAILED`、`STOPPED`、`RECOVERED`，原因为 `NONE`、`NETWORK`、`CACHE`、`CONTROL`、`STORAGE`、`TASK`。源端重启后 `TOPO_RESUME` 返回 `REBOOT_REQUIRES_NEW_SCAN`，不启动旧任务。

Master 间使用 `TOPO_PREP2 <session> <round> <token>`、`TOPO_PICK2 <session> <destination> <token>`，回复回显 token；测量后再发送 `TOPO_VALIDATE <session> <token>`。token 最长有效期 25 秒，从第一次准备从机之前计时，短于从机的 30 秒掩码租约。

## 实现与验证

- `master/main/topology_journal.c/.h`：固定槽循环日志、双元数据扇区、提交标记、CRC、持久 ACK、擦除意图及 70%/50% 滞回。
- `master/main/topology_scan.c`：可靠任务、接收端计划、网络暂停、取回与确认协议、测量前后通路校验。
- `master/partitions.csv`、`master/sdkconfig`、`master/main/CMakeLists.txt`：专用分区和构建接入。
- `topology_transfer.py`、`topology_scan.py`：本地提交、校验补传、会话恢复与报告字段。
- `topology_panel.py`、`cable_tester_gui.py`：暂停和恢复状态、持久数据及历史恢复文件路径。

最终完整回归结果：`Ran 227 tests`、`OK`，无跳过。测试使用隔离的临时目录、假外设、真实本地回环 TCP 和隐藏 Tk，不访问实物设备。

- Flash 日志 7 组测试包含 898 个逐字节断电注入点，覆盖记录提交、ACK、元数据换区、擦除与环形回绕；额外检查数据及元数据提交标记损坏不会丢弃未确认记录。写后读回校验另有“驱动返回成功但实际丢写”回归，校验失败后禁止继续写入或回收。
- 固件 C 状态机 21 项及 RS485 协议 C 测试 4 项实际编译运行，涵盖70%暂停、任务重试、掉线前后提交位置、右端通路失效和未确认数据保留。
- 真实本地 TCP 测试在首批 FETCH 仅收到 5 条数据、尚未收到批次状态时断开连接，确认本地提交及设备 ACK 均未前移；重新注册后从序号 1 补传，完成 168 个编码样本和 24 个单点样本，共 217 条唯一缓存记录。每次 ACK 均检查对应 SQLite 数据已从独立连接可见，全程不消费 GUI 队列。
- 隐藏 Tk 在 860×640、Vista 样式下检查 8 个新状态，状态区域宽 828 px，最长恢复消息宽 540 px，无裁切。既有电阻显示和 GUI 回归仍通过。

软件故障注入不等于实际掉电、Flash 寿命或无线稳定性验证。

在工程根目录执行完整回归；指定可用的本机 C 编译器才能运行固件 C 测试：

```powershell
$env:CABLE_HOST_CC = 'D:\python28\music\Dev-Cpp\MinGW64\bin\gcc.exe'
$env:PATH = 'D:\python28\music\Dev-Cpp\MinGW64\bin;' + $env:PATH
python -m unittest discover -s tests
```

最终源码使用现有 ESP-IDF v5.5.4，在 `master/` 执行 `./idf.ps1 build` 已通过。输出 `master/build/esp32s3_cable_tester_master.bin` 大小 `0xc8ed0`，1 MiB 应用分区剩余 `0x37130`（22%）。构建同时生成包含 960 KiB 拓扑缓存的分区表。编译成功不代表已经在两块实物上运行。

## 升级与实机验收

1. 两块 Master 均需升级应用和分区表，不能只写应用 bin。新分区未移动 NVS、PHY 或应用地址，不需要整片擦除。
2. 当前源码和构建身份仍是 `master1`。按现有配置流程单独构建 `master2`，不能把 master1 镜像原样烧到两块板上。从机无需为此次改动升级。
3. 结束当前操作后重启 `_rs485` 上位机。确认两端支持缓存模式，再进行已知接线扫描。
4. 实机验收分别覆盖左端掉线、右端掉线、重复重连、70% 暂停及低于 50% 恢复、停止期间掉线、MCU 重启后旧缓存另存，以及最终电阻与接线报告一致性。

本次没有执行以上烧录或实机步骤。此前的电阻显示与旧版断线诊断分别保留在 `PR_topology_connection_resistance.md`、`PR_topology_disconnect_recovery.md`，不要用旧文档的“仅重启上位机”代替此次分区升级要求。
