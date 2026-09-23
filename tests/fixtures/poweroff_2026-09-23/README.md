Real data from omarchy, captured 2026-09-23 before the 24 h raw retention
deleted it. The battery ran out at 09:37:02 PDT (hard power-off, no clean
shutdown); the machine booted again at 15:09:32 PDT.

- raw_rows.json — hwmon `raw` rows 09:26:36 → ~15:10 PDT: ts (unix UTC),
  battery_pct, battery_power_w, status. The gap between 09:37:02 and the
  first post-boot row is the event.
- boots.json — `journalctl --list-boots -o json` at capture time.
- boot0_evidence.json — the boot-0 journal lines proving the unclean end:
  systemd-fsck "Dirty bit is set", systemd-journald "uncleanly shut down".

Positive control for spec v3 acceptance 13: classifying this gap must yield
`hard_poweroff`, last_pct 2, ts_start 09:37:02 PDT, with the fsck line as
detail.
