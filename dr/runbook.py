"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import math
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Append one timestamped runbook step."""
    now = time.time()
    record = {
        "ts": now,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "step": n,
        "name": name,
        **kw,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, ensure_ascii=False) + "\n")
    print("RUNBOOK", json.dumps(record, ensure_ascii=False), flush=True)
    return record


def confirm(auto: bool, msg: str) -> bool:
    """Require an explicit operator confirmation unless running in CI mode."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def _latest_outage(primary: str) -> dict | None:
    path = pathlib.Path("chaos/chaos-events.jsonl")
    if not path.exists():
        return None
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            event = json.loads(line)
            if event.get("action") == "kill" and event.get("region") == primary:
                events.append(event)
    return events[-1] if events else None


def _golden_signals(target: str, count: int = 10) -> dict:
    latencies = []
    errors = 0
    with httpx.Client(timeout=3.0) as client:
        for i in range(count):
            started = time.perf_counter()
            try:
                response = client.get(f"{URL[target]}/v1/infer", params={"q": f"dr-check-{i}"})
                body = response.json()
                if response.status_code != 200 or body.get("region") != target:
                    errors += 1
            except (httpx.HTTPError, ValueError):
                errors += 1
            latencies.append((time.perf_counter() - started) * 1000)
    ordered = sorted(latencies)
    p95 = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
    return {
        "requests": count,
        "errors": errors,
        "error_rate": round(errors / count, 3),
        "p95_latency_ms": round(p95, 1),
    }


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Execute the seven-step, semi-automated regional failover runbook."""
    if primary == target or primary not in URL or target not in URL:
        raise ValueError("primary and target must be different known regions")

    started = time.time()
    attempts = []
    for attempt in range(3):
        ready, reason = hc.probe(primary, timeout=2.0)
        attempts.append({"ready": ready, "reason": reason})
        if ready:
            break
        if attempt < 2:
            # Match the operational health-check cadence so a fast connection-refused
            # failure cannot bypass the anti-flap detection window.
            time.sleep(5.0)
    target_ready, target_reason = hc.probe(target, timeout=2.0)
    outage_confirmed = len(attempts) == 3 and all(not item["ready"] for item in attempts)
    step(1, "xac_nhan_outage", ok=outage_confirmed, primary=primary,
         attempts=attempts, target=target, target_ready=target_ready,
         target_reason=target_reason)
    if not outage_confirmed:
        step(7, "post_incident", ok=False, elapsed_s=round(time.time() - started, 2),
             error="outage_not_confirmed")
        return {"ok": False, "error": "outage_not_confirmed", "attempts": attempts}

    outage = _latest_outage(primary)
    outage_ts = outage.get("ts") if outage else None
    incident = step(
        2,
        "thong_bao_incident",
        ok=True,
        primary=primary,
        outage_ts=outage_ts,
        outage_iso=outage.get("iso") if outage else None,
        notification_delay_s=(round(time.time() - outage_ts, 2) if outage_ts else None),
    )
    if not confirm(auto, f"Đã xác nhận region-{primary} outage. Fail over sang region-{target}?"):
        step(7, "post_incident", ok=False, elapsed_s=round(time.time() - started, 2),
             error="operator_declined")
        return {"ok": False, "error": "operator_declined", "incident": incident}

    failover_result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", ok=failover_result.get("ok", False), target=target,
         failover_called_once=True, waited_s=failover_result.get("waited_s"),
         error=failover_result.get("error"))

    restore = failover_result.get("restore") or {}
    target_state = failover_result.get("target_state") or failover_result.get("ready_state") or {}
    step(4, "verify_state_replica", ok=failover_result.get("ok", False), target=target,
         vector_count=(target_state.get("vectors") or {}).get("count"),
         weights=bool(restore.get("embed_model_version")),
         embed_model_version=restore.get("embed_model_version"),
         rpo_seconds=restore.get("rpo_seconds"), docs_lost=restore.get("docs_lost"))

    cutover_ok = failover_result.get("ok") and failover_result.get("active_region") == target
    step(5, "dns_cutover", ok=bool(cutover_ok), target=target,
         active_region=failover_result.get("active_region"),
         error=failover_result.get("error"))

    if cutover_ok:
        signals = _golden_signals(target)
        signals_ok = signals["errors"] == 0
        step(6, "verify_golden_signals", ok=signals_ok, target=target, **signals)
    else:
        signals = {"requests": 0, "errors": 0, "error_rate": None, "p95_latency_ms": None}
        signals_ok = False
        step(6, "verify_golden_signals", ok=False, target=target, skipped=True,
             reason="cutover_failed", **signals)

    ok = bool(cutover_ok and signals_ok)
    summary = step(
        7,
        "post_incident",
        ok=ok,
        elapsed_s=round(time.time() - started, 2),
        measure_command=(
            "python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
            "--target-rto 300"
        ),
    )
    return {
        "ok": ok,
        "primary": primary,
        "target": target,
        "incident": incident,
        "failover": failover_result,
        "golden_signals": signals,
        "summary": summary,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
