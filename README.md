# ESP32-S3 线缆矩阵 WiFi/RS485 工程

本目录是基于 `D:\esp32\cable_tester` 的独立版本，源工程保持不变。

## 目录

- `master/`：主机固件。WiFi 接收上位机命令，通过 UART1（GPIO17/18）驱动本机独立的 RS485 总线；UART2 使用 GPIO40(TX)/GPIO39(RX) 连接 XD31H，9600 8N1。
- `slave/`：从机固件。正常仅监听 UART1 RS485，每台两片 CH446X 合计提供 24 组双触点，不包含低阻测量模块；保留可单独启用的 WiFi 调试代码。
- `cable_tester_gui.py`：主从模式上位机。主机模式可同时接收 `master1`、`master2`，并把矩阵命令发给当前所选主机，由该主机访问自己 RS485 总线上的 slave；从机模式通过 `m1-s1`、`m2-s1` 等 WiFi 名称直连调试，支持双节点闭合，但不支持电阻测量。
- `topology_scan.py`、`topology_binary.py`、`topology_panel.py`、`topology_transfer.py`：双端扫描、二分搜索、独立窗口和已确认数据存储。支持“拓扑编码”“二分扫描”两种方法；每侧可选 1～10 台从机，默认值为 7。使用说明见 [拓扑扫描](docs/topology_scan.md)。

## ID

主机 WiFi 文本 ID：`master1`、`master2`。每块主机需在 `master/main/wifi_server.h` 按实际角色设置 `WIFI_DEVICE_ID` 并单独构建，两台在线主机的 ID 不能相同。ID 还决定本机矩阵的固定仪表路由，不能只作为显示名称修改。

从机 RS485 目标沿用每侧 `slave1`～`slave10`；WiFi 调试名称独立，使用 `m1-s1`～`m1-s10`、`m2-s1`～`m2-s10`。拓扑扫描不依赖从机 WiFi 名称，原有 RS485 外层帧格式不变。

每条独立 RS485 总线的数字 ID：本机 master=`0x01`，`slave1`～`slave10` 依次为 `0x11`～`0x1A`，`0xFF` 为广播地址。拓扑扫描按连续编号使用 `slave1` 到选定数量的从机。

每台主机只连接自己的一条 RS485 总线，两台主机之间不连接 RS485。不同总线可以重复使用同一组 `slave1`～`slave10`；同一条总线上每个从机 ID 仍必须唯一。指定命令由当前总线上的所有从机接收后自行校验 ID。

上位机内部使用 `master1-slave1`、`master2-slave1` 这样的组合键隔离状态、校准和报告；组合键只存在于上位机，不会发给从机。从机在 RS485 上收到的目标仍是原来的 `slave1`。

正常测量不要求从机连接 WiFi。`slave/main/board_config.h` 的 `BOARD_SLAVE_WIFI_DEBUG_ENABLED=1` 启用直连调试，`0` 关闭；当前保留为 `1`，便于两侧同时调试。上位机仍接受旧 `slave1`～`slave10` WiFi 名称，供逐板升级过渡，但两块旧固件不可同名同时注册。

配置每块从机只需在 `slave/main/board_config.h` 设置以下两个整数（使用 `1`、`2` 这样的十进制写法，不加 `U` 后缀），然后单独构建：

| 所属主机 | `BOARD_SLAVE_MASTER_INDEX` | `BOARD_SLAVE_INDEX` | 自动 WiFi ID | 自动 RS485 地址 |
|---|---:|---:|---|---|
| master1 的第 1 台 | 1 | 1 | `m1-s1` | `0x11` |
| master2 的第 1 台 | 2 | 1 | `m2-s1` | `0x11` |
| master2 的第 10 台 | 2 | 10 | `m2-s10` | `0x1A` |

当前从机配置为 `m1-s1` / `0x11`。`BOARD_RS485_NODE_ID` 和 `WIFI_DEVICE_ID` 都自动生成，不再分别手工修改。改变所属主机编号只改变 WiFi 名称，不改变 RS485 地址。

首次使用时，将 `master/main/wifi_config.example.h` 复制为同目录下的 `wifi_config.local.h`，按现场热点填写 `WIFI_STA_SSID` 和 `WIFI_STA_PASSWORD`。从机仅启用 WiFi 调试时需要同样复制并填写 `slave/main/wifi_config.local.h`。本地配置已加入 `.gitignore`，不会上传到仓库；未创建本地配置时使用空的示例值，需要填写后才能连接热点。ESP32 以 STA 模式连接笔记本热点，使用 DHCP 默认网关作为 TCP 服务器地址。

