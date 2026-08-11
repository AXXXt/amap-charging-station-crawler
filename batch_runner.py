import argparse
import json
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

from crawler import ADB_PATH, AmapCrawler
from task_queue import StationTaskQueue, utc_now_iso


PRINT_LOCK = threading.Lock()


def log(message, log_path=None):
    line = f"[{utc_now_iso()}] {message}"
    with PRINT_LOCK:
        print(line, flush=True)
        if log_path:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")


def evaluate_detail(result):
    if not result:
        return {"score": 0, "detailed": False, "missing": ["result"]}

    equipment_present = any(
        result.get(field)
        for field in (
            "fast_available", "fast_total", "fast_power",
            "super_available", "super_total", "super_power",
            "slow_available", "slow_total", "slow_power",
        )
    )
    price_schedule_present = bool(result.get("fast_prices") or result.get("slow_prices"))
    operational_signals = {
        "business_hours": bool(result.get("business_hours")),
        "operator": bool(result.get("operator")),
        "current_price": bool(result.get("current_price")),
        "parking": bool(result.get("parking_fee") or result.get("occupancy_fee")),
        "equipment": equipment_present,
        "price_schedule": price_schedule_present,
        "facilities": bool(result.get("facilities")),
    }
    score = 0
    score += 25 if result.get("detail_verified") else 0
    score += 15 if result.get("station_name") else 0
    score += 15 if result.get("address") else 0
    score += 10 if operational_signals["business_hours"] else 0
    score += 10 if operational_signals["operator"] else 0
    score += 10 if operational_signals["current_price"] else 0
    score += 7 if operational_signals["equipment"] else 0
    score += 4 if operational_signals["price_schedule"] else 0
    score += 2 if operational_signals["parking"] else 0
    score += 2 if operational_signals["facilities"] else 0
    score = min(100, score)

    station_name = result.get("station_name", "")
    charging_name = any(
        marker in station_name
        for marker in ("充电", "充换电", "超充", "快充", "重卡站")
    )
    signal_count = sum(operational_signals.values())
    detailed = bool(
        result.get("detail_verified")
        and station_name
        and charging_name
        and result.get("address")
        and signal_count >= 2
    )
    missing = []
    if not result.get("detail_verified"):
        missing.append("detail_verified")
    if not station_name:
        missing.append("station_name")
    elif not charging_name:
        missing.append("charging_station_name")
    if not result.get("address"):
        missing.append("address")
    if signal_count < 2:
        missing.append("operational_fields")
    return {
        "score": score,
        "detailed": detailed,
        "missing": missing,
        "signals": operational_signals,
    }


def parse_adb_devices(output):
    devices = []
    for line in output.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2 or parts[1] != "device":
            continue
        serial = parts[0]
        metadata = {}
        for part in parts[2:]:
            if ":" in part:
                key, value = part.split(":", 1)
                metadata[key] = value
        devices.append({"serial": serial, "metadata": metadata})
    return devices


