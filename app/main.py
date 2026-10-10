import os
import calendar
import pymongo
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

# Create indexes idempotently at startup
db.employees.create_index([("emp_code", ASCENDING)], unique=True)
db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
db.employees.create_index([("department", ASCENDING)])

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.replace(microsecond=0)

def now_dt() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)

def dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)

def get_attendance_date(punch_in_dt: datetime, shift_start_str: str, shift_end_str: str) -> str:
    ist_dt = punch_in_dt.astimezone(IST)
    date_str = ist_dt.strftime("%Y-%m-%d")
    if shift_end_str <= shift_start_str:
        ist_time_str = ist_dt.strftime("%H:%M:%S")
        if ist_time_str < shift_end_str + ":00":
            prev_day = ist_dt - timedelta(days=1)
            date_str = prev_day.strftime("%Y-%m-%d")
    return date_str

def compute_late_minutes(punch_in_dt: datetime, date_str: str, shift_start_str: str) -> int:
    ist_dt = punch_in_dt.astimezone(IST)
    start_dt_str = f"{date_str} {shift_start_str}"
    shift_start_dt = datetime.strptime(start_dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    diff_seconds = (ist_dt - shift_start_dt).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0

def compute_overtime(punch_out_dt: datetime, date_str: str, shift_start_str: str, shift_end_str: str) -> int:
    ist_out = punch_out_dt.astimezone(IST)
    end_date_str = date_str
    if shift_end_str <= shift_start_str:
        end_date = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
        end_date_str = end_date.strftime("%Y-%m-%d")
    end_dt_str = f"{end_date_str} {shift_end_str}"
    shift_end_dt = datetime.strptime(end_dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    diff_seconds = (ist_out - shift_end_dt).total_seconds()
    if diff_seconds > 0:
        overtime_mins = int(diff_seconds // 60)
        if overtime_mins >= 30:
            return overtime_mins
    return 0

def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    hours = diff_sec / 3600
    val = Decimal(str(hours)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return float(val)

def compute_half_day(work_hours: float) -> bool:
    return work_hours < 4.50

def format_log(d: dict) -> dict:
    d.pop("_id", None)
    if d.get("punch_in"): d["punch_in"] = dt_to_ms(d["punch_in"])
    if d.get("punch_out"): d["punch_out"] = dt_to_ms(d["punch_out"])
    if "history" in d:
        for h in d["history"]:
            if h.get("at"): h["at"] = dt_to_ms(h["at"])
            if "changes" in h:
                for k in ["punch_in", "punch_out"]:
                    if k in h["changes"]:
                        h["changes"][k]["from"] = dt_to_ms(h["changes"][k]["from"]) if h["changes"][k].get("from") else None
                        h["changes"][k]["to"] = dt_to_ms(h["changes"][k]["to"]) if h["changes"][k].get("to") else None
    return d

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r'^EMP\d{4,6}$')
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    shift_end: str = Field(default="18:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    joined_on: str

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: str = Field(default="PRESENT")

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)

class RegularizeIn(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = None
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)

# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start and shift_end must differ")
    doc = body.model_dump()
    dt = now_dt()
    doc["created_at"] = dt
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = dt_to_ms(dt)
    return doc

@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    for item in items:
        if item.get("created_at"):
            item["created_at"] = dt_to_ms(item["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    if body.status not in ["PRESENT", "WFH", "ON_DUTY"]:
        raise HTTPException(422, "Invalid status for punch-in")
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    ts = ms_to_dt(body.punched_at) if body.punched_at else now_dt()
    d = get_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, d, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")
        
    return format_log(doc)

@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    ts = ms_to_dt(body.punched_at) if body.punched_at else now_dt()
    log = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)]
    )
    if not log:
        raise HTTPException(404, "No valid punch-in found")
    if log.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
        
    diff_sec = (ts - log["punch_in"]).total_seconds()
    if diff_sec <= 0 or diff_sec > 24 * 3600:
        raise HTTPException(422, "Invalid punch-out time")
        
    work_hours = compute_work_hours(log["punch_in"], ts)
    overtime_mins = compute_overtime(ts, log["date"], emp["shift_start"], emp["shift_end"])
    half_day = compute_half_day(work_hours)
    
    update = {
        "$set": {
            "punch_out": ts,
            "work_hours": work_hours,
            "overtime_minutes": overtime_mins,
            "half_day": half_day
        }
    }
    res = db.attendance_logs.find_one_and_update(
        {"_id": log["_id"], "punch_out": None},
        update,
        return_document=pymongo.ReturnDocument.AFTER
    )
    if not res:
        raise HTTPException(409, "Concurrent punch out conflict")
        
    return format_log(res)

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from > date_to")
    if status:
        q["status"] = status
        
    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size))
    
    return {"items": [format_log(d) for d in docs], "total": total, "page": page, "page_size": page_size}

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    log = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not log:
        raise HTTPException(404, "Record not found")
        
    updates = {}
    changes = {}
    
    new_status = body.status if body.status else log.get("status")
    if new_status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "Cannot supply punch times for ABSENT/LEAVE")
    
    new_punch_in = log.get("punch_in")
    new_punch_out = log.get("punch_out")
    
    if body.punch_in is not None: new_punch_in = ms_to_dt(body.punch_in)
    if body.punch_out is not None: new_punch_out = ms_to_dt(body.punch_out)
        
    if new_status in ["ABSENT", "LEAVE"]:
        new_punch_in = None
        new_punch_out = None
    else:
        if new_punch_in is None:
            raise HTTPException(422, "Presence status requires a punch_in")
        d = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if d != log["date"]:
            raise HTTPException(422, "punch_in falls on different date")
        if new_punch_out is not None:
            diff_sec = (new_punch_out - new_punch_in).total_seconds()
            if diff_sec <= 0 or diff_sec > 24 * 3600:
                raise HTTPException(422, "Invalid punch_out time")
                
    if new_status in ["ABSENT", "LEAVE"]:
        new_work_hours, new_late_minutes, new_overtime, new_half_day = None, 0, 0, False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in, log["date"], emp["shift_start"])
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime = compute_overtime(new_punch_out, log["date"], emp["shift_start"], emp["shift_end"])
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours, new_overtime, new_half_day = None, 0, False
            
    def track_change(key, old_val, new_val):
        if old_val != new_val:
            changes[key] = {"from": old_val, "to": new_val}
            updates[key] = new_val
            
    track_change("status", log.get("status"), new_status)
    track_change("punch_in", log.get("punch_in"), new_punch_in)
    track_change("punch_out", log.get("punch_out"), new_punch_out)
    track_change("work_hours", log.get("work_hours"), new_work_hours)
    track_change("late_minutes", log.get("late_minutes", 0), new_late_minutes)
    track_change("overtime_minutes", log.get("overtime_minutes", 0), new_overtime)
    track_change("half_day", log.get("half_day", False), new_half_day)
    
    if not changes:
        raise HTTPException(422, "Request changes nothing")
        
    hist_size = len(log.get("history", []))
    match_query = {"_id": log["_id"], "history": {"$size": hist_size}} if hist_size > 0 else {"_id": log["_id"], "$or": [{"history": {"$exists": False}}, {"history": {"$size": 0}}]}
    
    db_updates = {"$set": updates, "$push": {"history": {"at": now_dt(), "by": body.regularized_by, "reason": body.reason, "changes": changes}}}
    res = db.attendance_logs.find_one_and_update(match_query, db_updates, return_document=pymongo.ReturnDocument.AFTER)
    if not res:
        raise HTTPException(409, "Concurrent edit conflict")
        
    return format_log(res)

