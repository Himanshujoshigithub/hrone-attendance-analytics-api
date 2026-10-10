# Part C — Design decisions

## 1. Indexes
I create a unique `employees.emp_code` index so duplicate employee codes are rejected by MongoDB even under concurrent inserts. A compound `employees(department, joined_on)` index supports exact department filters and joined-date eligibility. Attendance uses a unique `(emp_code, date)` index to guarantee one record per employee per attendance date, plus `(date DESC, emp_code ASC)` for the required attendance-list order and `(date, status)` for date/status filtering. I considered indexing every analytics field individually, but avoided doing so without evidence because every extra index increases write and storage cost. Explain plans should be checked against the actual MongoDB version and the large evaluation dataset.

## 2. Two identical punch-ins at the same instant
Both requests may reach the insert path, but the unique `(emp_code, date)` index is the source of truth. Exactly one insert succeeds with 201. The other receives MongoDB `DuplicateKeyError`, which the API converts to 409. Correctness does not depend on a preliminary existence check.

## 3. Ties at the leaderboard limit
I use MongoDB `$rank` over total late minutes, then filter for `rank <= limit`. If the cutoff rank is shared by multiple employees, all tied employees at that rank are returned—even if the number of rows exceeds `limit`. This implements standard competition ranking (1, 2, 2, 4), unlike slicing an already sorted array.

## 4. Employees with no attendance logs
The department summary starts from eligible employee documents (`joined_on` on or before the month's final day), then looks up that employee's logs. The lookup is unwound with `preserveNullAndEmptyArrays`, so employees without logs still contribute to headcount. Employees joining after the period end are filtered out before the lookup. Department attribution comes from the employee record.

## 5. If serving 100x data
I would inspect real execution stats and workload patterns, then partition/archive historical attendance if retention and query patterns justify it. I would also add workload-specific compound indexes only after validating selectivity and write cost, cap broad date ranges, and run load tests with production-like distributions. I would not introduce caching until correctness and invalidation rules for corrections were defined.
