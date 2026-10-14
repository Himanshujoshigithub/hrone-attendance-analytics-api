"""Employee Attendance & Analytics API - HROne Software Engineer Trainee assignment."""

from __future__ import annotations



import os

import re

from contextlib import asynccontextmanager

from datetime import date, datetime, time, timedelta, timezone

from decimal import Decimal, ROUND_HALF_UP

from typing import Literal, Optional



from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Query, Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument

from bson import json_util

from pymongo.errors import DuplicateKeyError, PyMongoError



load_dotenv()  # Existing process environment variables take precedence over .env.

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")

MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

IST = timezone(timedelta(hours=5, minutes=30))

UTC = timezone.utc

PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}

ALL_STATUS = PRESENCE | {"ABSENT", "LEAVE"}

MAX_EPOCH_MS = 4_102_444_800_000

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, tz_aware=False)

db = client[MONGO_DB]

employees = db["employees"]

attendance = db["attendance_logs"]



@asynccontextmanager

async def lifespan(app: FastAPI):

    # Idempotent indexes. The unique natural keys provide concurrency guarantees.

    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")

    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")

    employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined_on")

    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")

    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")

    attendance.create_index([("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_date_status")

    yield

    client.close()



app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)



# ---------- Models ----------

class EmployeeIn(BaseModel):

    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")

    name: str = Field(min_length=1, max_length=100)

    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    department: str = Field(min_length=1, max_length=50)

    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    joined_on: date



    @model_validator(mode="after")

    def valid_shift(self):

        if self.shift_start == self.shift_end:

            raise ValueError("shift_start must differ from shift_end")

        return self



class PunchInIn(BaseModel):

    model_config = ConfigDict(extra="ignore")

    emp_code: str

    punched_at: Optional[int] = Field(default=None, strict=True, ge=100_000_000_000, le=MAX_EPOCH_MS)

    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")

    @classmethod

    def validate_ts(cls, value):

        if value is not None:

            epoch_ms_to_datetime(value)

        return value



class PunchOutIn(BaseModel):

    model_config = ConfigDict(extra="ignore")

    emp_code: str

    punched_at: Optional[int] = Field(default=None, strict=True, ge=100_000_000_000, le=MAX_EPOCH_MS)

    @field_validator("punched_at")

    @classmethod

    def validate_ts(cls, value):

        if value is not None:

            epoch_ms_to_datetime(value)

        return value



class RegularizeIn(BaseModel):

    model_config = ConfigDict(extra="ignore")

    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None

    punch_in: Optional[int] = Field(default=None, strict=True, ge=100_000_000_000, le=MAX_EPOCH_MS)

    punch_out: Optional[int] = Field(default=None, strict=True, ge=100_000_000_000, le=MAX_EPOCH_MS)

    reason: str = Field(min_length=5, max_length=200)

    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")

    @classmethod

    def validate_ts(cls, value):

        if value is not None:

            epoch_ms_to_datetime(value)

        return value



# ---------- Common helpers ----------

def epoch_ms_to_datetime(value: int) -> datetime:

    if isinstance(value, bool) or not isinstance(value, int):

        raise ValueError("Timestamp must be an integer epoch-millisecond value")

    if value < 100_000_000_000 or value > MAX_EPOCH_MS:

        raise ValueError("Timestamp must be epoch milliseconds in the supported range")

    try:

        return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0).replace(tzinfo=None)

    except (OverflowError, OSError, ValueError) as exc:

        raise ValueError("Timestamp is out of range") from exc



def dt_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:

    if value is None:

        return None

    if value.tzinfo is None:

        value = value.replace(tzinfo=UTC)

    return int(value.timestamp()) * 1000



def utc_now() -> datetime:

    return datetime.now(UTC).replace(microsecond=0).replace(tzinfo=None)



def as_utc_aware(value: datetime) -> datetime:

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)



def date_range(month: str) -> tuple[date, date]:

    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):

        raise HTTPException(422, "month must be YYYY-MM")

    y, m = map(int, month.split("-"))

    start = date(y, m, 1)

    end = date(y + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1)

    return start, end



def round_half_up(value: float | Decimal, places: int = 2) -> float:

    q = Decimal("1").scaleb(-places)

    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))



def shift_datetime(day: date, hhmm: str) -> datetime:

    h, m = map(int, hhmm.split(":"))

    return datetime.combine(day, time(h, m), tzinfo=IST)



