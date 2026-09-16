#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重置 HENAN 采集任务状态（COMPLETED / FAILED → PENDING）。

【权威源，务必先读】2026-09-16 起 SQLite 已从 HENAN 任务链路彻底摘除

  MySQL   121.41.56.201/evcs.henan_heavy_truck_charging_station_task = ★唯一权威源★
  SQLite  data/mobile_control.db 的 scan_task（HENAN 行）             = 已废弃的导入缓存，
                                                                       本脚本不再读写

  => 只改 MySQL 即可：立即生效，且重启不会回滚（api_server 的 startup 已不再回灌 SQLite）。
  => 非 HENAN 任务（SITE_STATION_DETAIL / REGION_SCAN）和设备表仍以 SQLite 为源，
     与本脚本无关，不要顺手清理。

【用法】（在 ECS 上：cd /opt/amap-crawler）

  python3 reset_tasks.py                                   # dry-run（默认）：只统计，不写库
  python3 reset_tasks.py --apply                           # 重置 COMPLETED → PENDING
  python3 reset_tasks.py --apply --include-failed          # 连 FAILED（补采）一起归零
  python3 reset_tasks.py --apply --from COMPLETED,RUNNING  # 自定义要归零的源状态

【安全特性】
  * 默认 dry-run，必须显式 --apply 才写库；
  * 单事务更新，提交前校验影响行数；
  * 重置会一并清空租约（lease_device_id / lease_token / lease_expires_at）、
    attempt / recovery_attempt、progress / result_summary，
    避免手机端拿着陈旧租约继续上报（那会导致 409 与 outbox 卡死）。

【退出码】0 = 成功；1 = 失败。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_ENV = ".env"
DEFAULT_TABLE = "henan_heavy_truck_charging_station_task"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="重置 HENAN 采集任务状态（MySQL 权威源，不再触碰 SQLite）"
    )
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只做 dry-run）")
    parser.add_argument("--env", default=DEFAULT_ENV, help=f"含 EVCS_DATABASE_URL 的 env 文件（默认 {DEFAULT_ENV}）")
    parser.add_argument(
        "--from",
        dest="from_statuses",
        default="COMPLETED",
        help="要归零的状态，逗号分隔（默认 COMPLETED）",
    )
    parser.add_argument("--include-failed", action="store_true", help="把 FAILED（补采中的任务）也一并归零")
    parser.add_argument("--table", default=DEFAULT_TABLE, help=f"权威任务表名（默认 {DEFAULT_TABLE}）")
    parser.add_argument("--lock-timeout", type=int, default=15, help="锁等待秒数（默认 15）")
    return parser.parse_args()


def load_evcs_url(env_path: Path) -> str:
    """从 env 文件读取 EVCS_DATABASE_URL（不打印凭据）。"""
    pattern = re.compile(r"^EVCS_DATABASE_URL=(.+)$", re.M)
    match = pattern.search(env_path.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit(f"未在 {env_path} 中找到 EVCS_DATABASE_URL")
    return match.group(1).strip().strip('"').strip("'")


def connect_mysql(env_path: Path, lock_timeout: int):
    import pymysql

    url = load_evcs_url(env_path)
    m = re.match(r"mysql://([^:]+):([^@]+)@([^:/]+):(\d+)/([^?\s]+)", url)
    if not m:
        raise SystemExit("EVCS_DATABASE_URL 形态无法解析（期望 mysql://user:pass@host:port/db）")
    user, password, host, port, database = m.groups()
    conn = pymysql.connect(
        host=host,
        port=int(port),
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        autocommit=True,
    )
    with conn.cursor() as cur:
        cur.execute(f"SET SESSION innodb_lock_wait_timeout={int(lock_timeout)}")
    return conn, database


def status_counts(conn, table: str) -> list:
    with conn.cursor() as cur:
        cur.execute(f"SELECT status, COUNT(*) FROM `{table}` GROUP BY status ORDER BY COUNT(*) DESC")
        return list(cur.fetchall())


def reset_mysql(conn, table: str, statuses: list[str]) -> int:
    """在权威源上把指定状态归零为 PENDING；返回更新行数。

    时间列必须写 `UTC_TIMESTAMP()`：这张表的时间列（available_at /
    lease_expires_at / updated_at ...）全库按 **UTC** 存储，而 api_server 的
    claim / reaper 也用 UTC 做比较。若用 `NOW()`（MySQL 会话时区，本环境是
    +08:00）写入，`available_at` 会比 UTC 基准晚 8 小时，任务将无法被领取。
    """
    placeholders = ", ".join("%s" for _ in statuses)
    sql = f"""UPDATE `{table}`
                 SET status = 'PENDING',
                     attempt = 0,
                     recovery_attempt = 0,
                     lease_device_id = '',
                     lease_token = '',
                     lease_expires_at = NULL,
                     started_at = NULL,
                     finished_at = NULL,
                     last_error = '',
                     progress = NULL,
                     result_summary = NULL,
                     available_at = UTC_TIMESTAMP(),
                     updated_at = UTC_TIMESTAMP()
               WHERE status IN ({placeholders})"""
    with conn.cursor() as cur:
        cur.execute(sql, tuple(statuses))
        return cur.rowcount


def main() -> int:
    args = parse_args()
    env_path = Path(args.env).resolve()
    statuses = [s.strip().upper() for s in args.from_statuses.split(",") if s.strip()]
    if args.include_failed and "FAILED" not in statuses:
        statuses.append("FAILED")
    if not statuses:
        raise SystemExit("--from 不能为空")

    if not env_path.exists():
        raise SystemExit(f"env 文件不存在：{env_path}")

    conn, database = connect_mysql(env_path, args.lock_timeout)
    try:
        counts = status_counts(conn, args.table)
        total = sum(int(row[1]) for row in counts)
        targets = [int(row[1]) for row in counts if str(row[0]).upper() in set(statuses)]
        print(f"=== MySQL 权威源 {database}.{args.table} ===")
        for status, count in counts:
            marker = "  ← 将归零" if str(status).upper() in set(statuses) else ""
            print(f"   {str(status):<12} {int(count):>6}{marker}")
        print(f"   合计 {total} 条；本次待重置（status ∈ {statuses}）: {sum(targets)} 条")

        if not args.apply:
            print("\n[dry-run] 未写任何库。确认无误后加 --apply 执行。")
            print("           提示：如需留档，可在执行前另外 mysqldump 该表。")
            return 0

        changed = reset_mysql(conn, args.table, statuses)
        print(f"\n=== 重置完成 ===\n   影响 {changed} 行 → status=PENDING，租约/尝试次数/进度已清空")
        print("   手机端下一次 claim 即可领到任务；重启也不会回滚（startup 已不再回灌 SQLite）")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
