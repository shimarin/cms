#!/usr/bin/env python3
"""Dump or follow the SQLite analytics log."""

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone


def _local_time(iso_ts: str) -> str:
    """Convert ISO8601 UTC timestamp to local time in human-friendly format."""
    try:
        dt = datetime.fromisoformat(iso_ts)
        local_dt = dt.astimezone()
        return local_dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return iso_ts or "-"


def format_row(row: dict) -> str:
    ts = _local_time(row["ts"])
    elapsed = f'{row["elapsed_ms"]:.1f}ms' if row["elapsed_ms"] is not None else "-"
    status = row["status"]
    method = row["method"] or "-"
    path = row["path"] or "/"
    query = f'?{row["query"]}' if row["query"] else ""
    sf = "/".join(
        row[k] or "-"
        for k in ("sec_fetch_site", "sec_fetch_mode", "sec_fetch_dest", "sec_fetch_user")
    )
    lang = row["accept_language"] or "-"
    ua = row["user_agent"] or "-"
    ip = row["remote_ip"] or "-"
    referer = row["referer"] or "-"
    return (
        f"{ts} {elapsed} {status} {method} {path}{query} "
        f"[{sf}] {ip} \"{lang}\" \"{referer}\" \"{ua}\""
    )


def build_query(args) -> tuple[str, list]:
    conditions = []
    params = []
    if args.since:
        conditions.append("ts >= ?")
        params.append(args.since)
    if args.until:
        conditions.append("ts <= ?")
        params.append(args.until)
    if args.path:
        conditions.append("path LIKE ?")
        params.append(args.path)
    if args.status:
        conditions.append("status = ?")
        params.append(args.status)
    if args.sec_fetch_site:
        conditions.append("sec_fetch_site = ?")
        params.append(args.sec_fetch_site)
    if args.sec_fetch_mode:
        conditions.append("sec_fetch_mode = ?")
        params.append(args.sec_fetch_mode)
    if args.sec_fetch_dest:
        conditions.append("sec_fetch_dest = ?")
        params.append(args.sec_fetch_dest)
    if args.sec_fetch_user:
        conditions.append("sec_fetch_user = ?")
        params.append(args.sec_fetch_user)
    if args.lang:
        # Match language prefix: "ja" matches "ja", "ja-JP", "ja,en", etc.
        conditions.append(
            "(accept_language LIKE ? OR accept_language LIKE ? OR accept_language LIKE ?)"
        )
        lang = args.lang
        params.append(f"{lang}%")        # starts with lang
        params.append(f"%, {lang}%")     # after comma+space
        params.append(f"%,{lang}%")      # after comma (no space)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    limit = f" LIMIT {args.limit}" if args.limit else ""
    return f"SELECT * FROM access{where} ORDER BY id ASC{limit}", params


def dump(conn: sqlite3.Connection, args) -> None:
    query, params = build_query(args)
    cur = conn.execute(query, params)
    for row in cur:
        print(format_row(row))


def follow(conn: sqlite3.Connection, args) -> None:
    row = conn.execute("SELECT MAX(id) FROM access").fetchone()
    last_id = row[0] if row[0] is not None else 0

    def _build_follow_query(last_id):
        conditions = ["id > ?"]
        params = [last_id]
        if args.since:
            conditions.append("ts >= ?")
            params.append(args.since)
        if args.until:
            conditions.append("ts <= ?")
            params.append(args.until)
        if args.path:
            conditions.append("path LIKE ?")
            params.append(args.path)
        if args.status:
            conditions.append("status = ?")
            params.append(args.status)
        if args.sec_fetch_site:
            conditions.append("sec_fetch_site = ?")
            params.append(args.sec_fetch_site)
        if args.sec_fetch_mode:
            conditions.append("sec_fetch_mode = ?")
            params.append(args.sec_fetch_mode)
        if args.sec_fetch_dest:
            conditions.append("sec_fetch_dest = ?")
            params.append(args.sec_fetch_dest)
        if args.sec_fetch_user:
            conditions.append("sec_fetch_user = ?")
            params.append(args.sec_fetch_user)
        if args.lang:
            conditions.append(
                "(accept_language LIKE ? OR accept_language LIKE ? OR accept_language LIKE ?)"
            )
            lang = args.lang
            params.append(f"{lang}%")
            params.append(f"%, {lang}%")
            params.append(f"%,{lang}%")
        where = " WHERE " + " AND ".join(conditions)
        return f"SELECT * FROM access{where} ORDER BY id ASC", params

    idle_polls = 0
    idle_threshold = 60  # close/reopen after 30s of no new records (60 * 0.5s)

    try:
        while True:
            query, params = _build_follow_query(last_id)
            cur = conn.execute(query, params)
            got_rows = False
            for row in cur:
                print(format_row(row), flush=True)
                last_id = row["id"]
                got_rows = True
            if got_rows:
                idle_polls = 0
            else:
                idle_polls += 1
                if idle_polls >= idle_threshold:
                    # Release read lock to allow WAL checkpoint
                    conn.close()
                    conn = sqlite3.connect(f"file:{args.db_path}?mode=ro", uri=True)
                    conn.row_factory = sqlite3.Row
                    idle_polls = 0
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Dump or follow the SQLite analytics log")
    parser.add_argument("db_path", metavar="DB_PATH", help="Path to access_log.sqlite")
    parser.add_argument("-f", "--follow", action="store_true", help="Follow mode (like tail -f)")
    parser.add_argument("--since", metavar="TIMESTAMP", help="Show records from this time (ISO8601)")
    parser.add_argument("--until", metavar="TIMESTAMP", help="Show records until this time (ISO8601)")
    parser.add_argument("--path", metavar="PATTERN", help="Filter by path (SQL LIKE pattern)")
    parser.add_argument("--status", type=int, metavar="CODE", help="Filter by HTTP status code")
    parser.add_argument("--sec-fetch-site", metavar="VAL", help="Filter by Sec-Fetch-Site (none, same-origin, same-site, cross-site)")
    parser.add_argument("--sec-fetch-mode", metavar="VAL", help="Filter by Sec-Fetch-Mode (navigate, cors, same-origin, no-cors)")
    parser.add_argument("--sec-fetch-dest", metavar="VAL", help="Filter by Sec-Fetch-Dest (document, empty, image, script, ...)")
    parser.add_argument("--sec-fetch-user", metavar="VAL", help="Filter by Sec-Fetch-User (?1)")
    parser.add_argument("--lang", metavar="CODE", help="Filter by Accept-Language prefix (e.g. 'ja' matches ja, ja-JP)")
    parser.add_argument("--limit", type=int, metavar="N", help="Limit number of records")
    args = parser.parse_args()

    try:
        conn = sqlite3.connect(f"file:{args.db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    conn.row_factory = sqlite3.Row

    if args.follow:
        follow(conn, args)
    else:
        dump(conn, args)

    conn.close()


if __name__ == "__main__":
    main()