def shift_start_for_punch(punch_in: datetime, employee: dict) -> datetime:

    local = as_utc_aware(punch_in).astimezone(IST)

    start_h, start_m = map(int, employee["shift_start"].split(":"))

    end_h, end_m = map(int, employee["shift_end"].split(":"))

    start_t, end_t = time(start_h, start_m), time(end_h, end_m)

    shift_day = local.date()

    if end_t <= start_t and local.time().replace(tzinfo=None) < end_t:

        shift_day -= timedelta(days=1)

    return shift_datetime(shift_day, employee["shift_start"])



def attendance_date(punch_in: datetime, employee: dict) -> str:

    local = as_utc_aware(punch_in).astimezone(IST)

    start_h, start_m = map(int, employee["shift_start"].split(":"))

    end_h, end_m = map(int, employee["shift_end"].split(":"))

    if time(end_h, end_m) <= time(start_h, start_m) and local.time().replace(tzinfo=None) < time(end_h, end_m):

        return (local.date() - timedelta(days=1)).isoformat()

    return local.date().isoformat()



def compute_late_minutes(punch_in: Optional[datetime], employee: dict) -> int:

    if punch_in is None:

        return 0

    actual = as_utc_aware(punch_in)

    scheduled = shift_start_for_punch(punch_in, employee).astimezone(UTC)

    elapsed = int((actual - scheduled).total_seconds())

    return elapsed // 60 if elapsed > 600 else 0



def compute_work_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> Optional[float]:

    if punch_in is None or punch_out is None:

        return None

    seconds = int((as_utc_aware(punch_out) - as_utc_aware(punch_in)).total_seconds())

    return round_half_up(Decimal(seconds) / Decimal(3600), 2)



def compute_overtime(punch_in: Optional[datetime], punch_out: Optional[datetime], employee: dict) -> int:

    if punch_in is None or punch_out is None:

        return 0

    local_start = as_utc_aware(punch_in).astimezone(IST)

    start_h, start_m = map(int, employee["shift_start"].split(":"))

    end_h, end_m = map(int, employee["shift_end"].split(":"))

    start_t, end_t = time(start_h, start_m), time(end_h, end_m)

    shift_day = local_start.date()

    if end_t <= start_t and local_start.time().replace(tzinfo=None) < end_t:

        shift_day -= timedelta(days=1)

    end_local = shift_datetime(shift_day, employee["shift_end"])

    if end_t <= start_t:

        end_local += timedelta(days=1)

    difference = int((as_utc_aware(punch_out) - end_local.astimezone(UTC)).total_seconds())

    minutes = difference // 60

    return minutes if minutes >= 30 else 0



def derived_fields(status: str, pin: Optional[datetime], pout: Optional[datetime], employee: dict) -> dict:

    if status not in PRESENCE:

        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}

    hours = compute_work_hours(pin, pout)

    return {

        "late_minutes": compute_late_minutes(pin, employee),

        "work_hours": hours,

        "overtime_minutes": compute_overtime(pin, pout, employee),

        "half_day": bool(hours is not None and hours < 4.50),

    }



def clean_employee(doc: dict) -> dict:

    return {"emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],

            "department": doc["department"], "shift_start": doc["shift_start"],

            "shift_end": doc["shift_end"], "joined_on": str(doc["joined_on"]),

            "created_at": dt_to_epoch_ms(doc["created_at"])}



def clean_attendance(doc: dict) -> dict:

    return {"emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],

            "punch_in": dt_to_epoch_ms(doc.get("punch_in")), "punch_out": dt_to_epoch_ms(doc.get("punch_out")),

            "work_hours": doc.get("work_hours"), "late_minutes": int(doc.get("late_minutes", 0) or 0),

            "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0), "half_day": bool(doc.get("half_day", False)),

            "history": clean_history(doc.get("history", []))}



def clean_history(entries: list) -> list:

    result = []

    for entry in entries or []:

        changes = {}

        for key, pair in (entry.get("changes") or {}).items():

            changes[key] = {"from": dt_to_epoch_ms(pair.get("from")) if key in ("punch_in", "punch_out") else pair.get("from"),

                            "to": dt_to_epoch_ms(pair.get("to")) if key in ("punch_in", "punch_out") else pair.get("to")}

        result.append({"at": dt_to_epoch_ms(entry["at"]), "by": entry["by"], "reason": entry["reason"], "changes": changes})

    return result



