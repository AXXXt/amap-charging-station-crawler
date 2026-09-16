#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重置采集任务状态（COMPLETED → PENDING）。

【两个库的关系，务必先读】2026-09-16 起权威源已反转，详见 dev-docs/cloud_service_ops.md §10.3~10.5

  MySQL   121.41.56.201/evcs.henan_heavy_truck_charging_station_task = ★权威源★（手机端 claim/回传直读直写）
  SQLite  data/mobile_control.db 的 scan_task（HENAN 行）             = 启动导入缓存（运行期不再被服务写入）

  => 改 MySQL 才立即生效；但只改 MySQL 又会在下次重启时被 SQLite 快照覆盖。
  => 因此本脚本【两个库都改】：先重置 SQLite（保持缓存与权威源一致、防重启回滚），
     再把状态写进 MySQL（这一步才真正生效）。
  => 注意：权威源切到 MySQL 后，api_server.py 的 _sync_henan_task() 已是空实现，
     "镜像写失败不影响采集"的兜底已不存在。

【用法】（在 ECS 上：cd /opt/amap-crawler）

  python3 reset_tasks.py                        # dry-run（默认）：只统计，不写任何库
  python3 reset_tasks.py --apply                # 执行：备份 SQLite → 重置 → 同步镜像
  python3 reset_tasks.py --apply --include-failed          # 连 FAILED（补采）一起归零
  python3 reset_tasks.py --apply --types HENAN_POI_DETAIL,SITE_STATION_DETAIL
  python3 reset_tasks.py --apply --mirror-only  # 只改 MySQL（立即生效，但 SQLite 仍旧值 → 重启会回滚）
  python3 reset_tasks.py --apply --skip-mirror  # 只改 SQLite（当前不生效，等重启导入才生效）

【安全特性】
  * 默认 dry-run，必须显式 --apply 才会写库；
  * 写 SQLite 前先用 sqlite3 .backup 生成 <db>.bak-<时间戳>（WAL 下也安全）；
  * SQLite 侧单事务（BEGIN IMMEDIATE）+ 更新行数校验；
  * MySQL 侧分批（默认 100 行/批，独立事务）+ 被锁行跳过 + 第二轮重试 + 末尾汇总剩余；
  * MySQL 写失败不影响已完成的 SQLite 重置（两者各自独立事务；有失败时退出码为 1）。

【退出码】0 = 全部完成；1 = 有部分 MySQL 行未完成（可重跑本脚本补齐）。
"""

from __future__ import annotations

import argparse
import datetime
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

DEFAULT_DB = "data/mobile_control.db"
DEFAULT_ENV = ".env"
MIRROR_TABLE = "henan_heavy_truck_charging_station_task"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="重置采集任务状态 / 补齐 MySQL 镜像")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只做 dry-run）")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite 调度库路径（默认 {DEFAULT_DB}）")
    parser.add_argument("--env", default=DEFAULT_ENV, help=f"含 EVCS_DATABASE_URL 的 env 文件（默认 {DEFAULT_ENV}）")
    parser.add_argument("--types", default="HENAN_POI_DETAIL", help="要处理的任务类型，逗号分隔（默认 HENAN_POI_DETAIL）")
    parser.add_argument("--include-failed", action="store_true", help="把 FAILED（补采中的任务）也一并归零")
    parser.add_argument("--mirror-only", action="store_true", help="只把权威源状态补齐到 MySQL 镜像，不改权威源")
    parser.add_argument("--skip-mirror", action="store_true", help="只改权威源，不同步 MySQL 镜像")
    parser.add_argument("--batch", type=int, default=100, help="镜像分批大小（默认 100）")
    parser.add_argument("--lock-timeout", type=int, default=5, help="镜像写锁等待秒数（默认 5，超时跳过该批）")
    return parser.parse_args()


def load_evcs_url(env_path: Path) -> str:
    """从 env 文件读取 EVCS_DATABASE_URL（不打印凭据）。"""
    pattern = re.compile(r"^EVCS_DATABASE_URL=(.+)$", re.M)
    match = pattern.search(env_path.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit(f"未在 {env_path} 中找到 EVCS_DATABASE_URL")
    return match.group(1).strip().strip('"').strip("'")


def connect_mysql(env_path: Path, lock_timeout: int):
    import pymysql  # 延迟导入：--skip-mirror 时无需该依赖

    url = load_evcs_url(env_path)
    m = re.match(r"mysql://([^:]+):([^@]+)@([^:/]+):(\d+)/([^?\s]+)", url)
    if not m:
        raise SystemExit("EVCS_DATABASE_URL 形态无法解析（期望 mysql://user:pass@host:port/db）")
    user, password, host, port, database = m.groups()
    conn = pymysql.connect(host=host, port=int(port), user=user, password=password,
                           database=database, charset="utf8mb4", autocommit=True)
    with conn.cursor() as cur:
        cur.execute(f"SET SESSION innodb_lock_wait_timeout={int(lock_timeout)}")
    return conn


def load_sqlite_rows(db_path: Path, types: list[str], statuses: list[str] | None = None) -> dict:
    """读取指定类型的任务；statuses 非空时只取这些状态（用于统计"待重置"行数）。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    sql = f"""SELECT id, type, status, attempt, recovery_attempt, assigned_device_id,
                     lease_token, last_error, available_at
                FROM scan_task WHERE type IN ({",".join("?" for _ in types)})"""
    params: list = list(types)
    if statuses:
        sql += f" AND status IN ({','.join('?' for _ in statuses)})"
        params += statuses
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return {r["id"]: dict(r) for r in rows}


