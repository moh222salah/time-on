import frappe
from frappe import _
from frappe.utils import (
    nowdate, now_datetime, get_first_day, get_last_day,
    add_days, getdate, format_date, flt, cint
)
from datetime import datetime, timedelta
import json


# ─────────────────────────────────────────────
#  MAIN PAGE INIT
# ─────────────────────────────────────────────
def get_context(context):
    context.no_cache = 1


# ─────────────────────────────────────────────
#  1. TEAM STATS  (5 بطاقات الـ KPI)
# ─────────────────────────────────────────────
@frappe.whitelist()
def get_team_stats(date=None, team=None):
    date = date or nowdate()
    filters = {"status": "Active", "date_of_joining": ["<=", date]}
    if team:
        filters["department"] = team

    employees = frappe.get_all(
        "Employee",
        filters=filters,
        fields=["name", "employee_name", "department"],
    )
    employee_ids = [e.name for e in employees]
    total = len(employee_ids)

    if not employee_ids:
        return {"total": 0, "working": 0, "on_break": 0, "stopped": 0, "not_installed": 0}

    # Latest checkin per employee today
    checkins = frappe.db.sql(
        """
        SELECT employee, log_type, MAX(time) AS last_time
        FROM `tabEmployee Checkin`
        WHERE DATE(time) = %s
          AND employee IN ({})
        GROUP BY employee, log_type
        """.format(", ".join(["%s"] * len(employee_ids))),
        [date] + employee_ids,
        as_dict=True,
    )

    last_log = {}
    for c in checkins:
        emp = c["employee"]
        if emp not in last_log or c["last_time"] > last_log[emp]["last_time"]:
            last_log[emp] = c

    working = on_break = stopped = not_installed = 0
    for emp_id in employee_ids:
        log = last_log.get(emp_id)
        if not log:
            not_installed += 1
        elif log["log_type"] == "IN":
            working += 1
        elif log["log_type"] == "OUT":
            stopped += 1
        else:
            on_break += 1

    return {
        "total": total,
        "working": working,
        "on_break": on_break,
        "stopped": stopped,
        "not_installed": not_installed,
    }


# ─────────────────────────────────────────────
#  2. WORKLOAD ANALYSIS  (Chart)
# ─────────────────────────────────────────────
@frappe.whitelist()
def get_workload_analysis(period="week", team=None):
    today = getdate(nowdate())

    if period == "today":
        dates = [today]
    elif period == "week":
        dates = [today - timedelta(days=i) for i in range(6, -1, -1)]
    else:  # month
        first = get_first_day(today)
        last  = get_last_day(today)
        dates = []
        d = first
        while d <= last:
            dates.append(d)
            d = add_days(d, 1)
            d = getdate(d)

    filters = {}
    if team:
        filters["department"] = team

    employees = frappe.get_all("Employee", filters={**filters, "status": "Active"}, pluck="name")

    result_labels = []
    result_hours  = []

    for d in dates:
        att_filters = {"attendance_date": d, "status": ["in", ["Present", "Work From Home"]]}
        if team:
            att_filters["department"] = team

        rows = frappe.get_all("Attendance", filters=att_filters, fields=["working_hours"])
        total_hrs = sum(flt(r.working_hours) for r in rows)
        result_labels.append(format_date(d, "dd MMM"))
        result_hours.append(round(total_hrs, 1))

    return {"labels": result_labels, "hours": result_hours}


# ─────────────────────────────────────────────
#  3. LATE CLOCK-IN  (جدول التأخير)
# ─────────────────────────────────────────────
@frappe.whitelist()
def get_late_clockin(date=None, team=None):
    date = date or nowdate()

    emp_filters = {"status": "Active"}
    if team:
        emp_filters["department"] = team

    employees = frappe.get_all(
        "Employee",
        filters=emp_filters,
        fields=["name", "employee_name", "department", "image"],
    )
    emp_map = {e.name: e for e in employees}

    # Get shift assignments
    shift_assignments = frappe.get_all(
        "Shift Assignment",
        filters={
            "employee": ["in", list(emp_map.keys())],
            "start_date": ["<=", date],
            "docstatus": 1,
        },
        fields=["employee", "shift_type"],
        order_by="start_date desc",
    )
    emp_shift = {}
    for sa in shift_assignments:
        if sa.employee not in emp_shift:
            emp_shift[sa.employee] = sa.shift_type

    # Get shift start times
    shift_types = list(set(emp_shift.values()))
    shift_info = {}
    if shift_types:
        for st in frappe.get_all(
            "Shift Type",
            filters={"name": ["in", shift_types]},
            fields=["name", "start_time"],
        ):
            shift_info[st.name] = st.start_time

    # Actual first checkin per employee
    checkins = frappe.db.sql(
        """
        SELECT employee, MIN(time) AS first_in
        FROM `tabEmployee Checkin`
        WHERE DATE(time) = %s AND log_type = 'IN'
          AND employee IN ({})
        GROUP BY employee
        """.format(", ".join(["%s"] * len(emp_map))),
        [date] + list(emp_map.keys()),
        as_dict=True,
    )
    first_in_map = {c.employee: c.first_in for c in checkins}

    late_list = []
    for emp_id, emp in emp_map.items():
        shift = emp_shift.get(emp_id)
        if not shift:
            continue
        start_time = shift_info.get(shift)
        if not start_time:
            continue

        shift_start = datetime.strptime(str(start_time), "%H:%M:%S").replace(
            year=today_dt().year, month=today_dt().month, day=today_dt().day
        )
        grace = shift_start + timedelta(minutes=10)

        first_checkin = first_in_map.get(emp_id)
        if not first_checkin:
            continue

        if first_checkin > grace:
            late_by = int((first_checkin - shift_start).total_seconds() / 60)
            late_list.append({
                "employee": emp_id,
                "employee_name": emp.employee_name,
                "department": emp.department,
                "image": emp.image,
                "checkin_time": first_checkin.strftime("%H:%M"),
                "shift_start": str(start_time)[:5],
                "late_by_minutes": late_by,
            })

    late_list.sort(key=lambda x: x["late_by_minutes"], reverse=True)
    return late_list[:20]


