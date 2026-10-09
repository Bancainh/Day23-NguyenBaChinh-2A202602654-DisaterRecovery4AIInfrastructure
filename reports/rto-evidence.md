# RTO/RPO Evidence — Lab 23

Các số dưới đây được tính từ drill chạy ngày 2026-10-09 và truy ngược trực tiếp tới
các log JSONL được giữ lại cùng bài nộp.

## 1. Drill 1 — không có DR

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T04:35:18Z` | chaos kill Region A | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên sau t_outage | `+2.2s` | dòng `ok:false` đầu tiên có timestamp sau t_outage | `reports/drill-1-nodr.jsonl:18` |
| Request thành công sau đó | Không có | toàn bộ 13 request sau outage đều lỗi; dòng cuối vẫn lỗi | `reports/drill-1-nodr.jsonl:30` |
| RTO | `NO_RECOVERY` | không có request phục hồi trước khi loadgen kết thúc | `reports/drill-1-nodr.jsonl:30` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---:|---|---|
| t_outage (mốc 0) | 0.0s | `action:kill`, Region A | `chaos/chaos-events.jsonl:2` |
| User thấy lỗi đầu tiên | 2.2s | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-2-withdr.jsonl:23` |
| Health check phát hiện | 15.9s | A chuyển sang `UNHEALTHY` sau 3 lỗi liên tiếp | `reports/health-events.jsonl:2` |
| Snapshot restore xong | 18.1s | `2_restore_snapshot`, RPO 6.0s / 3 doc | `reports/failover-events.jsonl:2` |
| Region B ready | 24.8s | `4_wait_ready`, 215 vector | `reports/failover-events.jsonl:4` |
| DNS cutover | 24.8s | `5_dns_cutover`, active Region B | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **25.1s** | request `ok:true` đầu tiên sau lỗi, do B phục vụ | `reports/drill-2-withdr.jsonl:33` |

| Chỉ số | Đo được | Mục tiêu | Verdict |
|---|---:|---:|---|
| RTO — Inference API | `25.1s` | 300s | **PASS**, dư 274.9s |
| RPO — Vector DB | `6.0s` / `3` doc | 300s | **PASS**, dư 294.0s |

## 3. Phân rã RTO

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---:|---|---|
| Health-check detection | 15.9s (floor 15.0s) | `interval_s:5.0 × threshold:3`; event thực tế ở `reports/health-events.jsonl:2` | Giảm interval, nhưng tăng tải probe và nguy cơ phản ứng với lỗi ngắn hạn. |
| Xác nhận target + snapshot restore | 2.1s | từ health alert đến `2_restore_snapshot` ở `reports/failover-events.jsonl:2` | Alert-to-runbook tự động hơn và snapshot nhỏ/gần target hơn. |
| GPU pool warm-up | 6.7s | `waited_s:6.72` ở `reports/failover-events.jsonl:4` | Giữ warm capacity hoặc pre-warm pool. |
| DNS/LB TTL cache | 0.4s | cutover ở `reports/failover-events.jsonl:5` đến recovery ở `reports/drill-2-withdr.jsonl:33` | TTL thấp hơn hoặc global LB health routing. |
| **Tổng** | **25.1s** | khớp request recovery đầu tiên | `reports/drill-2-withdr.jsonl:33` |

RPO được đo tại restore, không suy đoán: snapshot chứa dữ liệu tới timestamp
`1791520642.7320342`, trong khi primary tới `1791520648.73631`; chênh 6.0s và 3
document, ghi tại `reports/failover-events.jsonl:2`.
