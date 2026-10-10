# DECISIONS.md

1. **Indexes.**
   - `employees`: Unique index on `emp_code` (guarantees `emp_code` uniqueness, serves `create_employee` and point lookups).
   - `attendance_logs`: Unique index on `(emp_code, date)` (prevents duplicate punch-ins per day and serves point lookups).
   - `attendance_logs`: Index on `(date, emp_code)` (serves `list_attendance` for efficient sorting and filtering by date).
   - `employees`: Index on `department` (rejected a compound index because simple lookup suffices and data size is small).

2. **Punch-in race.** 
   Two identical punch-in requests arriving simultaneously are both sent to MongoDB. Because there is a unique index on `(emp_code, date)`, the first `insert_one` succeeds and the database locks the tuple. The second `insert_one` attempts to insert the same key and fails with a `pymongo.errors.DuplicateKeyError`. My code catches this exception and raises a 409 HTTP error for the loser.

3. **Ties.** 
   When two employees tie on late minutes at the cutoff rank, they receive the same rank. Since the `limit` filter is applied *after* ranking, both employees will be included in the response, even if this causes the total number of returned rows to exceed `limit`. 

4. **Headcount.** 
   The department summary uses a MongoDB aggregation that starts from the `employees` collection (not the `attendance_logs`), filtering by `joined_on <= month_end`. It then performs a `$lookup` to `attendance_logs` to compute attendance. This guarantees that all active employees are counted in the headcount even if they have zero attendance logs.

5. **One thing you would change if this had to serve 100x the data.** 
   For 100x the data, calculating the daily trend via `$lookup` in aggregations could become too slow. I would pre-aggregate or asynchronously update daily and monthly summary tables instead of calculating analytics on-the-fly from the raw attendance logs.
