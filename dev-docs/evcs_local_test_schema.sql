CREATE DATABASE IF NOT EXISTS `evcs_local_test`
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

USE `evcs_local_test`;

CREATE TABLE IF NOT EXISTS `heavy_truck_stations` (
  `id` int NOT NULL AUTO_INCREMENT,
  `station_name` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL COMMENT '站点名称',
  `operator` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '运营商',
  `address` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '详细地址',
  `city` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '城市',
  `business_hours` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '营业时间',
  `current_price` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '实时电价',
  `parking_fee` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '停车费',
  `occupancy_fee` varchar(300) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '占位费',
  `longitude` decimal(10,6) DEFAULT NULL COMMENT '经度',
  `latitude` decimal(10,6) DEFAULT NULL COMMENT '纬度',
  `fast_available` varchar(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '快充可用数',
  `fast_total` varchar(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '快充枪数',
  `fast_power` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '快充功率',
  `super_available` varchar(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '超充可用数',
  `super_total` varchar(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '超充枪数',
  `super_power` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '超充功率',
  `slow_available` varchar(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '慢充可用数',
  `slow_total` varchar(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '慢充枪数',
  `slow_power` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '慢充功率',
  `fast_prices` json DEFAULT NULL COMMENT '24h快充价格趋势',
  `slow_prices` json DEFAULT NULL COMMENT '24h慢充价格趋势',
  `facilities` json DEFAULT NULL COMMENT '设施列表',
  `tags` json DEFAULT NULL COMMENT '标签列表',
  `favorite_count` varchar(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '收藏数',
  `collected_at` datetime DEFAULT CURRENT_TIMESTAMP COMMENT '采集时间',
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_city` (`city`) USING BTREE,
  KEY `idx_operator` (`operator`) USING BTREE,
  KEY `idx_location` (`longitude`,`latitude`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ROW_FORMAT=DYNAMIC COMMENT='重卡充电站数据表';

CREATE TABLE IF NOT EXISTS `scan_tasks` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `city` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL COMMENT '城市',
  `district` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL COMMENT '区县',
  `grid_index` int NOT NULL COMMENT '网格编号',
  `center_lng` double DEFAULT NULL COMMENT '网格中心经度',
  `center_lat` double DEFAULT NULL COMMENT '网格中心纬度',
  `min_lng` double DEFAULT NULL COMMENT '网格最小经度',
  `max_lng` double DEFAULT NULL COMMENT '网格最大经度',
  `min_lat` double DEFAULT NULL COMMENT '网格最小纬度',
  `max_lat` double DEFAULT NULL COMMENT '网格最大纬度',
  `status` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT 'pending' COMMENT 'pending/scanning/done/failed',
  `assigned_device` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT NULL COMMENT '分配设备',
  `station_count` int DEFAULT '0' COMMENT '找到的站点数',
  `started_at` timestamp NULL DEFAULT NULL COMMENT '开始时间',
  `completed_at` timestamp NULL DEFAULT NULL COMMENT '完成时间',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE KEY `idx_city_district_grid` (`city`,`district`,`grid_index`) USING BTREE,
  KEY `idx_status` (`status`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ROW_FORMAT=DYNAMIC COMMENT='网格扫描任务表';

CREATE TABLE IF NOT EXISTS `collection_logs` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `station_id` bigint DEFAULT NULL COMMENT '关联站点ID',
  `layer` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT NULL COMMENT '采集层(accessibility/api/ocr/vision)',
  `raw_data` text CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci COMMENT '原始数据',
  `success` tinyint(1) DEFAULT '1' COMMENT '是否成功',
  `error_msg` text CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci COMMENT '错误信息',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ROW_FORMAT=DYNAMIC COMMENT='采集日志表';