def mongo_round_half_up(value_expr, places: int = 2):

    """Mongo expression for non-negative values rounded half-up (Mongo $round is ties-to-even)."""

    scale = 10 ** places

    return {"$divide": [{"$floor": {"$add": [{"$multiply": [value_expr, scale]}, 0.5]}}, scale]}



def present_value_expr():

    return {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}



def month_match(start: date, end: date, emp_code: Optional[str] = None) -> dict:

    q = {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}

    if emp_code:

        q["emp_code"] = emp_code

    return q



def safe_json(value):

    """Make BSON values JSON-compatible for explain responses."""

    return json_util.loads(json_util.dumps(value))



# ---------- System and employee endpoints ----------

@app.get("/health")

def health():

    try:

        client.admin.command("ping")

        return {"status": "ok"}

    except PyMongoError:

        raise HTTPException(503, "MongoDB is unavailable")



@app.post("/employees", status_code=201)

def create_employee(body: EmployeeIn):

    doc = body.model_dump()

    doc["joined_on"] = body.joined_on.isoformat()

    doc["created_at"] = utc_now()

    try:

        employees.insert_one(doc)

    except DuplicateKeyError:

        raise HTTPException(409, "emp_code already exists")

    return clean_employee(doc)



@app.get("/employees")

def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):

    filt = {"department": department} if department is not None else {}

    total = employees.count_documents(filt)

    docs = employees.find(filt, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)

    return {"items": [clean_employee(d) for d in docs], "total": total, "page": page, "page_size": page_size}



# ---------- Attendance endpoints ----------

@app.post("/attendance/punch-in", status_code=201)

def punch_in(body: PunchInIn):

    employee = employees.find_one({"emp_code": body.emp_code}, {"_id": 0})

    if not employee:

        raise HTTPException(404, "Employee not found")

    ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else utc_now()

    day = attendance_date(ts, employee)

    derived = derived_fields(body.status, ts, None, employee)

    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts,

           "punch_out": None, **derived, "history": []}

    try:

        attendance.insert_one(doc)

    except DuplicateKeyError:

        raise HTTPException(409, "Attendance already exists for this employee and date")

    return clean_attendance(doc)



@app.post("/attendance/punch-out")

def punch_out(body: PunchOutIn):

    employee = employees.find_one({"emp_code": body.emp_code}, {"_id": 0})

    if not employee:

        raise HTTPException(404, "Employee not found")

    ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else utc_now()

    # Search by most recent eligible open punch-in, including overnight records.

    rec = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}, "punch_out": None}, sort=[("punch_in", DESCENDING)])

    if not rec:

        closed = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}, "punch_out": {"$ne": None}}, sort=[("punch_in", DESCENDING)])

        if closed:

            raise HTTPException(409, "The most recent eligible attendance record is already punched out")

        raise HTTPException(404, "No eligible punch-in found")

    pin = rec["punch_in"]

    elapsed = (as_utc_aware(ts) - as_utc_aware(pin)).total_seconds()

    if elapsed <= 0 or elapsed > 24 * 3600:

        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    derived = derived_fields(rec["status"], pin, ts, employee)

    updated = attendance.find_one_and_update({"_id": rec["_id"], "punch_out": None},

        {"$set": {"punch_out": ts, **derived}, "$setOnInsert": {}}, return_document=ReturnDocument.AFTER)

    if updated is None:

        raise HTTPException(409, "Attendance was already punched out")

    return clean_attendance(updated)



@app.get("/attendance")

