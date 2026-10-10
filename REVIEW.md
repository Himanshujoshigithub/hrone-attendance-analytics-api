# Part A — Starter code review

Review findings from the original starter; validate these against the exact starter copy in the candidate kit before submission.

| Area | Defect and observable failure | Fix applied / required |
|---|---|---|
| `compute_late_minutes` | Compared wall-clock fields without reliably handling IST/UTC conversion, overnight shifts, and the strict 10-minute grace boundary. | Convert the instant to IST, resolve the shift start date (including overnight shifts), and apply `elapsed_seconds > 600`, then floor whole minutes. |
| `compute_work_hours` | Built-in `round()` uses banker's rounding at exact ties. | Use `Decimal` and `ROUND_HALF_UP` on duration seconds divided by 3600. |
| `compute_overtime` | Treating shift end as the same calendar date breaks overnight shifts; overtime must start at 30 whole minutes. | Resolve the overnight shift end on the next date and floor elapsed minutes; return zero below 30 minutes. |
| Timestamp parsing | `if punched_at` treats 0 as omitted and naive `datetime.fromtimestamp` depends on host timezone. | Distinguish `None` from supplied values, validate epoch milliseconds, convert from UTC, truncate to whole seconds. |
| Employee creation | A pre-insert duplicate check is vulnerable to simultaneous requests; server creation time may be naive. | Enforce a unique database index and translate `DuplicateKeyError` to 409; store UTC time. |
| Employee shift validation | Equal start/end times are ambiguous under the contract. | Reject equal shift times with 422 validation. |
| Employee pagination | `skip = page * page_size` skips the first page. | Use `(page - 1) * page_size`. |
| Employee count | Counting all employees ignores the optional department filter. | Count with the same filter used for the list. |
| Employee list order | No deterministic sort means page membership can vary. | Sort by `emp_code` ascending. |
| Punch-in employee lookup | Missing employee may proceed and fail later or create an orphan log. | Return 404 before inserting. |
| Punch-in concurrency | Check-then-insert allows duplicate attendance for simultaneous calls. | Enforce unique `(emp_code, date)` index and map duplicate-key errors to 409. |
| Attendance list | Loading all matching records and sorting/slicing in Python scales poorly at 100k+ rows. | Apply filter, sort, skip and limit in MongoDB; use `count_documents` for filtered total. |
| Attendance response | Returning Mongo `_id` violates the natural-key API contract. | Serialize only contract fields; never return `_id` or an attendance ID. |
| Health check | Always returning `ok` can make an unavailable database look ready. | Run MongoDB `ping`; return 503 on database errors. |
| Timestamp serialization | Raw BSON datetimes are not epoch milliseconds and may leak timezone ambiguity. | Convert all response instants and history timestamp changes to epoch milliseconds. |
| Seeded optional fields | Seed records may omit `history` or `half_day`. | Serialize defaults as `[]` and `false`. |
| Patch concurrency | A read-modify-write can overwrite another correction's audit history. | Use optimistic compare-and-set on the prior history value; return 409 if another edit wins. |

## Also reviewed
- `emp_code` is the natural business key; Mongo `_id` is internal only.
- Attendance dates are inclusive ISO date strings, and API instants use epoch milliseconds.
- Analytics should use persisted derived values instead of silently recalculating historical logs.
