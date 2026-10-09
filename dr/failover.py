"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append one timestamped failover event and mirror it to stdout."""
    now = time.time()
    record = {
        "ts": now,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        **kw,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, ensure_ascii=False) + "\n")
    print("FAILOVER", json.dumps(record, ensure_ascii=False), flush=True)
    return record


def state_of(region: str, timeout: float = 2.0) -> dict:
    """Read the serving state of a region without treating readiness as liveness."""
    response = httpx.get(f"{URL[region]}/v1/state", timeout=timeout)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore and warm ``target`` before atomically moving the edge pointer."""
    if target not in URL:
        raise ValueError(f"unknown target region: {target}")
    if backend not in {"fs", "minio"}:
        raise ValueError(f"unknown snapshot backend: {backend}")
    if wait < 0:
        raise ValueError("wait must be >= 0")

    result = {"ok": False, "target": target, "backend": backend}
    source = "b" if target == "a" else "a"

    try:
        before = state_of(target)
        result["before"] = before
        emit(step="1_verify_target", target=target, ok=True,
             pool_state=before.get("pool_state"), weights=before.get("weights"),
             vector_count=before.get("count"))
    except Exception as exc:
        result["error"] = f"verify_target:{type(exc).__name__}:{exc}"
        emit(step="1_verify_target", target=target, ok=False, error=result["error"])
        return result

    try:
        restored = snapshot.get(target, backend)
        source = restored.get("source_region", source)
        rpo = snapshot.rpo(
            pathlib.Path(f"state/region-{source}/vectors.sqlite"),
            pathlib.Path(f"state/region-{target}/vectors.sqlite"),
        )
        restore_result = {**restored, **rpo}
        result["restore"] = restore_result
        emit(step="2_restore_snapshot", target=target, backend=backend, ok=True,
             snapshot_at=restored.get("snapshot_at"),
             restored_at=restored.get("restored_at"),
             rpo_seconds=rpo.get("rpo_seconds"), docs_lost=rpo.get("docs_lost"),
             embed_model_version=restored.get("embed_model_version"))
    except (Exception, SystemExit) as exc:
        result["error"] = f"restore_snapshot:{type(exc).__name__}:{exc}"
        emit(step="2_restore_snapshot", target=target, backend=backend,
             ok=False, error=result["error"])
        return result

    pool_file = pathlib.Path(f"state/region-{target}/pool_state")
    try:
        pool_file.parent.mkdir(parents=True, exist_ok=True)
        pool_file.write_text("full", encoding="utf-8")
        emit(step="3_scale_pool", target=target, ok=True, pool_state="full")
    except OSError as exc:
        result["error"] = f"scale_pool:{type(exc).__name__}:{exc}"
        emit(step="3_scale_pool", target=target, ok=False, error=result["error"])
        return result

    started = time.monotonic()
    deadline = started + wait
    last_reason = "not_probed"
    ready_state = None
    while True:
        try:
            response = httpx.get(f"{URL[target]}/readyz", timeout=min(2.0, max(wait, 0.1)))
            try:
                ready_state = response.json()
            except ValueError:
                ready_state = {"status_code": response.status_code}
            if response.status_code == 200 and ready_state.get("ready", True):
                waited = round(time.monotonic() - started, 2)
                result["ready_state"] = ready_state
                result["waited_s"] = waited
                emit(step="4_wait_ready", target=target, ok=True, waited_s=waited,
                     pool_state=ready_state.get("pool_state"),
                     vector_count=(ready_state.get("vectors") or {}).get("count"))
                break
            last_reason = ",".join(ready_state.get("reasons", [])) or f"http_{response.status_code}"
        except httpx.HTTPError as exc:
            last_reason = f"{type(exc).__name__}:{exc}"

        if time.monotonic() >= deadline:
            waited = round(time.monotonic() - started, 2)
            result["waited_s"] = waited
            result["error"] = f"wait_ready_timeout:{last_reason}"
            emit(step="4_wait_ready", target=target, ok=False, waited_s=waited,
                 reason=last_reason)
            return result
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

    active_file = pathlib.Path("edge/active_region")
    try:
        active_file.parent.mkdir(parents=True, exist_ok=True)
        active_file.write_text(target, encoding="utf-8")
        result.update(ok=True, active_region=target, target_state=ready_state)
        emit(step="5_dns_cutover", target=target, ok=True, active_region=target)
        return result
    except OSError as exc:
        result["error"] = f"dns_cutover:{type(exc).__name__}:{exc}"
        emit(step="5_dns_cutover", target=target, ok=False, error=result["error"])
        return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