def list_attendance(emp_code: Optional[str] = None, date_from: Optional[date] = None,

                    date_to: Optional[date] = None, status: Optional[str] = None,

                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):

    if date_from and date_to and date_from > date_to:

        raise HTTPException(422, "date_from must be on or before date_to")

    if status is not None and status not in ALL_STATUS:

        raise HTTPException(422, "Invalid status")

    filt = {}

    if emp_code is not None: filt["emp_code"] = emp_code

    if date_from is not None or date_to is not None:

        filt["date"] = {}

        if date_from is not None: filt["date"]["$gte"] = date_from.isoformat()

        if date_to is not None: filt["date"]["$lte"] = date_to.isoformat()

    if status is not None: filt["status"] = status

    total = attendance.count_documents(filt)

    docs = attendance.find(filt).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)

    return {"items": [clean_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}



@app.patch("/attendance/{emp_code}/{date}")

def regularize_attendance(emp_code: str, date: date = Path(...), body: RegularizeIn = ...):

    employee = employees.find_one({"emp_code": emp_code}, {"_id": 0})

    if not employee:

        raise HTTPException(404, "Employee not found")

    day = date.isoformat()

    old = attendance.find_one({"emp_code": emp_code, "date": day})

    if not old:

        raise HTTPException(404, "Attendance record not found")

    patch = body.model_dump(exclude_unset=True)

    status = patch.get("status", old["status"])

    if status in {"ABSENT", "LEAVE"}:

        if "punch_in" in patch or "punch_out" in patch:

            raise HTTPException(422, "ABSENT/LEAVE cannot be supplied with punch times")

        pin = pout = None

    else:

        pin = epoch_ms_to_datetime(patch["punch_in"]) if patch.get("punch_in") is not None else old.get("punch_in")

        pout = epoch_ms_to_datetime(patch["punch_out"]) if patch.get("punch_out") is not None else old.get("punch_out")

        if pin is None:

            raise HTTPException(422, "Presence status requires punch_in")

        if attendance_date(pin, employee) != day:

            raise HTTPException(422, "punch_in must belong to the record attendance date")

        if pout is not None:

            seconds = (as_utc_aware(pout) - as_utc_aware(pin)).total_seconds()

            if seconds <= 0 or seconds > 86400:

                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    derived = derived_fields(status, pin, pout, employee)

    new_values = {"status": status, "punch_in": pin, "punch_out": pout, **derived}

    old_values = {"status": old.get("status"), "punch_in": old.get("punch_in"), "punch_out": old.get("punch_out"),

                  "work_hours": old.get("work_hours"), "late_minutes": old.get("late_minutes", 0),

                  "overtime_minutes": old.get("overtime_minutes", 0), "half_day": old.get("half_day", False)}

    changes = {}

    for key, value in new_values.items():

        previous = old_values.get(key)

        if key in ("punch_in", "punch_out"):

            same = (previous is None and value is None) or (previous is not None and value is not None and as_utc_aware(previous) == as_utc_aware(value))

        else:

            same = previous == value

        if not same:

            changes[key] = {"from": previous, "to": value}

    if not changes:

        raise HTTPException(422, "Request does not change the record")

    at = utc_now()

    history_entry = {"at": at, "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic compare-and-set prevents concurrent PATCH requests losing audit history.

    selector = {"_id": old["_id"], "history": old.get("history", [])}

    if "history" not in old:

        selector["history"] = {"$exists": False}

    update = {"$set": new_values, "$push": {"history": history_entry}}

    result = attendance.update_one(selector, update)

    if result.modified_count != 1:

        raise HTTPException(409, "Record changed concurrently; reload and retry")

    return clean_attendance(attendance.find_one({"_id": old["_id"]}))



# ---------- Aggregation helpers ----------

def employee_monthly_pipeline(emp_code: str, start: date, end: date, joined_on: str) -> list:

    weekdays = sum(1 for n in range((end - max(start, date.fromisoformat(joined_on))).days + 1)

                   if (max(start, date.fromisoformat(joined_on)) + timedelta(days=n)).weekday() < 5) if date.fromisoformat(joined_on) <= end else 0

    return [

        {"$match": month_match(start, end, emp_code)},

        {"$addFields": {"_is_weekday": {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]}}},

        {"$addFields": {"_weekday": {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]}}},

        {"$group": {"_id": None,

            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, "$_weekday"]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},

            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},

            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},

            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},

            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}},

    ]



@app.get("/analytics/employees/{emp_code}/monthly")

def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):

    employee = employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})

    if not employee:

        raise HTTPException(404, "Employee not found")

    start, end = date_range(month)

    join = date.fromisoformat(str(employee["joined_on"]))

    first = max(start, join)

    working_days = sum(1 for n in range(max(0, (end - first).days + 1)) if (first + timedelta(days=n)).weekday() < 5) if first <= end else 0

    rows = list(attendance.aggregate(employee_monthly_pipeline(emp_code, start, end, str(employee["joined_on"]))))

    row = rows[0] if rows else {}

    present = round_half_up(row.get("present_days", 0), 2)

    pct = round_half_up(Decimal(str(present)) / Decimal(working_days) * 100, 4) if working_days else None

    return {"emp_code": emp_code, "month": month, "working_days": working_days, "present_days": present,

            "leave_days": row.get("leave_days", 0), "late_count": row.get("late_count", 0),

            "total_late_minutes": row.get("total_late_minutes", 0), "total_overtime_minutes": row.get("total_overtime_minutes", 0),

            "attendance_pct": pct}



