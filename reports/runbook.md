# Runbook — Region chính down

Phạm vi: active/passive local, Region A là primary, Region B là target. SLO: RTO ≤ 300s,
RPO ≤ 300s. Incident Commander (IC) có quyền quyết định cutover và rollback; on-call
thực thi, ML Platform xác nhận model/vector state.

| # | Bước | Lệnh copy-paste | Biết là xong khi | Owner |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python chaos/kill_region.py status` | A không phản hồi ở 3 lần probe liên tiếp; B còn `alive: true`. Không tiếp tục nếu B cũng down. | On-call |
| 2 | Mở incident và bắt đầu đồng hồ | `python dr/runbook.py --primary a --target b --backend fs` | Có dòng `thong_bao_incident`, sau đó operator nhập `y`; timestamp xuất hiện trong `reports/runbook-run.jsonl`. | IC + on-call |
| 3 | Restore state ở B | `Select-String -Path reports/failover-events.jsonl -Pattern '2_restore_snapshot'` | Event có `ok:true`, `rpo_seconds`, `docs_lost` và `embed_model_version`. | ML Platform |
| 4 | Scale và chờ B ready | `Invoke-RestMethod http://127.0.0.1:8002/readyz` | HTTP 200, `ready:true`, `pool_state:full`, vector count > 0 và không còn lý do lỗi. | ML Platform |
| 5 | DNS/LB cutover | `Invoke-RestMethod http://127.0.0.1:8080/edge/state` | `active_region` là `b`; event `5_dns_cutover` chỉ xuất hiện sau `4_wait_ready`. | On-call |
| 6 | Verify golden signals | `Select-String -Path reports/runbook-run.jsonl -Pattern 'verify_golden_signals'` | 10 request thật, `errors:0`, `error_rate:0.0`; drill này có p95 26.3ms. | SRE |
| 7 | Đo RTO/RPO và mở postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid:true`, warnings rỗng, `rto_verdict:"PASS"`; gắn kết quả vào incident. | IC |

Nếu bất kỳ bước restore/warm-up nào lỗi, giữ `edge/active_region` ở A, không chạy
cutover tay, và escalation cho IC. Nếu B trả lỗi sau cutover, IC có thể đặt edge về A
chỉ khi A đã `/readyz` 200 ba lần liên tiếp và dữ liệu đã được đối soát.

## Rollback về Region A

Điều kiện: nguyên nhân outage đã được khắc phục; A ready ba lần liên tiếp; dữ liệu phát
sinh ở B đã được snapshot; error rate của A trong 10 request kiểm tra là 0%. Chỉ IC được
phê duyệt rollback. Thực hiện:

```powershell
python state/snapshot.py put --region b --backend fs
python dr/failover.py --target a --backend fs
Invoke-RestMethod http://127.0.0.1:8080/edge/state
```

Nếu A chưa chạy, trước tiên dùng `python chaos/kill_region.py restore --region a
--backend bare`; nếu trả `need_manual_start`, khởi động lại service A theo Step 0 của
`GUIDE.md`. Dừng rollback nếu A không ready, RPO không tính được, hoặc golden signals
có lỗi; giữ traffic ở B và cập nhật incident.