def pipeline_employee_monthly(emp_code: str, month: str):
    start_date = f"{month}-01"
    y, m = month.split("-")
    _, last_day = calendar.monthrange(int(y), int(m))
    end_date = f"{month}-{last_day}"
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_date, "$lte": end_date}}},
        {"$addFields": {
            "is_weekday": {
                "$let": {
                    "vars": {"dow": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}},
                    "in": {"$lte": ["$$dow", 5]}
                }
            }
        }},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, "$is_weekday"]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                        0
                    ]
                }
            },
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}
        }}
    ]

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., regex=r'^\d{4}-(0[1-9]|1[0-2])$')):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    y, m = month.split("-")
    _, last_day = calendar.monthrange(int(y), int(m))
    start_dt = datetime(int(y), int(m), 1)
    end_dt = datetime(int(y), int(m), last_day)
    joined_dt = datetime.fromisoformat(emp["joined_on"])
    
    working_days = 0
    if joined_dt <= end_dt:
        actual_start = max(start_dt, joined_dt)
        curr = actual_start
        while curr <= end_dt:
            if curr.weekday() < 5: working_days += 1
            curr += timedelta(days=1)
            
    pipeline = pipeline_employee_monthly(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))
    stats = agg_res[0] if agg_res else {"present_days": 0.0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0}
    
    attendance_pct = None
    if working_days > 0:
        val = (stats["present_days"] / working_days) * 100
        attendance_pct = float(Decimal(str(val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
        
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": float(stats["present_days"]), "leave_days": stats["leave_days"],
        "late_count": stats["late_count"], "total_late_minutes": stats["total_late_minutes"],
        "total_overtime_minutes": stats["total_overtime_minutes"], "attendance_pct": attendance_pct
    }

def pipeline_department_summary(month: str, department: Optional[str] = None):
    start_date = f"{month}-01"
    y, m = month.split("-")
    _, last_day = calendar.monthrange(int(y), int(m))
    end_date = f"{month}-{last_day}"
    match_stage = {"joined_on": {"$lte": end_date}}
    if department: match_stage["department"] = department
    return [
        {"$match": match_stage},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "emp_codes": {"$push": "$emp_code"}}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp_codes": "$emp_codes"},
            "pipeline": [
                {"$match": {"$expr": {"$in": ["$emp_code", "$$emp_codes"]}, "date": {"$gte": start_date, "$lte": end_date}}},
                {"$addFields": {
                    "is_weekday": {"$let": {"vars": {"dow": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}, "in": {"$lte": ["$$dow", 5]}}},
                    "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
                }}
            ],
            "as": "logs"
        }},
        {"$project": {"department": "$_id", "_id": 0, "headcount": 1, "logs": 1}},
        {"$sort": {"department": ASCENDING}}
    ]