def discover_devices(adb_path=ADB_PATH):
    completed = subprocess.run(
        [adb_path, "devices", "-l"],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    return parse_adb_devices(completed.stdout)


class WorkBudget:
    def __init__(self, maximum=None):
        self.remaining = maximum if maximum and maximum > 0 else None
        self.lock = threading.Lock()

    def reserve(self):
        with self.lock:
            if self.remaining is None:
                return True
            if self.remaining <= 0:
                return False
            self.remaining -= 1
            return True

    def refund(self):
        with self.lock:
            if self.remaining is not None:
                self.remaining += 1


class LeaseHeartbeat(threading.Thread):
    def __init__(self, queue, task, worker_id, device_serial, lease_seconds):
        super().__init__(daemon=True)
        self.queue = queue
        self.task = task
        self.worker_id = worker_id
        self.device_serial = device_serial
        self.lease_seconds = lease_seconds
        self.done = threading.Event()
        self.lease_lost = threading.Event()

    def run(self):
        interval = max(5, min(30, self.lease_seconds // 3))
        while not self.done.wait(interval):
            if not self.queue.heartbeat(
                self.task,
                self.worker_id,
                self.device_serial,
                self.lease_seconds,
            ):
                self.lease_lost.set()
                return

    def close(self):
        self.done.set()
        self.join(timeout=5)


def build_search_query(payload):
    name = (payload.get("name") or "").strip()
    address = (payload.get("address") or "").strip()
    if not address:
        return name
    compact_name = re.sub(r"[\s（）()·•\-_]+", "", name)
    if len(compact_name) <= 10 or name in {"重卡充电站", "汽车充电站"}:
        return f"{name} {address}"
    return name


def save_failure_artifacts(crawler, failure_dir, task, error):
    output_dir = Path(failure_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", task.station_id)
    prefix = output_dir / f"{safe_id}_attempt{task.attempt_no}"
    try:
        xml_text = crawler.d.dump_hierarchy()
        prefix.with_suffix(".xml").write_text(xml_text, encoding="utf-8")
    except Exception:
        pass
    try:
        crawler.d.screenshot(str(prefix.with_suffix(".png")))
    except Exception:
        pass
    prefix.with_suffix(".json").write_text(
        json.dumps(
            {
                "station_id": task.station_id,
                "attempt": task.attempt_no,
                "error": str(error),
                "payload": task.payload,
                "captured_at": utc_now_iso(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


class DeviceWorker(threading.Thread):
    def __init__(
        self,
        queue,
        device,
        stop_event,
        budget,
        lease_seconds=180,
        visual_checker_factory=None,
        watch=False,
        log_path=None,
        failure_dir="debug_runs/batch_failures",
    ):
        super().__init__(name=f"device-{device['serial']}")
        self.queue = queue
        self.device = device
        self.device_serial = device["serial"]
        self.stop_event = stop_event
        self.budget = budget
        self.lease_seconds = lease_seconds
        self.visual_checker_factory = visual_checker_factory
        self.watch = watch
        self.log_path = log_path
        self.failure_dir = failure_dir
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{self.device_serial}"

    def _collect(self, crawler, task):
        payload = dict(task.payload)
        city = payload.get("city", "")
        if task.attempt_no < task.max_attempts:
            strategy = "poi_id"
            result = crawler.collect_detail(payload, city)
        else:
            strategy = "exact_search"
            query = build_search_query(payload)
            stations = crawler.search_stations(city, query=query)
            if not stations:
                return strategy, None
            search_payload = dict(payload)
            search_payload.pop("id", None)
            search_payload["search_query"] = query
            result = crawler.collect_detail(search_payload, city)
        return strategy, result

    def run(self):
        self.queue.register_device(
            self.device_serial,
            self.worker_id,
            self.device.get("metadata"),
        )
        try:
            checker = None
            if self.visual_checker_factory:
                checker = self.visual_checker_factory(self.device_serial)
            crawler = AmapCrawler(
                serial=self.device_serial,
                visual_checker=checker,
                stop_event=self.stop_event,
            )
        except Exception as error:
            log(f"{self.device_serial} 初始化失败: {error}", self.log_path)
            self.queue.mark_device_offline(self.device_serial, self.worker_id)
            return

        log(f"{self.device_serial} worker ready", self.log_path)
        try:
            while not self.stop_event.is_set():
                if not self.budget.reserve():
                    break
                task = self.queue.claim(
                    self.worker_id,
                    self.device_serial,
                    self.lease_seconds,
                )
                if task is None:
                    self.budget.refund()
                    stats = self.queue.stats()
                    if not self.watch and stats["pending"] == 0:
                        break
                    self.queue.heartbeat_device(
                        self.device_serial, self.worker_id, "idle", None
                    )
                    time.sleep(2)
                    continue

                log(
                    f"{self.device_serial} claim {task.station_id} "
                    f"attempt={task.attempt_no}/{task.max_attempts} "
                    f"priority={task.priority}",
                    self.log_path,
                )
                heartbeat = LeaseHeartbeat(
                    self.queue,
                    task,
                    self.worker_id,
                    self.device_serial,
                    self.lease_seconds,
                )
                heartbeat.start()
                try:
                    strategy, result = self._collect(crawler, task)
                    self.queue.set_attempt_strategy(task, strategy)
                    if self.stop_event.is_set():
                        self.queue.release(task, "batch stopped", refund_attempt=True)
                        break
                    if heartbeat.lease_lost.is_set():
                        log(f"{task.station_id} lease lost; result discarded", self.log_path)
                        continue

                    assessment = evaluate_detail(result)
                    if result and (assessment["detailed"] or task.attempt_no >= task.max_attempts):
                        self.queue.complete(
                            task,
                            result,
                            assessment["score"],
                            assessment["detailed"],
                        )
                        log(
                            f"{task.station_id} complete detailed={assessment['detailed']} "
                            f"score={assessment['score']}",
                            self.log_path,
                        )
                    else:
                        reason = "no detail result" if not result else (
                            "partial detail: "
                            + (",".join(assessment["missing"]) or "detail threshold")
                        )
                        retry_delay = min(60, 5 * (2 ** max(0, task.attempt_no - 1)))
                        self.queue.fail(
                            task,
                            reason,
                            retryable=True,
                            retry_delay=retry_delay,
                            result=result,
                            completeness_score=assessment["score"],
                        )
                        save_failure_artifacts(crawler, self.failure_dir, task, reason)
                        current = self.queue.get_task(task.station_id)
                        action = "retry" if current and current["status"] == "pending" else "failed"
                        log(f"{task.station_id} {action}: {reason}", self.log_path)
                except Exception as error:
                    if self.stop_event.is_set():
                        self.queue.release(task, str(error), refund_attempt=True)
                        break
                    retry_delay = min(60, 5 * (2 ** max(0, task.attempt_no - 1)))
                    self.queue.fail(task, error, retryable=True, retry_delay=retry_delay)
                    save_failure_artifacts(crawler, self.failure_dir, task, error)
                    log(f"{task.station_id} error: {error}", self.log_path)
                finally:
                    heartbeat.close()
                    self.queue.heartbeat_device(
                        self.device_serial, self.worker_id, "idle", None
                    )
        finally:
            self.queue.mark_device_offline(self.device_serial, self.worker_id)
            log(f"{self.device_serial} worker stopped", self.log_path)


def write_snapshot(queue, output_path):
    payload = {
        "generated_at": utc_now_iso(),
        "stats": queue.stats(),
        "tasks": list(queue.iter_task_rows()),
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def select_devices(discovered, requested):
    if requested == "all":
        return discovered
    requested_serials = {
        serial.strip() for serial in requested.split(",") if serial.strip()
    }
    return [device for device in discovered if device["serial"] in requested_serials]


def run_command(args):
    queue = StationTaskQueue(args.db)
    if not args.no_import:
        imported = queue.import_csv(
            args.csv,
            base_priority=args.priority,
            max_attempts=args.max_attempts,
        )
        log(f"imported {json.dumps(imported, ensure_ascii=False)}", args.log)

    discovered = discover_devices(args.adb_path)
    devices = select_devices(discovered, args.devices)
    if not devices:
        raise RuntimeError("没有找到符合条件的在线 ADB 设备")

    visual_checker_factory = None
    if args.visual:
        from visual_check import create_qianwen_checker

        visual_checker_factory = create_qianwen_checker

    stop_event = threading.Event()
    budget = WorkBudget(args.max_tasks)
    workers = [
        DeviceWorker(
            queue=queue,
            device=device,
            stop_event=stop_event,
            budget=budget,
            lease_seconds=args.lease_seconds,
            visual_checker_factory=visual_checker_factory,
            watch=args.watch,
            log_path=args.log,
            failure_dir=args.failure_dir,
        )
        for device in devices
    ]
    log(
        f"starting {len(workers)} worker(s): "
        + ", ".join(device["serial"] for device in devices),
        args.log,
    )
    for worker in workers:
        worker.start()

    try:
        for worker in workers:
            worker.join()
    except KeyboardInterrupt:
        log("interrupt received; stopping workers", args.log)
        stop_event.set()
        for worker in workers:
            worker.join(timeout=60)

    stats = queue.stats()
    write_snapshot(queue, args.snapshot)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def status_command(args):
    queue = StationTaskQueue(args.db)
    print(json.dumps(queue.stats(), ensure_ascii=False, indent=2))
    if args.snapshot:
        write_snapshot(queue, args.snapshot)
    return 0


def enqueue_command(args):
    queue = StationTaskQueue(args.db)
    payload = {
        "id": args.station_id,
        "name": args.name,
        "address": args.address,
        "latitude": args.latitude,
        "longitude": args.longitude,
    }
    station_id = queue.enqueue_user_task(
        payload,
        priority=args.priority,
        max_attempts=args.max_attempts,
    )
    print(json.dumps({"station_id": station_id, "status": "queued"}, ensure_ascii=False))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="多设备高德充电站采集调度器")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="导入任务并启动设备 worker")
    run_parser.add_argument("--db", default="data/station_tasks.db")
    run_parser.add_argument("--csv", default="cleaned_stations.csv")
    run_parser.add_argument("--no-import", action="store_true")
    run_parser.add_argument("--devices", default="all", help="all 或逗号分隔的设备序列号")
    run_parser.add_argument("--adb-path", default=ADB_PATH)
    run_parser.add_argument("--priority", type=int, default=100)
    run_parser.add_argument("--max-attempts", type=int, default=3)
    run_parser.add_argument("--lease-seconds", type=int, default=180)
    run_parser.add_argument("--max-tasks", type=int, default=0, help="0 表示不限")
    run_parser.add_argument("--watch", action="store_true", help="无任务时保持在线等待")
    run_parser.add_argument("--visual", action="store_true")
    run_parser.add_argument("--log", default="logs/batch_runner.log")
    run_parser.add_argument("--failure-dir", default="debug_runs/batch_failures")
    run_parser.add_argument("--snapshot", default="data/station_tasks_snapshot.json")
    run_parser.set_defaults(func=run_command)

    status_parser = subparsers.add_parser("status", help="查看队列状态")
    status_parser.add_argument("--db", default="data/station_tasks.db")
    status_parser.add_argument("--snapshot")
    status_parser.set_defaults(func=status_command)

    enqueue_parser = subparsers.add_parser("enqueue", help="提交用户高优先级站点任务")
    enqueue_parser.add_argument("--db", default="data/station_tasks.db")
    enqueue_parser.add_argument("--id", dest="station_id", default="")
    enqueue_parser.add_argument("--name", required=True)
    enqueue_parser.add_argument("--address", default="")
    enqueue_parser.add_argument("--latitude", default="")
    enqueue_parser.add_argument("--longitude", default="")
    enqueue_parser.add_argument("--priority", type=int, default=1000)
    enqueue_parser.add_argument("--max-attempts", type=int, default=3)
    enqueue_parser.set_defaults(func=enqueue_command)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