@app.get("/analytics/departments/summary")

def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):

    start, end = date_range(month)

    pipeline = [

        {"$match": {"joined_on": {"$lte": end.isoformat()}, **({"department": department} if department is not None else {})}},

        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [

            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}]}}}

        ], "as": "logs"}},

        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},

        {"$group": {"_id": {"department": "$department", "emp_code": "$emp_code"},

            "present_days": {"$sum": {"$cond": [{"$and": [{"$ne": ["$logs", None]}, {"$in": ["$logs.status", list(PRESENCE)]},

                {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 6]}]},

                {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]}, 0]}},

            "hours_sum": {"$sum": {"$cond": [{"$and": [{"$ne": ["$logs", None]}, {"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, "$logs.work_hours", 0]}},

            "hours_count": {"$sum": {"$cond": [{"$and": [{"$ne": ["$logs", None]}, {"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, 1, 0]}},

            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},

            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},

            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},

            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},

        {"$group": {"_id": "$_id.department", "headcount": {"$sum": 1}, "present_days": {"$sum": "$present_days"},

            "hours_sum": {"$sum": "$hours_sum"}, "hours_count": {"$sum": "$hours_count"},

            "late_count": {"$sum": "$late_count"}, "total_late_minutes": {"$sum": "$total_late_minutes"},

            "leave_count": {"$sum": "$leave_count"}, "on_duty_count": {"$sum": "$on_duty_count"}}},

        {"$match": {"headcount": {"$gt": 0}}},

        {"$project": {"_id": 0, "department": "$_id", "headcount": 1,

            "present_days": mongo_round_half_up("$present_days", 2),

            "avg_work_hours": {"$cond": [{"$eq": ["$hours_count", 0]}, None, mongo_round_half_up({"$divide": ["$hours_sum", "$hours_count"]}, 2)]},

            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},

        {"$sort": {"department": 1}}

    ]

    return {"month": month, "items": list(employees.aggregate(pipeline))}



@app.get("/analytics/leaderboard/late")

def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):

    start, end = date_range(month)

    pipeline = [

        {"$match": {**month_match(start, end), "late_minutes": {"$gt": 0}}},

        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},

        {"$match": {"total_late_minutes": {"$gt": 0}}},

        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},

        {"$unwind": "$employee"},

    ]

    if department is not None:

        pipeline.append({"$match": {"employee.department": department}})

    pipeline += [

        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},

        {"$match": {"rank": {"$lte": limit}}},

        {"$sort": {"total_late_minutes": -1, "_id": 1}},

        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}

    ]

    return {"month": month, "items": list(attendance.aggregate(pipeline))}



@app.get("/analytics/departments/{department}/trend")

def department_trend(department: str, from_date: date = Query(..., alias="from"), to: date = Query(...)):

    if to < from_date:

        raise HTTPException(422, "to must be on or after from")

    days = (to - from_date).days + 1

    if days > 92:

        raise HTTPException(422, "Date range cannot exceed 92 days")

    if not employees.find_one({"department": department}, {"_id": 1}):

        raise HTTPException(404, "Department not found")

    # Gap-fill and moving average run inside MongoDB. The initial range match is indexable.

    pipeline = [

        {"$match": {"date": {"$gte": from_date.isoformat(), "$lte": to.isoformat()}}},

        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},

        {"$unwind": "$employee"}, {"$match": {"employee.department": department}},

        {"$addFields": {"dateObj": {"$dateFromString": {"dateString": "$date"}}}},

        {"$group": {"_id": "$dateObj", "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},

            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},

        {"$unionWith": {"pipeline": [{"$documents": [{"_id": datetime.combine(from_date, time.min), "present_count": 0, "late_count": 0}]}]}},

        {"$group": {"_id": "$_id", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},

        {"$densify": {"field": "_id", "range": {"step": 1, "unit": "day", "bounds": [datetime.combine(from_date, time.min), datetime.combine(to + timedelta(days=1), time.min)]}}},

        {"$match": {"_id": {"$gte": datetime.combine(from_date, time.min), "$lte": datetime.combine(to, time.min)}}},

        {"$fill": {"output": {"present_count": {"value": 0}, "late_count": {"value": 0}}}},

        {"$set": {"dateStr": {"$dateToString": {"date": "$_id", "format": "%Y-%m-%d"}},

                   "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$_id"}, 2]}, {"$lte": [{"$dayOfWeek": "$_id"}, 6]}]}}},

        {"$lookup": {"from": "employees", "let": {"day": "$dateStr"}, "pipeline": [

            {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day"]}]}}},

            {"$count": "count"}

        ], "as": "headcountResult"}},

        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$headcountResult.count", 0]}, 0]}}},

        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, mongo_round_half_up({"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 100]}, 4), None]}}},

        {"$setWindowFields": {"sortBy": {"_id": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},

        {"$project": {"_id": 0, "date": "$dateStr", "is_working_day": 1, "headcount": 1, "present_count": mongo_round_half_up("$present_count", 2), "late_count": 1,

                      "attendance_rate": 1, "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_7d", None]}, None, mongo_round_half_up("$moving_avg_7d", 4)]}}},

        {"$sort": {"date": 1}}

    ]

    return {"department": department, "items": list(attendance.aggregate(pipeline))}