@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., regex=r'^\d{4}-(0[1-9]|1[0-2])$'), department: Optional[str] = None):
    pipeline = pipeline_department_summary(month, department)
    depts = list(db.employees.aggregate(pipeline))
    items = []
    for d in depts:
        present_days = 0.0
        work_hours_list = []
        late_count, total_late, leave_count, on_duty_count = 0, 0, 0, 0
        for log in d["logs"]:
            if log.get("is_present") and log.get("is_weekday"): present_days += 0.5 if log.get("half_day") else 1.0
            if log.get("is_present") and log.get("work_hours") is not None: work_hours_list.append(log["work_hours"])
            if log.get("late_minutes", 0) > 0:
                late_count += 1
                total_late += log["late_minutes"]
            if log.get("status") == "LEAVE": leave_count += 1
            if log.get("status") == "ON_DUTY": on_duty_count += 1
        avg_work_hours = float(Decimal(str(sum(work_hours_list) / len(work_hours_list))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)) if work_hours_list else None
        items.append({"department": d["department"], "headcount": d["headcount"], "present_days": present_days, "avg_work_hours": avg_work_hours, "late_count": late_count, "total_late_minutes": total_late, "leave_count": leave_count, "on_duty_count": on_duty_count})
    return {"month": month, "items": items}

