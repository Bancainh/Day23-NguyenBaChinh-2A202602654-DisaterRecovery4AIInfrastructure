# Postmortem — DR Drill Lab 23

Ngày drill: 2026-10-09. Đây là phân tích blameless về hệ thống và quy trình.

## 1. Timeline

| ISO time (UTC) | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T04:37:11Z | Region A outage bắt đầu; Region B vẫn alive | `chaos/chaos-events.jsonl:2` |
| 2026-10-09T04:37:13Z | Request đầu tiên sau outage thất bại | `reports/drill-2-withdr.jsonl:23` |
| 2026-10-09T04:37:27Z | Health checker đánh dấu A `UNHEALTHY` sau 3 lỗi liên tiếp | `reports/health-events.jsonl:2` |
| 2026-10-09T04:37:29Z | Operator confirm incident/cutover; notification delay 17.8s | `reports/runbook-run.jsonl:2` |
| 2026-10-09T04:37:36Z | Request đầu tiên thành công từ Region B | `reports/drill-2-withdr.jsonl:33` |

## 2. RTO/RPO so với mục tiêu và gap analysis

- RTO mục tiêu: 300s; đo được: **25.1s**; gap/margin: **274.9s dưới mục tiêu**.
- RPO mục tiêu: 300s; đo được: **6.0s (3 document mất)**; gap/margin: **294.0s dưới mục tiêu**.
- Bước tốn nhiều thời gian nhất là health-check detection: floor 15.0s, thực tế
  15.9s, tương đương 63.3% RTO. GPU warm-up đứng thứ hai với 6.72s.

## 3. Root cause — 5 whys

1. Vì sao user thấy lỗi? Edge vẫn cache và route request tới Region A sau khi A ngừng đáp ứng.
2. Vì sao edge chưa chuyển ngay? Cutover chỉ được phép sau khi health checker chống flap xác nhận ba lỗi liên tiếp.
3. Vì sao mất 15.9s để cảnh báo? Cấu hình là interval 5s × threshold 3, cộng thời điểm outage nằm giữa hai chu kỳ probe.
4. Vì sao B chưa thể nhận traffic ngay lúc outage? B ban đầu `warm`, thiếu weights và vector DB; cần restore snapshot rồi warm GPU pool.
5. Vì sao vẫn mất 3 document? Replication là snapshot theo chu kỳ 30s, nên các write sau snapshot gần nhất chưa có ở bản restore.

Điểm quy trình dễ thất bại trong outage thật là việc chạy runbook vẫn cần operator xác
nhận. Nếu alert không đánh thức on-call hoặc snapshot replication đã im lặng dừng, RTO
hoặc RPO có thể vượt SLO dù mã failover hoạt động đúng.

## 4. Action items

| # | Action | Owner | Deadline | Tác động dự kiến |
|---|---|---|---|---|
| 1 | Thêm alert khi replication lag > 30s hoặc snapshot thiếu model version | ML Platform | 2026-10-16 | Chặn RPO vượt 30s; tránh restore index/model không tương thích. |
| 2 | Thử interval 2s, threshold 3 trong 5 chaos runs và theo dõi false positive | SRE | 2026-10-23 | Detection floor từ 15s xuống 6s, RTO giảm tối đa 9s nếu không phát sinh flap. |

## 5. Ba câu hỏi bắt buộc

1. `interval × threshold = 5s × 3 = 15s`. Floor này chiếm 59.8% RTO 25.1s;
   thời gian phát hiện thực tế 15.9s chiếm 63.3%.
2. Nếu interval xuống 1s và vẫn giữ threshold 3, floor còn 3s nên RTO lý thuyết giảm
   khoảng 12s, còn khoảng 13.1s. Đổi lại số probe tăng 5 lần và ba lỗi thoáng qua
   liên tiếp dễ kích hoạt failover/flapping hơn; cần circuit breaker và quan sát false positive.
3. `docs_lost=3` là ba ticket đã ghi vào primary sau snapshot nhưng không tồn tại ở B,
   nên khách hàng có thể không tìm thấy chúng sau failover. Nó chỉ mô tả drill này,
   không bảo đảm cho outage 6 giờ: nếu replication cũng dừng suốt 6 giờ ở tốc độ
   0.5 doc/s thì có thể mất khoảng 10,800 document; vì vậy phải alert replication lag.

Kết quả máy đọc từ các log xác nhận `valid:true`, warnings rỗng, Region B phục vụ
recovery tại `reports/drill-2-withdr.jsonl:33`; RTO PASS và RPO có cả giây lẫn số document.