# ---------- Explain endpoint ----------

@app.get("/admin/explain/{endpoint}")

def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,

                     department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),

                     date_from: Optional[date] = None, date_to: Optional[date] = None, status: Optional[str] = None,

                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),

                     from_date: Optional[date] = Query(None, alias="from"), to: Optional[date] = None):

    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}

    if endpoint not in allowed:

        raise HTTPException(422, f"endpoint must be one of {sorted(allowed)}")

    if endpoint == "attendance_list":

        if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be on or before date_to")

        filt = {}

        if emp_code: filt["emp_code"] = emp_code

        if date_from or date_to:

            filt["date"] = {}

            if date_from: filt["date"]["$gte"] = date_from.isoformat()

            if date_to: filt["date"]["$lte"] = date_to.isoformat()

        if status: filt["status"] = status

        # Explain exactly the find/sort/skip/limit operation used by GET /attendance.

        command = {"find": "attendance_logs", "filter": filt, "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*page_size, "limit": page_size}

        collection = "attendance_logs"

    elif endpoint == "employee_monthly":

        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")

        start, end = date_range(month)

        emp = employees.find_one({"emp_code": emp_code}, {"joined_on": 1})

        if not emp: raise HTTPException(404, "Employee not found")

        command = {"aggregate": "attendance_logs", "pipeline": employee_monthly_pipeline(emp_code, start, end, str(emp["joined_on"])), "cursor": {}}

        collection = "attendance_logs"

    elif endpoint == "department_summary":

        if not month: raise HTTPException(422, "month is required")

        start, end = date_range(month)

        pipeline = [

            {"$match": {"joined_on": {"$lte": end.isoformat()}, **({"department": department} if department else {})}},

            {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}]}}}], "as": "logs"}},

            {"$project": {"_id": 0, "department": 1, "emp_code": 1, "logs": 1}}

        ]

        command = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}

        collection = "employees"

    elif endpoint == "late_leaderboard":

        if not month: raise HTTPException(422, "month is required")

        start, end = date_range(month)

        pipeline = [{"$match": {**month_match(start, end), "late_minutes": {"$gt": 0}}},

                    {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},

                    {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}}, {"$unwind": "$employee"}]

        if department is not None: pipeline.append({"$match": {"employee.department": department}})

        pipeline += [{"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}}, {"$match": {"rank": {"$lte": limit}}}]

        command = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}

        collection = "attendance_logs"

    else:

        if not department or not from_date or not to: raise HTTPException(422, "department, from and to are required")

        if to < from_date or (to-from_date).days >= 92: raise HTTPException(422, "Invalid trend date range")

        pipeline = [{"$match": {"date": {"$gte": from_date.isoformat(), "$lte": to.isoformat()}}},

                    {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},

                    {"$match": {"employee.department": department}}]

        command = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}

        collection = "attendance_logs"

    try:

        result = client[db.name].command({"explain": command, "verbosity": "executionStats"})

    except PyMongoError as exc:

        raise HTTPException(500, f"Could not explain query: {exc.__class__.__name__}")

    return {"endpoint": endpoint, "collection": collection, "explain": safe_json(result)}