`master1` 本地 S1 始终保留 `X0-Y0`、`X1-Y1`、`X2-Y2`、`X3-Y3` 四条仪表通路；`RESET` 也不会断开它们，只清除其他交叉点。`master2` 上电和 `RESET` 后本地矩阵全断，从机 `RESET` 后两片矩阵全断。两端仅按同名连接 Y0～Y3，四条 Y 总线不能彼此短接。

## RS485 帧

```text
[SOF=0xA5][DST_ID][CMD][LEN][DATA...][CRC16-Modbus]
```

每条一主多从总线的外层帧不传源地址和序号。普通指定命令仍会被本总线上的所有从机接收，只有 ID 匹配的从机执行并回复；`0xFF` 广播命令不回复。上位机先选择 `master1` 或 `master2`，目标列表再提供当前主机、`slave1`～`slave10` 和 `broadcast`。选择当前主机时直接控制该主机本地的 U1/S1 矩阵；选择 `broadcast` 时当前主机只报告帧已发出，不等待从机响应。`STATUS` 查询不使用广播，而是由所选主机单播查询具体从机。

命令字：`0x01 RESET`、`0x02 PING`、`0x03 STATUS`、`0x10 SWITCH`、`0x11 CONNECT`。`SWITCH` 数据为 `[bank][x][y][state]`，其中 `bank=0/1` 对应 S1/S2，`state=0/1` 对应 OFF/ON。`STATUS` 返回每片 CH446X 的 24×5 软件状态位图；它反映固件最近写入的状态，不是 CH446X 硬件读回。

拓扑新增 `0x12 MASK`、`0x13 CAPS`、`0x14 CLEAR`，定义在 `shared/rs485_topology_protocol.h`。MASK 载荷包含协议版本、会话、步号、24 位分组掩码和正负侧标志；应答回显请求以拒绝错轮次回包。从机的拓扑掩码默认有 30 秒租约，只有被接受的 MASK 才续期，普通 PING/STATUS 不续期。

## 构建

使用 ESP-IDF v5.5.4。根目录是主机/从机工程的容器，不是可直接构建的 ESP-IDF 应用；它没有 `main/app_main.c`。VS Code 请打开 `cable_tester_rs485.code-workspace`，或直接打开 `master` / `slave` 子目录。不要在 `D:\esp32\cable_tester_rs485` 根目录执行 `idf.py build`。

```powershell
cd D:\esp32\cable_tester_rs485\master
./idf.ps1 build

cd D:\esp32\cable_tester_rs485\slave
./idf.ps1 build
```

烧录时必须使用项目脚本激活 ESP-IDF 环境，并显式指定设备管理器中的 ESP32 端口；不要使用蓝牙 COM5/6/11/12，也不要直接调用系统环境里的 `idf.py`：

```powershell
cd D:\esp32\cable_tester_rs485\master
./idf.ps1 -p COM16 flash
./idf.ps1 -p COM16 monitor
```

此前记录的 ESP32-S3 原生 USB-JTAG/串口为 `COM13`（设备管理器名称为 `USB 串行设备`，硬件 ID 为 `VID_303A:PID_1001`），当时 `COM14` 是 CH343、`COM17/COM18` 是 XDS110。重新插拔后端口号可能变化，烧录前请重新核对设备管理器，不能直接套用历史端口。烧录前关闭其他串口监视器；若端口存在但握手失败，按住 BOOT，点按 EN，再松开 BOOT 后重试。master 和 slave 应分别在对应目录烧录，不能混用生成的 bin。

默认 RS485 收发方向脚为 GPIO2（`BOARD_RS485_DE_RE_GPIO`），必须与实际收发器硬件一致。主机只控制 GPIO4/5/6/7 对应的 CH446X U1/S1；主机 UART2 使用 GPIO40(TX)/GPIO39(RX)，GPIO41/42未使用。如果硬件没有按此连接，先修改 `board_config.h`。

## 上位机

```powershell
python -m pip install -r requirements.txt
python cable_tester_gui.py
```

笔记本开启热点，监听 `0.0.0.0:3333`。上位机默认隐藏重复的 TCP 路由回执，只保留目标、命令和结果；需要排查链路时可关闭“过滤正常收发”。