# ─────────────────────────────────────────────
#  4. ABSENT TODAY
# ─────────────────────────────────────────────
@frappe.whitelist()
def get_absence_today(date=None, team=None):
    date = date or nowdate()

    emp_filters = {"status": "Active"}
    if team:
        emp_filters["department"] = team

    employees = frappe.get_all(
        "Employee",
        filters=emp_filters,
        fields=["name", "employee_name", "department", "image"],
    )

    absent_records = frappe.get_all(
        "Attendance",
        filters={
            "attendance_date": date,
            "status": "Absent",
            "employee": ["in", [e.name for e in employees]],
            "docstatus": 1,
        },
        fields=["employee", "employee_name"],
    )

    absent_ids = {r.employee for r in absent_records}
    result = [
        {
            "employee": e.name,
            "employee_name": e.employee_name,
            "department": e.department,
            "image": e.image,
        }
        for e in employees
        if e.name in absent_ids
    ]
    return result


# ─────────────────────────────────────────────
#  5. WORK HOURS SUMMARY  (بار شارت)
# ─────────────────────────────────────────────
@frappe.whitelist()
def get_work_hours_summary(period="week", team=None):
    today = getdate(nowdate())

    if period == "week":
        start = today - timedelta(days=today.weekday())
    elif period == "month":
        start = get_first_day(today)
    else:
        start = today

    att_filters = {
        "attendance_date": ["between", [start, today]],
        "status": ["in", ["Present", "Work From Home"]],
        "docstatus": 1,
    }
    if team:
        att_filters["department"] = team

    rows = frappe.get_all(
        "Attendance",
        filters=att_filters,
        fields=["employee", "employee_name", "working_hours"],
    )

    emp_hours = {}
    for r in rows:
        emp_hours[r.employee] = emp_hours.get(r.employee, 0) + flt(r.working_hours)

    sorted_data = sorted(emp_hours.items(), key=lambda x: x[1], reverse=True)[:10]

    # Get employee names
    emp_names = {
        e.name: e.employee_name
        for e in frappe.get_all(
            "Employee",
            filters={"name": ["in", [s[0] for s in sorted_data]]},
            fields=["name", "employee_name"],
        )
    }

    return [
        {"employee": s[0], "employee_name": emp_names.get(s[0], s[0]), "hours": round(s[1], 1)}
        for s in sorted_data
    ]


# ─────────────────────────────────────────────
#  6. UPCOMING HOLIDAYS
# ─────────────────────────────────────────────
@frappe.whitelist()
def get_upcoming_holidays(days=30):
    today = nowdate()
    end = add_days(today, cint(days))

    company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value(
        "Global Defaults", "default_company"
    )
    holiday_list = None
    if company:
        holiday_list = frappe.db.get_value("Company", company, "default_holiday_list")

    filters = {"holiday_date": ["between", [today, end]]}
    if holiday_list:
        filters["parent"] = holiday_list

    holidays = frappe.get_all(
        "Holiday",
        filters=filters,
        fields=["holiday_date", "description"],
        order_by="holiday_date asc",
        limit=10,
    )
    return holidays


# ─────────────────────────────────────────────
#  7. UPCOMING LEAVES
# ─────────────────────────────────────────────
@frappe.whitelist()
def get_upcoming_leaves(team=None, days=14):
    today = nowdate()
    end = add_days(today, cint(days))

    filters = {
        "from_date": [">=", today],
        "to_date": ["<=", end],
        "status": "Approved",
        "docstatus": 1,
    }
    if team:
        filters["department"] = team

    leaves = frappe.get_all(
        "Leave Application",
        filters=filters,
        fields=["employee", "employee_name", "leave_type", "from_date", "to_date", "total_leave_days"],
        order_by="from_date asc",
        limit=15,
    )
    return leaves


