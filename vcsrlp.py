#!/usr/bin/env python3
"""
VictronConnect Log Reader
=========================
Parses the binary .log files produced by VictronConnect (iOS/Android).

File format
-----------
  Bytes 0-3  : uint32 big-endian (MSB-first) length, in BYTES, of the
               decompressed payload (used to verify the file extracted intact)
  Bytes 4-end: zlib-compressed UTF-8 text

The decompressed text contains named sections, each opened by a banner:
  === SectionNameV01 =====...=====

Sections
--------
  LogHeaderV01   - build / device metadata (key: value lines)
  LogLinesV01    - structured log lines (LEVEL TIMESTAMP [MODULE] message)
  EventLogV01    - CSV event history (one event per line)
  ProductDbV01   - pipe-delimited table of known devices
  NetworkingDbV01- pipe-delimited networking identifiers

Usage
-----
  python3 vcsrlp.py <path/to/VictronConnect_report.log> [options]

Options
-------
  --section    {header,logs,events,products,networking,all}
               Which section(s) to display (default: all)
  --level      {DEBUG,INFO,WARN,ERROR}
               Filter log lines by level (applies to LogLinesV01 section)
  --module     MODULE_NAME
               Filter log lines by module tag, e.g. FM, VRGTLR
  --since      DATETIME  (e.g. 2026-03-29T19:00:00)
               Only show log/event entries at or after this timestamp
  --until      DATETIME
               Only show log/event entries before or at this timestamp
  --search     TEXT
               Case-insensitive substring filter applied to message text
  --output     FILE
               Write output to a file instead of stdout
  --summary    Print a summary of counts instead of full content
  --strict     Fail if the decompressed size does not match the 4-byte
               length header (otherwise a mismatch only warns on stderr)
"""

import argparse
import csv
import io
import re
import sys
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class LogHeader:
    fields: dict = field(default_factory=dict)


@dataclass
class LogLine:
    level: str
    timestamp: datetime
    timestamp_raw: str
    module: Optional[str]
    message: str
    raw: str


@dataclass
class EventEntry:
    timestamp: datetime
    timestamp_raw: str
    app_version: str
    serial: str
    pid: str
    event_type: str
    path: str
    old_value: str
    new_value: str
    raw: str


@dataclass
class Product:
    name: str
    custom_name: str
    pid: str
    serial: str


@dataclass
class NetworkEntry:
    id: str
    key: str
    name: str


@dataclass
class ParsedLog:
    header: LogHeader = field(default_factory=LogHeader)
    log_lines: list = field(default_factory=list)
    events: list = field(default_factory=list)
    products: list = field(default_factory=list)
    network_entries: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Decompression
# ---------------------------------------------------------------------------


def decompress(path: Path, strict: bool = False) -> str:
    """Read and decompress a VictronConnect .log file.

    File layout:
        bytes 0-3 : uint32 big-endian (MSB-first) length of the decompressed
                    payload, in BYTES (not characters).
        bytes 4-  : zlib-compressed UTF-8 text.

    The length prefix lets a streaming reader pre-size its buffer and lets us
    verify the file was extracted intact. If the decompressed byte count does
    not match the header, the file is likely truncated or corrupt: a warning is
    printed to stderr, or a ValueError is raised when ``strict`` is True.
    """
    path = Path(path)  # accept either a str or a Path
    raw = path.read_bytes()
    if len(raw) < 4:
        raise ValueError("File too small to contain a 4-byte length header.")

    expected_len = int.from_bytes(raw[:4], "big")

    try:
        payload = zlib.decompress(raw[4:])
    except zlib.error as exc:
        raise ValueError(f"Failed to decompress log file: {exc}") from exc

    # Validate on BYTES, before decoding — UTF-8 multibyte characters make the
    # decoded character count smaller than the raw byte count.
    if len(payload) != expected_len:
        msg = (
            f"Length mismatch: header declares {expected_len:,} bytes, "
            f"decompressed {len(payload):,}. File may be truncated or corrupt."
        )
        if strict:
            raise ValueError(msg)
        print(f"Warning: {msg}", file=sys.stderr)

    return payload.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Section splitting
# ---------------------------------------------------------------------------

SECTION_RE = re.compile(r"^=== (\w+) =+\s*$", re.MULTILINE)


def split_sections(text: str) -> dict:
    """Split decompressed text into named sections."""
    sections = {}
    matches = list(SECTION_RE.finditer(text))
    for i, match in enumerate(matches):
        name = match.group(1)
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[name] = text[start:end].strip()
    return sections


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------


def parse_header(text: str) -> LogHeader:
    header = LogHeader()
    for line in text.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            header.fields[key.strip()] = val.strip()
    return header


