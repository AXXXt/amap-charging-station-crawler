"""Bridge site_exploration_site JSON rows into sequential mobile collection tasks."""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import pymysql


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TASK_TYPE = "SITE_STATION_DETAIL"
HENAN_POI_TASK_TYPE = "HENAN_POI_DETAIL"
RESULT_TASK_TYPES = {TASK_TYPE, HENAN_POI_TASK_TYPE}
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}
ACTIVE_STATUSES = {"PENDING", "LEASED", "RUNNING"}
IDLE_PILE_STATUSES = {"空闲", "空"}
BUSY_PILE_STATUSES = {"充电中", "使用中", "占用", "已满"}


def parse_mysql_database_url(value: str) -> Dict[str, Any]:
    """Parse mysql://user:password@host:port/database without exposing it to Android."""
    value = (value or "").strip()
    if not value:
        return {}
    # A copied Markdown value may contain an escaped @ before the host.
    value = value.replace(r"\@", "@")
    parsed = urlparse(value)
    if parsed.scheme not in {"mysql", "mysql+pymysql"}:
        raise ValueError("EVCS_DATABASE_URL must use mysql:// or mysql+pymysql://")
    database = unquote(parsed.path.lstrip("/"))
    if not parsed.hostname or not parsed.username or not database:
        raise ValueError("EVCS_DATABASE_URL is missing host, user, or database")
    return {
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": unquote(parsed.username),
        "password": unquote(parsed.password or ""),
        "database": database,
    }


def safe_identifier(value: str, label: str) -> str:
    value = (value or "").strip()
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def quote_identifier(value: str, label: str) -> str:
    return f"`{safe_identifier(value, label)}`"


def station_source_key(station_id: Any, station_name: Any = "") -> str:
    """Return one cross-flow station key, preferring the stable Amap POI id."""
    poi_id = str(station_id or "").strip().upper()
    if poi_id:
        identity = f"amap-poi:{poi_id}"
    else:
        normalized_name = unicodedata.normalize("NFKC", str(station_name or ""))
        normalized_name = re.sub(r"\s+", "", normalized_name).casefold()
        identity = f"station-name:{normalized_name}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _fit_text(value: Any, limit: Optional[int]) -> Any:
    """按结果表字段长度截断文本，避免单条异常内容阻塞整批同步。"""
    if value is None or not limit or limit <= 0:
        return value
    text = str(value)
    return text if len(text) <= limit else text[:limit]


def _text_limit(column_type: Any) -> Optional[int]:
    """从 varchar(N) 类型声明中解析可写入的最大字符数。"""
    match = re.search(r"(?:var)?char\((\d+)\)", str(column_type or ""), re.IGNORECASE)
    return int(match.group(1)) if match else None


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        text = html.unescape(value).strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return None
    return None


def parse_station_list(value: Any) -> List[Dict[str, Any]]:
    parsed = _json_value(value)
    if isinstance(parsed, dict):
        for key in ("stations", "data", "items", "list"):
            candidate = parsed.get(key)
            if isinstance(candidate, list):
                parsed = candidate
                break
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _as_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _pile_snapshot_summary(payload: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, int]], Dict[str, int]]:
    """按逐桩状态统计快充、超充、慢充的空闲/忙碌/未知数量。"""
    summary = {
        "fast": {"idle": 0, "busy": 0, "total": 0},
        "super": {"idle": 0, "busy": 0, "total": 0},
        "slow": {"idle": 0, "busy": 0, "total": 0},
    }
    overall = {"idle": 0, "busy": 0, "unknown": 0, "total": 0}
    piles = payload.get("chargingPiles")
    if not isinstance(piles, list):
        piles = []

    if piles:
        for pile in piles:
            if not isinstance(pile, dict):
                continue
            charging_type = str(pile.get("chargingType") or "")
            if "超" in charging_type:
                group = "super"
            elif "慢" in charging_type:
                group = "slow"
            else:
                group = "fast"
            status = str(pile.get("status") or "").strip()
            summary[group]["total"] += 1
            overall["total"] += 1
            if status in IDLE_PILE_STATUSES:
                summary[group]["idle"] += 1
                overall["idle"] += 1
            elif status in BUSY_PILE_STATUSES:
                summary[group]["busy"] += 1
                overall["busy"] += 1
            else:
                overall["unknown"] += 1
        return summary, overall

    # 没有逐桩明细时退化为顶部汇总数量；忙碌数按总数减去可用数计算。
    for group, prefix in (("fast", "fast"), ("super", "super"), ("slow", "slow")):
        total = max(0, _as_int(payload.get(f"{prefix}Total"), 0))
        idle = max(0, _as_int(payload.get(f"{prefix}Available"), 0))
        busy = max(0, total - idle)
        summary[group] = {"idle": idle, "busy": busy, "total": total}
        overall["idle"] += idle
        overall["busy"] += busy
        overall["total"] += total
    overall["unknown"] = max(0, overall["total"] - overall["idle"] - overall["busy"])
    return summary, overall