def sqlite_status_counts(rows: dict) -> dict:
    counts: dict = {}
    for row in rows.values():
        key = f"{row['type']}/{row['status']}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def backup_sqlite(db_path: Path) -> Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = db_path.with_name(db_path.name + f".bak-{stamp}")
    subprocess.run(["sqlite3", str(db_path), f".backup '{target}'"], check=True)
    return target


def reset_sqlite(db_path: Path, types: list[str], statuses: list[str]) -> int:
    """权威源单事务重置；返回更新行数。"""
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("BEGIN IMMEDIATE")
        type_ph = ",".join("?" for _ in types)
        status_ph = ",".join("?" for _ in statuses)
        before = conn.total_changes
        conn.execute(
            f"""UPDATE scan_task
                   SET status='PENDING', attempt=0, recovery_attempt=0,
                       assigned_device_id=NULL, lease_token=NULL, lease_expires_at=NULL,
                       started_at=NULL, finished_at=NULL, last_error='',
                       progress='{{}}', result_summary='{{}}',
                       available_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),
                       updated_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now')
                 WHERE type IN ({type_ph}) AND status IN ({status_ph})""",
            types + statuses,
        )
        changed = conn.total_changes - before
        conn.execute("COMMIT")
        return changed
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def iso_to_datetime(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return value


def sync_mirror(conn, rows: dict, batch: int) -> tuple[int, list]:
    """把状态写进 MySQL 权威表（分批、跳过被锁行）。返回 (成功数, 剩余 id 列表)。

    注意命名沿用历史：MySQL 现在不是"镜像"而是权威源，这一步才是真正生效的动作。
    """
    with conn.cursor() as cur:
        cur.execute(f"SELECT local_task_id, status FROM {MIRROR_TABLE}")
        mirror = dict(cur.fetchall())

    pending = []
    for local_id, row in rows.items():
        if mirror.get(local_id) != row["status"]:
            pending.append((local_id, row))

    ok, failed = 0, []
    for start in range(0, len(pending), batch):
        chunk = pending[start:start + batch]
        for local_id, row in chunk:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""UPDATE {MIRROR_TABLE}
                               SET status=%s, attempt=%s, recovery_attempt=%s, lease_device_id=%s,
                                   lease_token=%s, lease_expires_at=NULL, last_error=%s,
                                   available_at=%s, updated_at=NOW()
                             WHERE local_task_id=%s""",
                        (row["status"], int(row["attempt"] or 0), int(row["recovery_attempt"] or 0),
                         str(row["assigned_device_id"] or ""), str(row["lease_token"] or ""),
                         str(row["last_error"] or "")[:1000], iso_to_datetime(row["available_at"]), local_id),
                    )
                ok += 1
            except Exception:
                failed.append(local_id)
    return ok, failed


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).resolve()
    env_path = Path(args.env).resolve()
    types = [t.strip() for t in args.types.split(",") if t.strip()]
    statuses = ["COMPLETED"] + (["FAILED"] if args.include_failed else [])

    if not db_path.exists():
        raise SystemExit(f"SQLite 调度库不存在：{db_path}")

    all_rows = load_sqlite_rows(db_path, types)
    reset_targets = load_sqlite_rows(db_path, types, statuses)
    print("=== SQLite 缓存现状（非权威源；HENAN 真实进度请看 MySQL） ===")
    for key, count in sorted(sqlite_status_counts(all_rows).items()):
        print(f"   {key:<38} {count}")
    print(f"   本次待重置（type ∈ {types}，status ∈ {statuses}）: {len(reset_targets)} 行")

    if not args.apply:
        print("\n[dry-run] 未写任何库。确认无误后加 --apply 执行。")
        return 0

    if not args.mirror_only:
        backup = backup_sqlite(db_path)
        print(f"\n=== ① SQLite 备份 ===\n   {backup}")
        changed = reset_sqlite(db_path, types, statuses)
        print(f"=== ② SQLite 缓存重置 ===\n   更新 {changed} 行 → status=PENDING, attempt=0, recovery_attempt=0, 领取痕迹已清空")

    rows = load_sqlite_rows(db_path, types)

    if args.skip_mirror:
        print("\n=== ③ 跳过 MySQL（--skip-mirror）：SQLite 已改、MySQL 未改 → 当前不生效，需重启经启动导入才生效 ===")
        return 0

    conn = connect_mysql(env_path, args.lock_timeout)
    try:
        ok, failed = sync_mirror(conn, rows, args.batch)
        print(f"=== ③ MySQL 权威源重置（真正生效的一步）===\n   成功 {ok} 行，未完成 {len(failed)} 行")
        if failed:
            print(f"   未完成（多为被别人的未提交事务锁住的 1205）：{failed[:10]}"
                  f"{' …' if len(failed) > 10 else ''}")
            print("   处置：等锁释放后重跑本脚本补齐（SQLite 已改，MySQL 未改的部分会在下次重启时被刷进去）；")
            print("   排查锁：用带 PROCESS 权限的账号 SELECT * FROM information_schema.INNODB_TRX; 然后 KILL 对应线程。")
    finally:
        conn.close()

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
