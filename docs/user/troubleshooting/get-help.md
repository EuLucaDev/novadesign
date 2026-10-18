---
title: Report a problem
description: What to include and where to post when Nova Design misbehaves or crashes.
order: 20
verified: 0.7.1
---

Post in the [issue tracker](https://github.com/EuLucaDev/novadesign/issues) or in [Suporte](https://workflowind.agcunae.com/inbox), and attach the log file. The log is the most useful thing you can send after a crash.

## What to include

- Your Nova Design version and how you installed it (installer, portable, AppImage and so on).
- Your operating system and graphics card.
- What you did, what you expected and what happened.
- The log file `novadesign.log`.

## Where the log is

Nova Design writes `logs/novadesign.log` in its settings folder, beside `ui.json` and `prefs.json`.

| System  | Log folder                                                              |
|---------|-------------------------------------------------------------------------|
| Windows | `%APPDATA%\Nova Design\logs\`                                           |
| macOS   | `~/Library/Application Support/Nova Design/logs/`                       |
| Linux   | `~/.config/novadesign/logs/` (or `$XDG_CONFIG_HOME/novadesign/logs/`) |

Each start moves the previous log to `novadesign.1.log`, and that one to `novadesign.2.log`. The log from a run that crashed survives the next start, so attach `novadesign.1.log` if you have restarted since. The file stops growing at 16 MiB.

## More detail in the log

Set the environment variable `RUST_LOG` to `debug` before starting Nova Design to log more. The default is `info` for Nova Design's own parts and `warn` for everything else.