def _station_key(source_site_id: str, station: Dict[str, Any], index: int) -> Tuple[str, str]:
    """Return a stable *global* identity for one charging station.

    ``source_site_id`` is intentionally not part of the task id.  The source
    table can mention the same station from more than one exploration row; it
    is still one physical station and must be collected by only one device.
    The source site id remains in the task payload/result for traceability.
    """
    del source_site_id, index  # retained in the signature for bridge callers
    station_id = str(station.get("id") or "").strip()
    name = " ".join(str(station.get("name") or "").split())
    address = " ".join(str(station.get("address") or "").split())
    identity = station_id or hashlib.sha256(
        f"{name}\0{address}".encode("utf-8")
    ).hexdigest()[:24]
    source_station_id = station_id or f"derived-{identity}"
    digest = hashlib.sha256(f"station\0{source_station_id}".encode("utf-8")).hexdigest()
    return source_station_id, f"site-{digest[:32]}"


def _utc_at_offset(offset: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(microseconds=offset)).isoformat()


class SiteExplorationBridge:
    def __init__(self, db_config: Dict[str, Any], enabled: bool):
        self.db_config = dict(db_config)
        self.enabled = enabled
        self.source_table = safe_identifier(
            os.getenv("SITE_EXPLORATION_TABLE", "site_exploration_site"),
            "site exploration table",
        )
        self.station_column = safe_identifier(
            os.getenv("SITE_EXPLORATION_STATION_COLUMN", "nearby_truck_charging_stations"),
            "station JSON column",
        )
        self.configured_id_column = os.getenv("SITE_EXPLORATION_ID_COLUMN", "id").strip()
        self.configured_order_column = os.getenv("SITE_EXPLORATION_ORDER_COLUMN", "id").strip()
        self.result_table = safe_identifier(
            os.getenv(
                "SITE_EXPLORATION_RESULT_TABLE",
                "site_exploration_charging_station_result",
            ),
            "site exploration result table",
        )
        self.dynamic_history_table = safe_identifier(
            os.getenv(
                "SITE_EXPLORATION_DYNAMIC_HISTORY_TABLE",
                "site_exploration_charging_station_dynamic_history",
            ),
            "site exploration dynamic history table",
        )
        self.priority = int(os.getenv("SITE_EXPLORATION_TASK_PRIORITY", "1000"))
        self.max_attempts = int(os.getenv("SITE_EXPLORATION_TASK_MAX_ATTEMPTS", "3"))
        self._result_column_meta: Dict[str, Dict[str, Any]] = {}

    def mysql_connect(self):
        return pymysql.connect(**self.db_config)

    def ensure_local_schema(self, conn) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(scan_task)").fetchall()}
        additions = {
            "source_site_id": "TEXT NOT NULL DEFAULT ''",
            "source_site_order": "INTEGER NOT NULL DEFAULT 0",
            "source_station_id": "TEXT NOT NULL DEFAULT ''",
            "source_sequence": "INTEGER NOT NULL DEFAULT 0",
            "source_payload": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, ddl in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE scan_task ADD COLUMN {name} {ddl}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scan_task_source_order "
            "ON scan_task(type, source_site_order, source_sequence, status)"
        )

    def ensure_result_table(self) -> None:
        if not self.enabled:
            return
        table = quote_identifier(self.result_table, "result table")
        conn = self.mysql_connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
                        source_key CHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '充电站跨采集来源稳定唯一键（优先使用高德POI编号生成）',
                        observation_id VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '本次采集观测记录唯一标识',
                        task_id VARCHAR(40) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '采集任务编号',
                        device_id VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '执行采集的设备编号',
                        collection_source VARCHAR(32) COLLATE utf8mb4_general_ci NOT NULL DEFAULT 'SITE_EXPLORATION' COMMENT '最近一次结果来源：SITE_EXPLORATION/HENAN_POI',
                        province VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '省份',
                        city VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '城市',
                        district VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '区县',
                        source_site_id VARCHAR(191) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '周边站点任务来源站点编号；河南POI任务为空',
                        source_station_id VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '来源数据中的充电站编号，优先保存高德POI编号',
                        requested_name VARCHAR(512) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '任务请求搜索的站点名称',
                        matched_station_name VARCHAR(512) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '地图页面匹配到的充电站名称',
                        source_address VARCHAR(1000) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '来源数据中的站点地址',
                        collected_address VARCHAR(1000) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '采集页面展示的充电站地址',
                        source_latitude DECIMAL(12,8) NOT NULL DEFAULT 0.00000000 COMMENT '来源数据纬度，缺失时为 0',
                        source_longitude DECIMAL(12,8) NOT NULL DEFAULT 0.00000000 COMMENT '来源数据经度，缺失时为 0',
                        operator VARCHAR(255) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '充电站运营方',
                        business_hours VARCHAR(255) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '营业时间',
                        current_price VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '当前充电价格原始文本',
                        parking_fee VARCHAR(1000) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '停车费说明',
                        fast_available VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '快充可用数量',
                        fast_total VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '快充总数量',
                        fast_power VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '快充功率说明',
                        super_available VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '超充可用数量',
                        super_total VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '超充总数量',
                        super_power VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '超充功率说明',
                        slow_available VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '慢充可用数量',
                        slow_total VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '慢充总数量',
                        slow_power VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '慢充功率说明',
                        result_payload JSON NOT NULL DEFAULT (JSON_OBJECT()) COMMENT '采集结果原始数据 JSON',
                        captured_at INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '采集发生时间（Unix 时间戳，秒）',
                        received_at INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '服务端接收时间（Unix 时间戳，秒）',
                        created_at INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '创建时间（Unix 时间戳，秒）',
                        updated_at INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '更新时间（Unix 时间戳，秒）',
                        PRIMARY KEY (id),
                        UNIQUE KEY uk_site_collection_source (source_key),
                        UNIQUE KEY uk_site_collection_observation (observation_id),
                        KEY idx_site_collection_type_city (collection_source, city),
                        KEY idx_site_collection_site (source_site_id),
                        KEY idx_site_collection_station (source_station_id),
                        KEY idx_site_collection_received (received_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_general_ci
                      COMMENT='统一充电站安卓采集结果表'
                    """
                )
                cursor.execute(f"SHOW COLUMNS FROM {table}")
                existing_columns = {str(row[0]) for row in cursor.fetchall()}
                additions = {
                    "collection_source": "VARCHAR(32) COLLATE utf8mb4_general_ci NOT NULL DEFAULT 'SITE_EXPLORATION' COMMENT '最近一次结果来源：SITE_EXPLORATION/HENAN_POI' AFTER device_id",
                    "province": "VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '省份' AFTER collection_source",
                    "city": "VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '城市' AFTER province",
                    "district": "VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '区县' AFTER city",
                }
                for column, ddl in additions.items():
                    if column not in existing_columns:
                        cursor.execute(f"ALTER TABLE {table} ADD COLUMN `{column}` {ddl}")
                cursor.execute(f"SHOW INDEX FROM {table} WHERE Key_name = 'idx_site_collection_type_city'")
                if cursor.fetchone() is None:
                    cursor.execute(
                        f"CREATE INDEX idx_site_collection_type_city "
                        f"ON {table} (collection_source, city)"
                    )
            conn.commit()
            self._result_column_meta = self._result_column_metadata(conn, table)
        finally:
            conn.close()
        self._ensure_dynamic_history_table()

    def _ensure_dynamic_history_table(self) -> None:
        if not self.enabled:
            return
        table = quote_identifier(self.dynamic_history_table, "dynamic history table")
        conn = self.mysql_connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
                        snapshot_key CHAR(64) COLLATE utf8mb4_general_ci NOT NULL COMMENT '站点source_key+captured_at生成的快照稳定键',
                        source_key CHAR(64) COLLATE utf8mb4_general_ci NOT NULL COMMENT '站点稳定唯一键',
                        source_station_id VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '高德POI编号',
                        observation_id VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '采集观测标识',
                        task_id VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '任务编号',
                        device_id VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '采集设备编号',
                        collection_source VARCHAR(32) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '任务来源',
                        matched_station_name VARCHAR(512) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '站点名称',
                        captured_at INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '采集时间，Unix秒',
                        received_at INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '服务端接收时间，Unix秒',
                        current_price VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '采集时的当前电价',
                        fast_idle INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '快充空闲数量',
                        fast_busy INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '快充忙碌数量',
                        fast_total INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '快充总数量',
                        super_idle INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '超充空闲数量',
                        super_busy INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '超充忙碌数量',
                        super_total INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '超充总数量',
                        slow_idle INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '慢充空闲数量',
                        slow_busy INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '慢充忙碌数量',
                        slow_total INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '慢充总数量',
                        pile_idle INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '全部充电桩空闲数量',
                        pile_busy INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '全部充电桩忙碌数量',
                        pile_unknown INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '状态未知的充电桩数量',
                        pile_total INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '充电桩总数量',
                        price_json JSON NULL COMMENT '分时电价、服务费、当前价格等动态价格数据',
                        availability_json JSON NULL COMMENT '快充/超充/慢充空闲、忙碌、总数汇总',
                        pile_json JSON NULL COMMENT '每根充电桩的编号、类型、功率、电流、电压、状态列表',
                        snapshot_hash CHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '动态数据内容哈希',
                        is_changed TINYINT(1) NOT NULL DEFAULT 1 COMMENT '相对该站点上一条快照是否发生变化',
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
                        PRIMARY KEY (id),
                        UNIQUE KEY uk_dynamic_snapshot (snapshot_key),
                        KEY idx_dynamic_station_time (source_key, captured_at),
                        KEY idx_dynamic_station_changed (source_key, is_changed, captured_at),
                        KEY idx_dynamic_captured_at (captured_at),
                        KEY idx_dynamic_task (task_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_general_ci
                      COMMENT='站点动态信息历史快照表，用于电价和充电桩状态趋势分析'
                    """
                )
            conn.commit()
        finally:
            conn.close()

    def _result_column_metadata(self, conn, table: str) -> Dict[str, Dict[str, Any]]:
        """读取结果表元数据，以兼容升级前的线上 DATETIME 表。"""
        with conn.cursor() as cursor:
            cursor.execute(f"SHOW COLUMNS FROM {table}")
            rows = cursor.fetchall()
        return {
            str(row[0]): {
                "type": str(row[1]).lower(),
                "nullable": str(row[2]).upper() == "YES",
            }
            for row in rows
        }

    def _resolve_source_columns(self, conn) -> Tuple[str, str]:
        table = quote_identifier(self.source_table, "source table")
        with conn.cursor() as cursor:
            cursor.execute(f"SHOW COLUMNS FROM {table}")
            rows = cursor.fetchall()
        columns = [str(row[0]) for row in rows]
        if self.station_column not in columns:
            raise RuntimeError(
                f"{self.source_table}.{self.station_column} does not exist"
            )
        primary = next((str(row[0]) for row in rows if str(row[3]) == "PRI"), "")
        id_column = self.configured_id_column if self.configured_id_column in columns else primary
        if not id_column:
            id_column = "id" if "id" in columns else columns[0]
        order_column = (
            self.configured_order_column
            if self.configured_order_column in columns
            else id_column
        )
        return safe_identifier(id_column, "source id column"), safe_identifier(
            order_column, "source order column"
        )

    def _iter_source_rows(self) -> Iterable[Tuple[int, str, Any]]:
        conn = self.mysql_connect()
        try:
            id_column, order_column = self._resolve_source_columns(conn)
            table = quote_identifier(self.source_table, "source table")
            id_sql = quote_identifier(id_column, "source id column")
            order_sql = quote_identifier(order_column, "source order column")
            station_sql = quote_identifier(self.station_column, "station JSON column")
            with conn.cursor() as cursor:
                cursor.execute(
                    f"SELECT {id_sql}, {station_sql} FROM {table} "
                    f"WHERE {station_sql} IS NOT NULL ORDER BY {order_sql}, {id_sql}"
                )
                for source_order, row in enumerate(cursor, start=1):
                    yield source_order, str(row[0]), row[1]
        finally:
            conn.close()

    def sync_next_site(self, local_conn_factory: Callable[[], Any]) -> int:
        """Materialize exactly one source row at a time into the local mobile queue."""
        if not self.enabled:
            return 0

        local = local_conn_factory()
        try:
            self.ensure_local_schema(local)
            active = local.execute(
                """SELECT source_site_id FROM scan_task
                   WHERE type = ? AND (
                         status IN ('LEASED', 'RUNNING')
                         OR (status = 'PENDING' AND attempt < max_attempts)
                       )
                   ORDER BY source_site_order, source_sequence LIMIT 1""",
                (TASK_TYPE,),
            ).fetchone()
            local.commit()
            if active is not None:
                return 0
        finally:
            local.close()

        for source_order, source_site_id, raw_stations in self._iter_source_rows():
            stations = parse_station_list(raw_stations)
            task_specs = []
            seen_station_ids = set()
            for index, station in enumerate(stations, start=1):
                name = str(station.get("name") or "").strip()
                if not name:
                    continue
                source_station_id, task_id = _station_key(source_site_id, station, index)
                # A source row can contain the same station more than once.
                # Materialize it only once before touching the queue.
                if source_station_id in seen_station_ids:
                    continue
                seen_station_ids.add(source_station_id)
                sequence = _as_int(station.get("sequence"), index)
                task_specs.append(
                    {
                        "id": task_id,
                        "source_station_id": source_station_id,
                        "sequence": sequence,
                        "name": name,
                        "address": str(station.get("address") or "").strip(),
                        "payload": json.dumps(station, ensure_ascii=False),
                    }
                )
            if not task_specs:
                continue

            local = local_conn_factory()
            try:
                self.ensure_local_schema(local)
                local.execute("BEGIN IMMEDIATE")
                placeholders = ",".join("?" for _ in task_specs)
                existing_rows = local.execute(
                    f"SELECT id, status, attempt, max_attempts FROM scan_task WHERE id IN ({placeholders})",
                    [item["id"] for item in task_specs],
                ).fetchall()
                existing = {row["id"]: row for row in existing_rows}
                nonterminal = [
                    row for row in existing.values()
                    if row["status"] in {"LEASED", "RUNNING"}
                    or (row["status"] == "PENDING" and row["attempt"] < row["max_attempts"])
                ]
                if nonterminal:
                    local.commit()
                    return 0

                missing = [item for item in task_specs if item["id"] not in existing]
                if not missing:
                    local.commit()
                    continue

                for offset, item in enumerate(sorted(missing, key=lambda value: value["sequence"])):
                    created_at = _utc_at_offset(offset)
                    local.execute(
                        """INSERT OR IGNORE INTO scan_task (
                               id, type, priority, province, city, district, keyword,
                               search_region, status, assigned_device_id, lease_token,
                               lease_expires_at, attempt, max_attempts, progress,
                               result_summary, available_at, created_at, updated_at, started_at,
                               finished_at, last_error, source_site_id, source_site_order,
                               source_station_id, source_sequence, source_payload
                           ) VALUES (?, ?, ?, '', '', '', ?, ?, 'PENDING', NULL, NULL,
                                     NULL, 0, ?, '{}', '{}', ?, ?, ?, NULL, NULL, '', ?, ?, ?, ?, ?)""",
                        (
                            item["id"],
                            TASK_TYPE,
                            self.priority,
                            item["name"],
                            item["address"],
                            self.max_attempts,
                            created_at,
                            created_at,
                            created_at,
                            source_site_id,
                            source_order,
                            item["source_station_id"],
                            item["sequence"],
                            item["payload"],
                        ),
                    )
                local.commit()
                return len(missing)
            except Exception:
                local.rollback()
                raise
            finally:
                local.close()
        return 0

    def task_payload_fields(self, row: Any) -> Dict[str, Any]:
        if row is None or row["type"] != TASK_TYPE:
            return {}
        source_payload = _json_value(row["source_payload"]) or {}
        return {
            "sourceSiteId": row["source_site_id"],
            "sourceSiteOrder": row["source_site_order"],
            "stationId": row["source_station_id"],
            "stationName": str(source_payload.get("name") or row["keyword"] or ""),
            "stationAddress": str(source_payload.get("address") or row["search_region"] or ""),
            "stationLatitude": _as_float(source_payload.get("latitude")),
            "stationLongitude": _as_float(source_payload.get("longitude")),
            "stationSequence": row["source_sequence"],
            "sourcePayload": source_payload,
        }

    def _append_dynamic_snapshot(
        self,
        conn,
        source_key: str,
        source_station_id: str,
        observation_id: str,
        task_id: str,
        device_id: str,
        collection_source: str,
        matched_station_name: str,
        payload: Dict[str, Any],
        captured_at: Any,
        received_at: Any,
    ) -> None:
        """Append one dynamic snapshot without affecting the latest-result upsert."""
        captured_ts = unix_timestamp(captured_at or payload.get("collectedAt")) or int(
            datetime.now(timezone.utc).timestamp()
        )
        received_ts = unix_timestamp(received_at) or captured_ts
        snapshot_key = hashlib.sha256(
            f"{source_key}:{captured_ts}".encode("utf-8")
        ).hexdigest()
        summary, overall = _pile_snapshot_summary(payload)
        price_json = {
            "currentPrice": str(payload.get("currentPrice") or ""),
            "priceTrendTitle": str(payload.get("priceTrendTitle") or ""),
            "fastPrices": payload.get("fastPrices") or [],
            "slowPrices": payload.get("slowPrices") or [],
        }
        pile_json = payload.get("chargingPiles") or []
        if not isinstance(pile_json, list):
            pile_json = []
        availability_json = {
            "fast": summary["fast"],
            "super": summary["super"],
            "slow": summary["slow"],
            "pile": overall,
        }
        snapshot_hash = hashlib.sha256(
            json.dumps(
                {
                    "price": price_json,
                    "availability": availability_json,
                    "piles": pile_json,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        table = quote_identifier(self.dynamic_history_table, "dynamic history table")
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT snapshot_hash FROM {table} "
                "WHERE source_key=%s AND snapshot_key<>%s "
                "ORDER BY captured_at DESC, id DESC LIMIT 1",
                (source_key, snapshot_key),
            )
            previous = cursor.fetchone()
            is_changed = 1 if previous is None or str(previous[0] or "") != snapshot_hash else 0
            values = {
                "snapshot_key": snapshot_key,
                "source_key": source_key,
                "source_station_id": str(source_station_id or ""),
                "observation_id": str(observation_id or ""),
                "task_id": str(task_id or ""),
                "device_id": str(device_id or ""),
                "collection_source": str(collection_source or ""),
                "matched_station_name": _fit_text(matched_station_name, 512),
                "captured_at": captured_ts,
                "received_at": received_ts,
                "current_price": _fit_text(str(payload.get("currentPrice") or ""), 128),
                "fast_idle": summary["fast"]["idle"],
                "fast_busy": summary["fast"]["busy"],
                "fast_total": summary["fast"]["total"],
                "super_idle": summary["super"]["idle"],
                "super_busy": summary["super"]["busy"],
                "super_total": summary["super"]["total"],
                "slow_idle": summary["slow"]["idle"],
                "slow_busy": summary["slow"]["busy"],
                "slow_total": summary["slow"]["total"],
                "pile_idle": overall["idle"],
                "pile_busy": overall["busy"],
                "pile_unknown": overall["unknown"],
                "pile_total": overall["total"],
                "price_json": json.dumps(price_json, ensure_ascii=False),
                "availability_json": json.dumps(availability_json, ensure_ascii=False),
                "pile_json": json.dumps(pile_json, ensure_ascii=False),
                "snapshot_hash": snapshot_hash,
                "is_changed": is_changed,
            }
            columns = list(values)
            updates = ", ".join(
                f"{column}=VALUES({column})"
                for column in columns
                if column != "snapshot_key"
            )
            cursor.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"VALUES ({', '.join('%s' for _ in columns)}) "
                f"ON DUPLICATE KEY UPDATE {updates}",
                tuple(values[column] for column in columns),
            )

    def upsert_result(
        self,
        task: Any,
        observation_id: str,
        station_id: str,
        device_id: str,
        payload: Dict[str, Any],
        captured_at: Any,
        received_at: Any,
    ) -> None:
        if not self.enabled or task is None or task["type"] not in RESULT_TASK_TYPES:
            return
        source_payload = _json_value(task["source_payload"]) or {}
        is_henan_poi = task["type"] == HENAN_POI_TASK_TYPE
        collection_source = "HENAN_POI" if is_henan_poi else "SITE_EXPLORATION"
        source_site_id = "" if is_henan_poi else str(task["source_site_id"] or "")
        source_station_id = str(task["source_station_id"] or station_id or "")
        requested_name = str(source_payload.get("name") or task["keyword"] or "")
        matched_name = str(payload.get("stationName") or requested_name)
        source_key = station_source_key(source_station_id, matched_name or requested_name)
        table = quote_identifier(self.result_table, "result table")
        conn = self.mysql_connect()
        try:
            column_meta = self._result_column_meta or self._result_column_metadata(conn, table)
            self._result_column_meta = column_meta
            uses_unix_time = column_meta.get("captured_at", {}).get("type", "").startswith("int")
            now_value = unix_timestamp(received_at) or int(datetime.now(timezone.utc).timestamp())
            captured_value = (
                unix_timestamp(captured_at or payload.get("collectedAt"))
                if uses_unix_time
                else mysql_datetime(captured_at or payload.get("collectedAt"))
            )
            received_value = (
                unix_timestamp(received_at) or now_value
                if uses_unix_time
                else mysql_datetime(received_at) or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )
            if not column_meta.get("source_latitude", {}).get("nullable", True):
                source_latitude = _as_float(source_payload.get("latitude")) or 0.0
                source_longitude = _as_float(source_payload.get("longitude")) or 0.0
            else:
                source_latitude = _as_float(source_payload.get("latitude"))
                source_longitude = _as_float(source_payload.get("longitude"))
            created_value = (
                mysql_datetime(received_at) or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if not uses_unix_time
                else now_value
            )
            values = {
                "source_key": source_key,
                "observation_id": str(observation_id or ""),
                "task_id": str(task["id"] or ""),
                "device_id": str(device_id or ""),
                "collection_source": collection_source,
                "province": str(payload.get("province") or source_payload.get("province") or task["province"] or ""),
                "city": str(payload.get("city") or source_payload.get("city") or task["city"] or ""),
                "district": str(payload.get("district") or source_payload.get("district") or task["district"] or ""),
                "source_site_id": source_site_id,
                "source_station_id": source_station_id,
                "requested_name": requested_name,
                "matched_station_name": matched_name,
                "source_address": str(source_payload.get("address") or task["search_region"] or ""),
                "collected_address": str(payload.get("address") or ""),
                "source_latitude": source_latitude,
                "source_longitude": source_longitude,
                "operator": str(payload.get("operator") or ""),
                "business_hours": str(payload.get("businessHours") or ""),
                "current_price": str(payload.get("currentPrice") or ""),
                "parking_fee": str(payload.get("parkingFee") or ""),
                "fast_available": str(payload.get("fastAvailable") or ""),
                "fast_total": str(payload.get("fastTotal") or ""),
                "fast_power": str(payload.get("fastPower") or ""),
                "super_available": str(payload.get("superAvailable") or ""),
                "super_total": str(payload.get("superTotal") or ""),
                "super_power": str(payload.get("superPower") or ""),
                "slow_available": str(payload.get("slowAvailable") or ""),
                "slow_total": str(payload.get("slowTotal") or ""),
                "slow_power": str(payload.get("slowPower") or ""),
                "result_payload": json.dumps(payload, ensure_ascii=False),
                "captured_at": captured_value,
                "received_at": received_value,
                "created_at": created_value,
                "updated_at": created_value,
            }
            for column, value in list(values.items()):
                values[column] = _fit_text(
                    value,
                    _text_limit(column_meta.get(column, {}).get("type")),
                )
            columns = list(values)
            incoming_is_newer = "VALUES(captured_at) >= captured_at OR captured_at = 0"
            preserve_if_blank = {
                "province", "city", "district", "source_site_id", "source_station_id",
                "requested_name", "matched_station_name", "source_address",
            }
            updates = []
            for column in columns:
                if column in {"source_key", "created_at", "captured_at", "updated_at"}:
                    continue
                incoming = f"VALUES({column})"
                if column in preserve_if_blank:
                    incoming = f"IF({incoming} <> '', {incoming}, {column})"
                updates.append(
                    f"{column} = IF({incoming_is_newer}, {incoming}, {column})"
                )
            updates.extend([
                f"captured_at = IF({incoming_is_newer}, VALUES(captured_at), captured_at)",
                f"updated_at = IF({incoming_is_newer}, VALUES(updated_at), updated_at)",
            ])
            with conn.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) "
                    f"VALUES ({', '.join('%s' for _ in columns)}) "
                    f"ON DUPLICATE KEY UPDATE {', '.join(updates)}",
                    tuple(values[column] for column in columns),
                )
            try:
                self._append_dynamic_snapshot(
                    conn=conn,
                    source_key=source_key,
                    source_station_id=source_station_id,
                    observation_id=str(observation_id or ""),
                    task_id=str(task["id"] or ""),
                    device_id=str(device_id or ""),
                    collection_source=collection_source,
                    matched_station_name=matched_name,
                    payload=payload,
                    captured_at=captured_at or payload.get("collectedAt"),
                    received_at=received_at,
                )
            except Exception as error:
                # The dynamic history table must not break the existing latest-result
                # upsert path. The next successful upload can retry the snapshot.
                print(f"  Dynamic history snapshot skipped: {error}", file=sys.stderr)
            conn.commit()
        finally:
            conn.close()


def unix_timestamp(value: Any) -> int:
    """将外部时间统一转换为秒级 Unix 时间戳；无法解析时返回 0。"""
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000.0
        return max(0, int(seconds))
    text = str(value).strip()
    if text.isdigit():
        return unix_timestamp(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0, int(parsed.timestamp()))
    except (TypeError, ValueError, OSError, OverflowError):
        return 0


def mysql_datetime(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    text = str(value).strip()
    if text.isdigit():
        return mysql_datetime(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return text[:19].replace("T", " ")
