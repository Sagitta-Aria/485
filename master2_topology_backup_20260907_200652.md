# master2 topology cache recovery

Date: 2026-09-07, Asia/Shanghai.

## Device And Scope

- Device: master2, ESP32-S3, MAC `58:e6:c5:6b:07:a4`.
- Download port: USB Serial/JTAG COM20. Console bridge: COM22.
- Tool: esptool v4.12.dev1 from the ESP-IDF v5.5.4 Python environment.
- The user explicitly authorized the same backup and partition-only recovery
  previously performed on master1.
- Master1 and the GUI process were not changed during this recovery.

## Before Recovery

The existing router returned this read-only master2 response:

```text
OK TOPO_INFO role=MASTER capacity=10 configured=7 bus=1 route=0 reliable=1 cache_ready=0 cache_high=70 cache_low=50 cache_session=0 cache_ack=0 cache_next=0 plan_session=0
```

The actual partition table was read from flash address `0x8000`, length `0x1000`,
to `master2_partition_table_20260907_200652.bin`. Its MD5 checksum passed.
The topology entry was confirmed as data subtype `0x40`, flags 0, start
`0x110000`, length `0xF0000` (983040 bytes), ending at `0x200000`.
No other partition overlaps this range.

Two complete reads were captured with the device remaining in download mode:

- `master2_topology_backup_20260907_200652.bin`
- `master2_topology_verify_20260907_200652.bin`

Both are 983040 bytes and byte-for-byte identical. Their SHA256 is:

```text
83104dccca32d9b7407d60735dc06c22517569922b9a1b4693bba73fbe5aa6ea
```

The partition contained 209725 non-0xFF bytes across 52 sectors, with the last
non-0xFF byte at partition offset `0x33E9F`. All 128 metadata slots and 799 data
slots were nonblank. Neither metadata nor data journal magic appeared anywhere
in the dump, so no valid records under the current journal format were present.
The first data slot at partition offset `0x2000` was also nonblank. This matches
the invalid-region pattern previously found on master1. The precise origin of
the residual contents on master2 was not established.

## Recovery And Verification

Only the backed-up topology partition was erased:

```text
--chip esp32s3 --port COM20 --after no_reset erase_region 0x110000 0xF0000
```

Esptool reconfirmed the chip MAC and reported successful erase in 0.5 seconds.
The entire partition was then read to:

```text
master2_topology_erased_after_backup_20260907_200652.bin
```

This readback was 983040 bytes, all `0xFF`. Its SHA256 is:

```text
8e6f77366baf8f9b9cddc873765484b539c202d890ef8291302eae35e63b37ce
```

The existing application was restarted with `--after hard_reset read_mac`.
A temporary NODE connection to the existing router at `127.0.0.1:3333` queried
master2 with `TOPO_INFO` and received:

```text
OK TOPO_INFO role=MASTER capacity=10 configured=7 bus=1 route=0 reliable=1 cache_ready=1 cache_high=70 cache_low=50 cache_session=0 cache_ack=0 cache_next=1 plan_session=0
```

The transition from `cache_ready=0` to `cache_ready=1` verifies successful cache
initialization. This device's response did not include a `cache_error` field.
All backup files remain intact. Serial access and the temporary router socket
were released after verification.

No application firmware, partition table, NVS, PHY, bootloader, configuration,
or calibration was programmed or erased. No physical topology scan or network
interruption/resume test was performed.