# ─────────────────────────────────────────────
#  8. RECENT CHECKINS
# ─────────────────────────────────────────────
@frappe.whitelist()
def get_recent_checkins(limit=20, team=None):
    emp_filters = {"status": "Active"}
    if team:
        emp_filters["department"] = team

    employees = frappe.get_all("Employee", filters=emp_filters, pluck="name")

    if not employees:
        return []

    checkins = frappe.db.sql(
        """
        SELECT ec.employee, ec.employee_name, ec.log_type,
               ec.time, ec.device_id,
               e.department, e.image
        FROM `tabEmployee Checkin` ec
        LEFT JOIN `tabEmployee` e ON e.name = ec.employee
        WHERE ec.employee IN ({})
          AND DATE(ec.time) = %s
        ORDER BY ec.time DESC
        LIMIT %s
        """.format(", ".join(["%s"] * len(employees))),
        employees + [nowdate(), cint(limit)],
        as_dict=True,
    )
    return checkins


# ─────────────────────────────────────────────
#  9. MONTHLY ATTENDANCE MATRIX
# ─────────────────────────────────────────────
@frappe.whitelist()
def get_monthly_attendance(month=None, year=None, team=None):
    today = getdate(nowdate())
    month = cint(month) or today.month
    year  = cint(year)  or today.year

    first_day = getdate(f"{year}-{month:02d}-01")
    last_day  = get_last_day(first_day)

    emp_filters = {"status": "Active"}
    if team:
        emp_filters["department"] = team

    employees = frappe.get_all(
        "Employee",
        filters=emp_filters,
        fields=["name", "employee_name", "department"],
        order_by="employee_name",
    )

    attendance = frappe.get_all(
        "Attendance",
        filters={
            "attendance_date": ["between", [first_day, last_day]],
            "employee": ["in", [e.name for e in employees]],
            "docstatus": 1,
        },
        fields=["employee", "attendance_date", "status"],
    )

    att_map = {}
    for a in attendance:
        key = (a.employee, str(a.attendance_date))
        att_map[key] = a.status

    days = []
    d = first_day
    while d <= last_day:
        days.append(str(d))
        d = add_days(d, 1)
        d = getdate(d)

    result = []
    for emp in employees:
        row = {
            "employee": emp.name,
            "employee_name": emp.employee_name,
            "department": emp.department,
            "attendance": {},
        }
        for day in days:
            row["attendance"][day] = att_map.get((emp.name, day), "")
        result.append(row)

    return {"employees": result, "days": days}


# ─────────────────────────────────────────────
#  10. LEAVE MANAGEMENT — Apply Leave
# ─────────────────────────────────────────────
@frappe.whitelist()
def submit_leave_application(
    employee, leave_type, from_date, to_date, reason="", half_day=0, half_day_date=None
):
    doc = frappe.get_doc({
        "doctype": "Leave Application",
        "employee": employee,
        "leave_type": leave_type,
        "from_date": from_date,
        "to_date": to_date,
        "description": reason,
        "half_day": cint(half_day),
        "half_day_date": half_day_date,
        "status": "Open",
    })
    doc.insert(ignore_permissions=True)
    doc.submit()
    return {"success": True, "name": doc.name}


@frappe.whitelist()
def get_leave_types():
    return frappe.get_all("Leave Type", fields=["name", "max_leaves_allowed"], order_by="name")


@frappe.whitelist()
def get_leave_balance(employee, leave_type, date=None):
    from hrms.hr.doctype.leave_application.leave_application import get_leave_balance_on
    date = date or nowdate()
    balance = get_leave_balance_on(employee, leave_type, date)
    return {"balance": balance}


# ─────────────────────────────────────────────
#  11. LEAVE SUMMARY
# ─────────────────────────────────────────────
@frappe.whitelist()
def get_leave_summary(employee=None, year=None):
    year = cint(year) or getdate(nowdate()).year
    filters = {
        "from_date": [">=", f"{year}-01-01"],
        "to_date": ["<=", f"{year}-12-31"],
        "status": "Approved",
        "docstatus": 1,
    }
    if employee:
        filters["employee"] = employee

    leaves = frappe.get_all(
        "Leave Application",
        filters=filters,
        fields=["employee", "employee_name", "leave_type", "total_leave_days", "from_date", "to_date"],
        order_by="from_date desc",
    )
    return leaves


# ─────────────────────────────────────────────
#  12. DEPARTMENTS (for filter dropdown)
# ─────────────────────────────────────────────
@frappe.whitelist()
def get_departments():
    return frappe.get_all("Department", filters={"is_group": 0}, fields=["name"], order_by="name")


# ─────────────────────────────────────────────
#  13. EMPLOYEES LIST (for Add Users, etc.)
# ─────────────────────────────────────────────
@frappe.whitelist()
def get_employees(team=None, search=None):
    filters = {"status": "Active"}
    if team:
        filters["department"] = team
    if search:
        filters["employee_name"] = ["like", f"%{search}%"]

    return frappe.get_all(
        "Employee",
        filters=filters,
        fields=["name", "employee_name", "department", "designation", "image", "date_of_joining"],
        order_by="employee_name",
        limit=50,
    )


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def today_dt():
    return datetime.now()