主机模式下先在“主机ID”选择 `master1` 或 `master2`，再在“矩阵目标”选择该主机本地矩阵或旧制从机 ID。同一时间只运行一个自动测量任务，但两台主机的 TCP 连接会同时保持在线。

“拓扑扫描”打开双主机扫描窗口：左端选择 `master1`、右端选择 `master2`，分别填写实际从机数。每侧 7 台时是 168 对 168 路，初筛 1680 次；每侧 10 台时是 240 对 240 路，初筛 2400 次。初筛后执行异常补测，并为尚无单点读数的唯一接线各测一次电阻；异常补测读数直接复用。结果表和 JSON/CSV 报告包含每条接线的单点电阻，未测、超量程和失败不记为零。电阻为未校准的 XD31H 端到端读数，不是分组并联读数，也未扣除测试通路影响。追加测量次数另计，默认稳定时间 1 秒，实际耗时还包括仪表和总线通信。完整设置、端口分组、报告和限制见 [拓扑扫描](docs/topology_scan.md)。

拓扑缓存续传需要两块 Master 都升级固件及分区表。测量结果先写入 Master Flash，上位机校验并提交本地 SQLite 后确认，设备只回收已确认记录。缓存占用达到 70% 暂停采样，降到 50% 以下且控制连接恢复后继续；TCP 重连可续传、续扫。设备重启后只恢复已缓存数据，需要新建扫描。上次残留数据另存，不混入新结果。旧 Master 固件仍使用兼容传输，中断后需重新扫描。当前主机构建身份是 `master1`，升级 `master2` 时须按角色单独构建。详细协议、分区与验收见 [Flash 缓存与断线续传](docs/PR_topology_flash_resume.md)。

扫描窗口新增“拓扑编码 / 二分扫描”选择，默认拓扑编码；上面的初筛次数仅适用于编码方式。二分扫描无需预先填写分支数量，对导通区间继续检查两半，单点确认每个分支，并复查未确认端口。“二分采样次数”可选 1～10 次，默认 1 次，作用于每次区间或单点判定；选择多次时要求读数分类一致。复查和异常补测可能再次访问同一区间或端口，所选次数不是整个扫描中该端口的总次数上限。遇到矛盾会逐点补测并保留异常，不直接判为正常。二分方式需要两块 Master 同时支持新版区间命令，沿用 Flash 缓存和 70%/50% 暂停恢复机制；已有二分固件只需重启上位机即可使用采样次数选项。方法差异、准确性边界和升级说明见 [二分扫描变更](docs/PR_topology_binary_scan.md)。

编码扫描结束后可选“补测待确认 (N)”，对异常左端口逐点复核右端全部计划端口，每对采样一次；原正常行保留，结果和单点电阻更新到新的报告中。再次补测只处理仍未确定的端口对。按钮沿用原扫描配置，不重新执行编码，也不隐含三次采样。详情见 [扫描后逐点补测](docs/PR_topology_pending_recheck.md)。此选项仅修改上位机，已有拓扑固件无需为此升级。

## 仅一台主机在线时测试 RS485

1. 烧录并启动 `master1` 或 `master2`，上位机启动服务器后等待在线列表出现对应 ID。在“主机ID”选择它；“矩阵目标”选择同名主机可直接控制主机 U1/S1 矩阵，选择 `broadcast` 可测试 RS485 发射。
2. 选择当前主机或 `broadcast`，点击“复位矩阵”；也可以在自定义指令输入 `RESET` 或 `SWITCH S1 4 Y4 ON`。查询 `STATUS` 时应选择具体设备，不使用广播。master1 的 X0～X3 属于受保护的固定仪表路由，不能用旧的任意选路命令覆盖。控制台旁的“矩阵状态”页可按当前主机范围查看 S1/S2 的 24×5 开闭表。
3. 日志中出现 `OK BUS_SENT broadcast ...` 表示当前主机已经完成 RS485 帧发送并释放 DE/RE，不代表有从机应答。广播帧不回包，适合先用 USB-RS485 或示波器观察 UART1/GPIO17、GPIO18 和 GPIO2 收发方向。

例如向从机发送 `SWITCH S1 0 Y4 ON` 的广播帧（CRC16-Modbus，小端）应为：

```text
A5 FF 10 04 00 00 04 01 B5 13
```
