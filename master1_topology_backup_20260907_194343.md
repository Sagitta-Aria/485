# master1 topology cache inspection

Date: 2026-09-07, Asia/Shanghai.

Device was identified through USB Serial/JTAG COM13. The chip MAC reported by
esptool was `e0:72:a1:d1:4e:34`, matching the user's master1 boot log.

## Backup

- Flash start: `0x110000`.
- Length: `0xF0000` (983040 bytes).
- Initial read: `master1_topology_backup_20260907_194343.bin`.
- Verification read: `master1_topology_verify_20260907_194343.bin`.
- Both files are byte-for-byte identical.
- SHA256: `28b2ff95b65e4996299dc5e153087dfc508b6fee7e1f5d70032e8b599e394735`.

Both reads used esptool v4.12.dev1, read_flash, and --after no_reset.
During the initial inspection, no flash erase, firmware programming, calibration
change, or scan was performed. The subsequently approved recovery is recorded below.

## Findings

- All 128 metadata slots are nonblank, but none has a committed valid journal
  header. There are no metadata/data journal magic signatures anywhere in the
  partition dump.
- 799 data slots are nonblank. None is a valid committed topology record.
- 52 consecutive sectors contain nonblank data, starting at partition offset 0.
  The final non-0xFF byte is at partition offset `0x33E9F`.
- A 64-byte block at dump offset `0x10000` exactly matches the master firmware
  image at file offset `0x9DB0C`, supporting residual program data in the newly
  assigned cache region. The exact older image/version was not identified.
- In topology_journal_init(), no valid metadata is found. The first data slot
  at partition offset `0x2000` (absolute flash address `0x112000`) is nonblank.
  The `if (!found)` check therefore returns `ESP_ERR_INVALID_CRC` before any
  initialization write or erase. This matches the device's reported error.

## Diagnostic Limits

The failure is explained by non-journal contents in the cache region. These
reads do not indicate a capacity overrun or inconsistent readback, and do not
constitute a general flash hardware qualification.

The absence of valid records means no committed topology records were recognized
under the current format; it is not proof that all historical bytes are worthless.

## Approved Recovery And Verification

After the user approved recovery, only master1's backed-up topology partition was
erased using esptool v4.12.dev1 on COM13:

```text
--chip esp32s3 --port COM13 --after no_reset erase_region 0x110000 0xF0000
```

- The chip MAC was reconfirmed as `e0:72:a1:d1:4e:34`.
- Erase completed successfully.
- The entire partition was read back to
  `master1_topology_erased_after_backup_20260907_194343.bin` before restarting.
- Readback length: 983040 bytes. Non-0xFF bytes: 0.
- Erased readback SHA256:
  `8e6f77366baf8f9b9cddc873765484b539c202d890ef8291302eae35e63b37ce`.
- The existing application was restarted using `--after hard_reset read_mac`.
- A temporary NODE connection to the existing router at `127.0.0.1:3333` queried
  master1 using the read-only `TOPO_INFO` command. The reply was:

```text
OK TOPO_INFO role=MASTER capacity=10 configured=7 bus=1 route=1 reliable=1 cache_ready=1 cache_high=70 cache_low=50 cache_session=0 cache_ack=0 cache_next=1 plan_session=0 binary=1 cache_error=ESP_OK
```

This verifies successful cache initialization after removing the invalid region
contents. The two original backups remain intact. No firmware was programmed,
and NVS, PHY, the bootloader, application, partition table, and calibration were
not changed. Master2 was not repaired. Serial access and the temporary router
connection were released after verification.

This recovery verifies initialization only; no physical topology scan or network
interruption/resume test was performed.
