-- site_exploration_charging_station_result
-- 用途：新环境初始化站点附近充电站采集结果表。
-- 说明：本文件仅包含 CREATE TABLE IF NOT EXISTS，不会删除、清空或改写已有数据。
-- 现有数据库如已存在同名表，执行时不会自动变更其结构；结构迁移请另行评估并备份。

CREATE TABLE IF NOT EXISTS `site_exploration_charging_station_result` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    `source_key` CHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '充电站跨采集来源稳定唯一键（优先使用高德POI编号生成）',
    `observation_id` VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '本次采集观测记录唯一标识',
    `task_id` VARCHAR(40) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '采集任务编号',
    `device_id` VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '执行采集的设备编号',
    `collection_source` VARCHAR(32) COLLATE utf8mb4_general_ci NOT NULL DEFAULT 'SITE_EXPLORATION' COMMENT '最近一次结果来源：SITE_EXPLORATION/HENAN_POI',
    `province` VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '省份',
    `city` VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '城市',
    `district` VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '区县',
    `source_site_id` VARCHAR(191) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '周边站点任务来源站点编号；河南POI任务为空',
    `source_station_id` VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '来源数据中的充电站编号，优先保存高德POI编号',
    `requested_name` VARCHAR(512) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '任务请求搜索的站点名称',
    `matched_station_name` VARCHAR(512) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '地图页面匹配到的充电站名称',
    `source_address` VARCHAR(1000) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '来源数据中的站点地址',
    `collected_address` VARCHAR(1000) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '采集页面展示的充电站地址',
    `source_latitude` DECIMAL(12,8) NOT NULL DEFAULT 0.00000000 COMMENT '来源数据纬度，缺失时为 0',
    `source_longitude` DECIMAL(12,8) NOT NULL DEFAULT 0.00000000 COMMENT '来源数据经度，缺失时为 0',
    `operator` VARCHAR(255) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '充电站运营方',
    `business_hours` VARCHAR(255) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '营业时间',
    `current_price` VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '当前充电价格原始文本',
    `parking_fee` VARCHAR(1000) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '停车费说明',
    `fast_available` VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '快充可用数量',
    `fast_total` VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '快充总数量',
    `fast_power` VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '快充功率说明',
    `super_available` VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '超充可用数量',
    `super_total` VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '超充总数量',
    `super_power` VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '超充功率说明',
    `slow_available` VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '慢充可用数量',
    `slow_total` VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '慢充总数量',
    `slow_power` VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '慢充功率说明',
    `result_payload` JSON NOT NULL DEFAULT (JSON_OBJECT()) COMMENT '采集结果原始数据 JSON',
    `captured_at` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '采集发生时间（Unix 时间戳，秒）',
    `received_at` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '服务端接收时间（Unix 时间戳，秒）',
    `created_at` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '创建时间（Unix 时间戳，秒）',
    `updated_at` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '更新时间（Unix 时间戳，秒）',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_site_collection_source` (`source_key`),
    UNIQUE KEY `uk_site_collection_observation` (`observation_id`),
    KEY `idx_site_collection_type_city` (`collection_source`, `city`),
    KEY `idx_site_collection_site` (`source_site_id`),
    KEY `idx_site_collection_station` (`source_station_id`),
    KEY `idx_site_collection_received` (`received_at`)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_general_ci
  COMMENT='统一充电站安卓采集结果表';
