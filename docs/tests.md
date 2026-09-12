# 测试与 C 编译工具链

本工程的上位机测试由 Python `unittest` 驱动；其中 4 个模块会**真正编译固件 C 源码**，因此需要一台可用的本机 C 编译器。本文说明如何运行，以及缺少编译器时会发生什么。

## 运行

在工程根目录执行：

```powershell
cd D:\esp32\cable_tester_rs485
python -m unittest discover -s tests
```

## 为什么需要 C 编译器

固件测试不是用 Python 重写一遍逻辑，而是让 harness 直接 `#include` 生产的 `.c` 文件（`tests/firmware/*.c`，见 `tests/firmware/topology_scan_test.c:9`），再用 `ctypes` 调用编出来的动态库。因此它们覆盖的是**真实固件代码**，也是 Flash 日志、掩码租约和拓扑状态机唯一的覆盖来源：

| 模块 | 测试数 | 编译的生产代码 |
|---|---:|---|
| `tests/test_topology_firmware.py` | 29 | `master/main/topology_scan.c`、`ch446_master.c` |
| `tests/test_topology_journal.py` | 7 | `master/main/topology_journal.c`（逐字节掉电注入） |
| `tests/test_rs485_topology_protocol.py` | 4 | `shared/rs485_topology_protocol.h` |
| `tests/test_slave_identity.py` | 2 | `slave/main/board_config.h`、`wifi_server.h` 宏展开 |

## 指定编译器

`tests/c_toolchain.py` 按以下顺序探测，第一个**真正编译并链接成功**的即为所选编译器：

1. 环境变量 `CABLE_HOST_CC`（其次 `CABLE_HOST_CLANG`）
2. `PATH` 上的 `cc`、`gcc`、`clang`
3. `tests/.tools/ziglang/` 下自带的 Zig（当前仓库未附带）
4. `slave/build/compile_commands.json` 中记录的 IDF 编译器

本机可用的写法：

```powershell
$env:CABLE_HOST_CC = 'D:\python28\music\Dev-Cpp\MinGW64\bin\gcc.exe'
$env:PATH = 'D:\python28\music\Dev-Cpp\MinGW64\bin;' + $env:PATH
python -m unittest discover -s tests
```

## 缺少编译器时会失败，不再跳过

这 4 个模块过去在找不到编译器时调用 `unittest.SkipTest`。跳过在汇总行里和通过无法区分：`setUpClass` 跳过只让汇总行多一个 `skipped`，上面这 42 个用例**一个都不会出现在 `Ran N tests` 计数里**，等于静默丢掉全套里最有价值的断言。文档中 `PR_topology_connection_resistance.md`、`PR_topology_disconnect_recovery.md` 记录的 `OK (skipped=2)` 就是这个状态。现在改为 `RuntimeError`，汇总行显示 `FAILED (errors=1)`。

探测不只检查“能否回答 `-v`”，还要求编译器**为本机 64 位目标编译并链接成功**。这条附加条件很必要：ESP-IDF 的 `xtensa-esp32s3-elf-gcc.exe` 能回答 `-v`，也能对着自己的 esp32s3 libc 链接一个平凡 `main`，但它无法构建本机测试库；仅检查 `-v` 或仅链接会误判为可用。

## 实机与构建

软件测试全部使用隔离的临时目录、假外设、本地回环 TCP 和隐藏 Tk，**不访问实物设备**。固件构建与烧录见 [双端线缆拓扑扫描](topology_scan.md) 的“构建与验证”一节；软件测试通过不等于实机验证通过。