def pipeline_late_leaderboard(month: str, department: Optional[str] = None):
    start_date = f"{month}-01"
    y, m = month.split("-")
    _, last_day = calendar.monthrange(int(y), int(m))
    end_date = f"{month}-{last_day}"
    pipeline = [
        {"$match": {"date": {"$gte": start_date, "$lte": end_date}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"}
    ]
    if department: pipeline.append({"$match": {"emp.department": department}})
    pipeline.extend([
        {"$project": {"emp_code": "$_id", "name": "$emp.name", "department": "$emp.department", "total_late_minutes": 1, "late_count": 1, "_id": 0}},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}}
    ])
    return pipeline

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., regex=r'^\d{4}-(0[1-9]|1[0-2])$'), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    pipeline = pipeline_late_leaderboard(month, department)
    results = list(db.attendance_logs.aggregate(pipeline))
    ranked = []
    current_rank, current_late = 1, None
    for i, r in enumerate(results):
        if r["total_late_minutes"] != current_late:
            current_rank = i + 1
            current_late = r["total_late_minutes"]
        if current_rank > limit: break
        r["rank"] = current_rank
        ranked.append(r)
    return {"month": month, "items": ranked}

def pipeline_department_trend(department: str, from_date: str, to_date: str):
    return [
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department, "date": {"$gte": from_date, "$lte": to_date}}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}
        }},
        {"$project": {"date": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1, "_id": 0}},
        {"$sort": {"date": ASCENDING}},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [datetime.fromisoformat(from_date), datetime.fromisoformat(to_date) + timedelta(days=1)]}}},
        {"$addFields": {
            "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$let": {"vars": {"dow": {"$isoDayOfWeek": "$date"}}, "in": {"$lte": ["$$dow", 5]}}}
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"curr_date": "$date_str"},
            "pipeline": [{"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$curr_date"]}}}, {"$count": "headcount"}],
            "as": "hc"
        }},
        {"$addFields": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.headcount", 0]}, 0]}}},
        {"$addFields": {
            "attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}
        }},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"date": "$date_str", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1, "_id": 0}},
        {"$sort": {"date": ASCENDING}}
    ]

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "Department not found")
    diff = (datetime.fromisoformat(to_date) - datetime.fromisoformat(from_date)).days
    if diff < 0 or diff > 92:
        raise HTTPException(422, "Invalid date range")
        
    pipeline = pipeline_department_trend(department, from_date, to_date)
    items = list(db.attendance_logs.aggregate(pipeline))
    for item in items:
        if item.get("attendance_rate") is not None:
            item["attendance_rate"] = float(Decimal(str(item["attendance_rate"])).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP))
        if item.get("moving_avg_7d") is not None:
            item["moving_avg_7d"] = float(Decimal(str(item["moving_avg_7d"])).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP))
    return {"department": department, "items": items}

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None,
    status: Optional[str] = None, from_date: Optional[str] = Query(None, alias="from"), to_date: Optional[str] = Query(None, alias="to"),
    page: int = 1, page_size: int = Query(20, ge=1, le=100)
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        if status: q["status"] = status
        skip = (page - 1) * page_size
        explain_res = db.command("explain", {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": skip, "limit": page_size}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}
    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "Missing params")
        explain_res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline_employee_monthly(emp_code, month), "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}
    elif endpoint == "department_summary":
        if not month: raise HTTPException(422, "Missing month")
        explain_res = db.command("explain", {"aggregate": "employees", "pipeline": pipeline_department_summary(month, department), "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}
    elif endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, "Missing month")
        explain_res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline_late_leaderboard(month, department), "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date: raise HTTPException(422, "Missing params")
        explain_res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline_department_trend(department, from_date, to_date), "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}
    else:
        raise HTTPException(422, "Invalid endpoint")