LOG_LINE_RE = re.compile(
    r"^(DEBUG|WARN |INFO |ERROR)\s"
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+)\s"
    r"(?:\[([^\]]+)\]\s)?"
    r"(.*)"
)


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp into a naive datetime.

    Event timestamps carry a tz offset (e.g. -04:00) while log lines do not.
    We drop any offset so all timestamps stay naive and remain comparable with
    the (naive) --since/--until filters.
    """
    try:
        return datetime.fromisoformat(ts).replace(tzinfo=None)
    except ValueError:
        return datetime.min


def parse_log_lines(text: str) -> list:
    lines = []
    for raw in text.splitlines():
        m = LOG_LINE_RE.match(raw)
        if m:
            level, ts_raw, module, message = m.groups()
            lines.append(
                LogLine(
                    level=level.strip(),
                    timestamp=_parse_ts(ts_raw),
                    timestamp_raw=ts_raw,
                    module=module,
                    message=message,
                    raw=raw,
                )
            )
    return lines


def parse_events(text: str) -> list:
    events = []
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        # Pad short rows
        while len(row) < 8:
            row.append("")
        ts_raw = row[0]
        events.append(
            EventEntry(
                timestamp=_parse_ts(ts_raw),
                timestamp_raw=ts_raw,
                app_version=row[1],
                serial=row[2],
                pid=row[3],
                event_type=row[4],
                path=row[5] if len(row) > 5 else "",
                old_value=row[6] if len(row) > 6 else "",
                new_value=row[7] if len(row) > 7 else "",
                raw=",".join(row),
            )
        )
    return events


def _parse_pipe_table(text: str) -> list:
    """Parse a pipe-separated table, skipping header and divider rows."""
    rows = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("-"):
            continue
        parts = [p.strip() for p in line.split("|")]
        rows.append(parts)
    return rows


def parse_products(text: str) -> list:
    rows = _parse_pipe_table(text)
    products = []
    for i, row in enumerate(rows):
        if i == 0:  # header row
            continue
        if len(row) >= 4:
            products.append(
                Product(name=row[0], custom_name=row[1], pid=row[2], serial=row[3])
            )
    return products


def parse_networking(text: str) -> list:
    rows = _parse_pipe_table(text)
    entries = []
    for i, row in enumerate(rows):
        if i == 0:
            continue
        if len(row) >= 3:
            entries.append(NetworkEntry(id=row[0], key=row[1], name=row[2]))
    return entries


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------


def parse_log(path: Path, strict: bool = False) -> ParsedLog:
    text = decompress(path, strict=strict)
    sections = split_sections(text)
    result = ParsedLog()

    if "LogHeaderV01" in sections:
        result.header = parse_header(sections["LogHeaderV01"])
    if "LogLinesV01" in sections:
        result.log_lines = parse_log_lines(sections["LogLinesV01"])
    if "EventLogV01" in sections:
        result.events = parse_events(sections["EventLogV01"])
    if "ProductDbV01" in sections:
        result.products = parse_products(sections["ProductDbV01"])
    if "NetworkingDbV01" in sections:
        result.network_entries = parse_networking(sections["NetworkingDbV01"])

    return result


# ---------------------------------------------------------------------------
# Filtering helpers
# ---------------------------------------------------------------------------


def _dt_arg(s: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Cannot parse datetime '{s}'. Use YYYY-MM-DDTHH:MM:SS or YYYY-MM-DD."
    )


def filter_logs(lines, level=None, module=None, since=None, until=None, search=None):
    for ln in lines:
        if level and ln.level != level:
            continue
        if module and (ln.module is None or module.upper() not in ln.module.upper()):
            continue
        if since and ln.timestamp < since:
            continue
        if until and ln.timestamp > until:
            continue
        if search and search.lower() not in ln.raw.lower():
            continue
        yield ln


def filter_events(events, since=None, until=None, search=None):
    for ev in events:
        if since and ev.timestamp < since:
            continue
        if until and ev.timestamp > until:
            continue
        if search and search.lower() not in ev.raw.lower():
            continue
        yield ev


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

LEVEL_COLORS = {
    "DEBUG": "\033[90m",  # dark grey
    "INFO": "\033[36m",  # cyan
    "WARN": "\033[33m",  # yellow
    "ERROR": "\033[31m",  # red
}
RESET = "\033[0m"


def colorize(level: str, text: str, use_color: bool) -> str:
    if not use_color:
        return text
    return f"{LEVEL_COLORS.get(level, '')}{text}{RESET}"


def print_header(header: LogHeader, out):
    out.write("\n─── App / Device Info ────────────────────────────────\n")
    for k, v in header.fields.items():
        out.write(f"  {k:<28} {v}\n")


def print_log_lines(lines, out, use_color=True):
    out.write(f"\n─── Log Lines ({len(lines)} entries) ─────────────────────────\n")
    for ln in lines:
        module_tag = f"[{ln.module}] " if ln.module else ""
        line = f"{ln.level:<5}  {ln.timestamp_raw}  {module_tag}{ln.message}"
        out.write(colorize(ln.level, line, use_color) + "\n")


def print_events(events, out):
    out.write(f"\n─── Event Log ({len(events)} entries) ───────────────────────\n")
    for ev in events:
        device = f"{ev.serial} ({ev.pid})" if ev.serial else ev.pid or "–"
        out.write(
            f"  {ev.timestamp_raw}  "
            f"v={ev.app_version:<6}  "
            f"device={device:<22}  "
            f"event={ev.event_type}\n"
        )
        if ev.path:
            out.write(f"    path: {ev.path}\n")
        if ev.old_value or ev.new_value:
            out.write(f"    {ev.old_value!r} → {ev.new_value!r}\n")


def print_products(products, out):
    out.write(f"\n─── Known Products ({len(products)}) ─────────────────────────\n")
    out.write(f"  {'Product Name':<40} {'Custom Name':<30} {'PID':<8} Serial\n")
    out.write("  " + "─" * 90 + "\n")
    for p in products:
        out.write(f"  {p.name:<40} {p.custom_name:<30} {p.pid:<8} {p.serial}\n")


def print_networking(entries, out):
    out.write(f"\n─── Networking ({len(entries)}) ──────────────────────────────\n")
    out.write(f"  {'ID':<6} {'Key':<34} Name\n")
    out.write("  " + "─" * 60 + "\n")
    for e in entries:
        out.write(f"  {e.id:<6} {e.key:<34} {e.name}\n")


def print_summary(log: ParsedLog, out):
    out.write("\n═══ VictronConnect Log Summary ══════════════════════\n")
    if log.header.fields:
        out.write(
            f"  App version   : {log.header.fields.get('VictronConnect version', '?')}\n"
        )
        out.write(
            f"  Device        : {log.header.fields.get('Product name', '?')} "
            f"{log.header.fields.get('Version', '?')}\n"
        )

    levels = {}
    for ln in log.log_lines:
        levels[ln.level] = levels.get(ln.level, 0) + 1
    out.write(
        f"  Log lines     : {len(log.log_lines):,}  "
        f"({', '.join(f'{k}={v}' for k,v in sorted(levels.items()))})\n"
    )
    out.write(f"  Events        : {len(log.events):,}\n")
    out.write(f"  Known products: {len(log.products)}\n")

    valid_ts = [e.timestamp for e in log.events if e.timestamp != datetime.min]
    if valid_ts:
        out.write(f"  Event span    : {min(valid_ts).date()} → {max(valid_ts).date()}\n")

    if log.products:
        out.write("  Products:\n")
        for p in log.products:
            out.write(f"    • {p.name} – {p.custom_name} (serial {p.serial})\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Read and filter VictronConnect .log files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("logfile", type=Path, help="Path to the .log file")
    p.add_argument(
        "--section",
        "-s",
        choices=["header", "logs", "events", "products", "networking", "all"],
        default="all",
    )
    p.add_argument(
        "--level",
        "-l",
        choices=["DEBUG", "INFO", "WARN", "ERROR"],
        help="Filter log lines by level",
    )
    p.add_argument("--module", "-m", help="Filter log lines by module tag (e.g. FM)")
    p.add_argument(
        "--since",
        type=_dt_arg,
        metavar="DATETIME",
        help="Start timestamp (YYYY-MM-DDTHH:MM:SS)",
    )
    p.add_argument(
        "--until",
        type=_dt_arg,
        metavar="DATETIME",
        help="End timestamp (YYYY-MM-DDTHH:MM:SS)",
    )
    p.add_argument("--search", help="Case-insensitive text search")
    p.add_argument("--output", "-o", type=Path, help="Write output to file")
    p.add_argument(
        "--summary", action="store_true", help="Print summary statistics only"
    )
    p.add_argument("--no-color", action="store_true", help="Disable ANSI color output")
    p.add_argument(
        "--strict",
        action="store_true",
        help="Fail if the decompressed size does not match the 4-byte length header",
    )
    return p


def main():
    args = build_parser().parse_args()

    if not args.logfile.exists():
        print(f"Error: file not found: {args.logfile}", file=sys.stderr)
        sys.exit(1)

    print(f"Reading {args.logfile} …", file=sys.stderr)
    try:
        log = parse_log(args.logfile, strict=args.strict)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    use_color = not args.no_color and sys.stdout.isatty()

    out = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout

    try:
        if args.summary:
            print_summary(log, out)
            return

        sec = args.section

        if sec in ("header", "all"):
            print_header(log.header, out)

        if sec in ("logs", "all"):
            filtered = list(
                filter_logs(
                    log.log_lines,
                    level=args.level,
                    module=args.module,
                    since=args.since,
                    until=args.until,
                    search=args.search,
                )
            )
            print_log_lines(filtered, out, use_color=use_color)

        if sec in ("events", "all"):
            filtered_ev = list(
                filter_events(
                    log.events,
                    since=args.since,
                    until=args.until,
                    search=args.search,
                )
            )
            print_events(filtered_ev, out)

        if sec in ("products", "all"):
            print_products(log.products, out)

        if sec in ("networking", "all"):
            print_networking(log.network_entries, out)

    finally:
        if args.output:
            out.close()
            print(f"Output written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
