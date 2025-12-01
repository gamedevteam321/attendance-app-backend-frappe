"""
API methods for Attendance Portal
All methods are whitelisted for frontend access via frappe-react-sdk
"""

import frappe
from frappe import _
from frappe.utils import today, now_datetime, get_datetime
from frappe.model.rename_doc import rename_doc
from datetime import datetime
import math


@frappe.whitelist(allow_guest=True)
def get_current_profile():
    """
    Get current user's profile with role information
    
    Returns:
        dict: {
            user: str,
            employee_id: str,
            employee_name: str,
            roles: list,
            is_manager: bool,
            is_hr_admin: bool
        }
    """
    user = frappe.session.user
    print(f"DEBUG: get_current_profile called. User: {user}")
    print(f"DEBUG: Headers: {frappe.request.headers}")
    
    # Get employee linked to this user
    employee = frappe.db.get_value("Employee", {"user_id": user}, ["name", "employee_name"], as_dict=True)
    print(f"DEBUG: Found employee: {employee}")
    
    if not employee:
        # Return basic profile without employee details
        roles = frappe.get_roles(user)
        return {
            "user": user,
            "employee_id": None,
            "employee_name": user, # Fallback to username
            "roles": roles,
            "is_manager": "Manager" in roles,
            "is_hr_admin": "HR Admin" in roles,
            "debug_message": f"No employee found for user '{user}' via query {{'user_id': '{user}'}}"
        }
    
    # Get user roles
    roles = frappe.get_roles(user)
    
    return {
        "user": user,
        "employee_id": employee.name,
        "employee_name": employee.employee_name,
        "roles": roles,
        "is_manager": "Manager" in roles,
        "is_hr_admin": "HR Admin" in roles
    }


def haversine_distance(lat1, lon1, lat2, lon2):
    """
    Calculate the great circle distance between two points on earth (in meters)
    
    Args:
        lat1, lon1: First point coordinates
        lat2, lon2: Second point coordinates
    
    Returns:
        float: Distance in meters
    """
    # Convert decimal degrees to radians
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    
    # Haversine formula
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a))
    
    # Radius of earth in meters
    r = 6371000
    
    return c * r


@frappe.whitelist()
def is_inside_office(employee, lat, lng):
    """
    Check if coordinates are within any allowed office location
    
    Args:
        employee: Employee ID
        lat: Latitude
        lng: Longitude
    
    Returns:
        dict: {
            inside: bool,
            office: str | None,
            distance: float | None
        }
    """
    lat = float(lat)
    lng = float(lng)
    
    # Get allowed office locations for employee
    employee_doc = frappe.get_doc("Employee", employee)
    
    if not employee_doc.allowed_locations:
        return {
            "inside": True,
            "office": "Anywhere",
            "distance": 0,
            "message": "No office locations assigned - allowing from anywhere"
        }
    
    # Check each allowed office location
    for location_row in employee_doc.allowed_locations:
        office = frappe.get_doc("Office Location", location_row.office)
        
        if not office.is_active:
            continue
        
        # Calculate distance
        distance = haversine_distance(lat, lng, office.latitude, office.longitude)
        
        # Check if within radius
        if distance <= office.radius_meters:
            return {
                "inside": True,
                "office": office.name,
                "office_name": office.office_name,
                "distance": round(distance, 2)
            }
    
    return {
        "inside": False,
        "office": None,
        "distance": None
    }


@frappe.whitelist()
def has_remote_for_date(employee, date):
    """
    Check if employee has remote work (pending or approved) for given date
    
    Args:
        employee: Employee ID
        date: Date string (YYYY-MM-DD)
    
    Returns:
        dict: {
            has_remote: bool,
            request_name: str | None,
            request_status: str | None  # "Pending", "Approved", or None
        }
    """
    # Find remote working requests (both pending and approved) that cover this date
    # Get all matching requests
    requests = frappe.get_all(
        "Remote Working Request",
        filters={
            "employee": employee,
            "status": ["in", ["Pending", "Approved"]],
            "from_date": ["<=", date],
            "to_date": [">=", date]
        },
        fields=["name", "from_date", "to_date", "status"]
    )
    
    if requests:
        # Prioritize Approved over Pending
        approved_request = next((r for r in requests if r.status == "Approved"), None)
        if approved_request:
            return {
                "has_remote": True,
                "request_name": approved_request.name,
                "request_status": approved_request.status
            }
        else:
            # Return pending request
            return {
                "has_remote": True,
                "request_name": requests[0].name,
                "request_status": requests[0].status
            }
    
    return {
        "has_remote": False,
        "request_name": None,
        "request_status": None
    }


@frappe.whitelist()
def mark_punch(employee, lat, lng, action):
    """
    Mark punch in or out
    
    Args:
        employee: Employee ID
        lat: Latitude
        lng: Longitude
        action: "IN" or "OUT"
    
    Returns:
        dict: Attendance Log document
    """
    # Validate that employee belongs to current user
    current_user = frappe.session.user
    employee_user = frappe.db.get_value("Employee", employee, "user_id")
    
    if employee_user != current_user:
        frappe.throw(_("You can only mark attendance for yourself"))
    
    lat = float(lat)
    lng = float(lng)
    attendance_date = today()
    now_time = now_datetime()
    
    # Check if employee has remote work (pending or approved) for today
    remote_check = has_remote_for_date(employee, attendance_date)
    
    location_type = None
    office_location = None
    remote_req = None
    remote_status = None
    
    if remote_check["has_remote"]:
        # User has remote work request (pending or approved)
        remote_req = remote_check["request_name"]
        remote_status = remote_check["request_status"]
        location_type = "Remote"
        
        # For remote work, allow punch IN and OUT from anywhere
        # Still check if they happen to be in office for record keeping
        office_check = is_inside_office(employee, lat, lng)
        if office_check["inside"]:
            office_location = office_check["office"]
    else:
        # No remote request - must follow office location rules
        office_check = is_inside_office(employee, lat, lng)
        
        if not office_check["inside"]:
            if action == "IN":
                frappe.throw(_("You are not within any allowed office location. Please request remote work if working remotely."))
            else:
                # For OUT action, throw error - user should use request_punch_out_outside_office API instead
                frappe.throw(_("You must be within an office location to punch out when not working remotely. Please use the punch out outside office feature."))
        
        location_type = "Office"
        office_location = office_check["office"]

    if action == "IN":
        # Check if there's already an attendance log for TODAY (regardless of check_out status)
        # This allows users to punch in again even after completing a full cycle
        attendance_log_name = frappe.db.get_value(
            "Attendance Log",
            {
                "employee": employee, 
                "attendance_date": attendance_date  # Only check for today's date
            }
        )
        
        # Fallback: Also check by auto-generated name format in case query didn't find it
        if not attendance_log_name:
            expected_name = f"ATT-{employee}-{attendance_date}"
            if frappe.db.exists("Attendance Log", expected_name):
                attendance_log_name = expected_name
        
        if attendance_log_name:
            attendance_log = frappe.get_doc("Attendance Log", attendance_log_name)
            
            # Double-check that this is today's log (safety check)
            if attendance_log.attendance_date != attendance_date:
                # This shouldn't happen, but if it does, create a new log for today
                attendance_log_name = None
                attendance_log = None
            else:
                # If already punched in today (check_out is None)
                if not attendance_log.check_out:
                    # Allow updating punch-in if:
                    # 1. Status is "Pending Approval" AND
                    # 2. Remote request is pending
                    # This allows users to update their punch-in time when remote request is pending
                    if attendance_log.status == "Pending Approval" and remote_status == "Pending":
                        # Update the existing punch-in with new time and location
                        attendance_log.check_in = now_time
                        attendance_log.check_in_lat = lat
                        attendance_log.check_in_lng = lng
                        attendance_log.location_type = location_type
                        attendance_log.office_location = office_location
                        attendance_log.working_remote_req = remote_req
                        attendance_log.status = "Pending Approval"
                    else:
                        # Otherwise, throw error - user must punch out first
                        frappe.throw(_("You have already punched in today. Please punch out first."), exc=frappe.exceptions.DuplicateEntryError)
                else:
                    # Re-punching IN: Reset check_out and update check_in
                    attendance_log.check_out = None
                    attendance_log.check_out_lat = None
                    attendance_log.check_out_lng = None
                    attendance_log.check_in = now_time
                    attendance_log.check_in_lat = lat
                    attendance_log.check_in_lng = lng
                    attendance_log.location_type = location_type
                    attendance_log.office_location = office_location
                    attendance_log.working_remote_req = remote_req
                    
                    # Set status based on remote request approval status
                    if remote_status == "Pending":
                        attendance_log.status = "Pending Approval"
                    elif remote_status == "Approved":
                        attendance_log.status = "Present"
                    else:
                        attendance_log.status = "Present"
        
        if not attendance_log_name or attendance_log is None:
            # Check one more time if log exists before creating (race condition protection)
            expected_name = f"ATT-{employee}-{attendance_date}"
            if frappe.db.exists("Attendance Log", expected_name):
                # Log exists, load it instead of creating new
                attendance_log = frappe.get_doc("Attendance Log", expected_name)
                # Handle re-punching IN
                attendance_log.check_out = None
                attendance_log.check_out_lat = None
                attendance_log.check_out_lng = None
                attendance_log.check_in = now_time
                attendance_log.check_in_lat = lat
                attendance_log.check_in_lng = lng
                attendance_log.location_type = location_type
                attendance_log.office_location = office_location
                attendance_log.working_remote_req = remote_req
                if remote_status == "Pending":
                    attendance_log.status = "Pending Approval"
                elif remote_status == "Approved":
                    attendance_log.status = "Present"
                else:
                    attendance_log.status = "Present"
            else:
                # Create new attendance log
                attendance_log = frappe.get_doc({
                    "doctype": "Attendance Log",
                    "employee": employee,
                    "attendance_date": attendance_date,
                    "check_in": now_time,
                    "location_type": location_type,
                    "office_location": office_location,
                    "check_in_lat": lat,
                    "check_in_lng": lng,
                    "working_remote_req": remote_req,
                    "status": "Pending Approval" if remote_status == "Pending" else "Present"
                })
            
    elif action == "OUT":
        # Find today's attendance log with check_in but no check_out
        attendance_log_name = frappe.db.get_value(
            "Attendance Log",
            {
                "employee": employee,
                "attendance_date": attendance_date,
                "check_in": ["is", "set"],
                "check_out": ["is", "not set"]
            }
        )
        
        if not attendance_log_name:
            frappe.throw(_("No active punch-in found for today. Please punch in first."))
        
        # Update attendance log
        attendance_log = frappe.get_doc("Attendance Log", attendance_log_name)
        attendance_log.check_out = now_time
        attendance_log.check_out_lat = lat
        attendance_log.check_out_lng = lng
        
        # Update office_location if available (for remote workers who punch out from office)
        if office_location:
            attendance_log.office_location = office_location
        
        # Only update status if remote request is approved
        # If pending, keep as "Pending Approval" - will be updated when manager approves
        if attendance_log.status != "Pending Approval":
            # Calculate working hours (simple duration for now, but history tracking handles complex cases)
            check_in_time = get_datetime(attendance_log.check_in)
            check_out_time = get_datetime(attendance_log.check_out)
            duration_hours = (check_out_time - check_in_time).total_seconds() / 3600
            
            if duration_hours < 9:
                attendance_log.status = "Half Day"
            else:
                attendance_log.status = "Present"

    else:
        frappe.throw(_("Invalid action. Must be 'IN' or 'OUT'"))

    # Add to Punch History
    attendance_log.append("punch_history", {
        "punch_type": action,
        "punch_time": now_time,
        "latitude": lat,
        "longitude": lng,
        "location_type": location_type,
        "office_location": office_location
    })
    
    # Calculate total working hours from history
    total_hours = 0
    last_in_time = None
    
    # Sort history by time to be safe
    sorted_history = sorted(attendance_log.punch_history, key=lambda x: get_datetime(x.punch_time))
    
    for punch in sorted_history:
        if punch.punch_type == "IN":
            last_in_time = get_datetime(punch.punch_time)
        elif punch.punch_type == "OUT" and last_in_time:
            out_time = get_datetime(punch.punch_time)
            duration = (out_time - last_in_time).total_seconds() / 3600
            total_hours += duration
            last_in_time = None
            
    attendance_log.working_hours = round(total_hours, 2)
    
    # Don't update status to Present if it's Pending Approval
    # Status will be updated when manager approves the remote request
    if attendance_log.status != "Pending Approval":
        if attendance_log.working_hours < 4:
            attendance_log.status = "Half Day"
        else:
            attendance_log.status = "Present"
    
    # Save or insert the attendance log
    # New documents need insert(), existing documents need save()
    if not attendance_log.name:
        attendance_log.insert(ignore_permissions=True)
    else:
        attendance_log.save(ignore_permissions=True)
    frappe.db.commit()
    
    return attendance_log.as_dict()


@frappe.whitelist()
def request_punch_out_outside_office(employee, lat, lng, reason):
    """
    Request punch out outside office location with reason
    
    Args:
        employee: Employee ID
        lat: Latitude
        lng: Longitude
        reason: Reason for punching out outside office
    
    Returns:
        dict: Attendance Log and Punch Out Request documents
    """
    # Validate that employee belongs to current user
    current_user = frappe.session.user
    employee_user = frappe.db.get_value("Employee", employee, "user_id")
    
    if employee_user != current_user:
        frappe.throw(_("You can only mark attendance for yourself"))
    
    if not reason or not reason.strip():
        frappe.throw(_("Reason is required for punching out outside office"))
    
    lat = float(lat)
    lng = float(lng)
    attendance_date = today()
    now_time = now_datetime()
    
    # Check if employee has remote work for today
    remote_check = has_remote_for_date(employee, attendance_date)
    if remote_check["has_remote"]:
        frappe.throw(_("You have an active remote work request. Please use regular punch out."))
    
    # Find today's attendance log with check_in but no check_out
    attendance_log_name = frappe.db.get_value(
        "Attendance Log",
        {
            "employee": employee,
            "attendance_date": attendance_date,
            "check_in": ["is", "set"],
            "check_out": ["is", "not set"]
        }
    )
    
    if not attendance_log_name:
        frappe.throw(_("No active punch-in found for today. Please punch in first."))
    
    # Update attendance log
    attendance_log = frappe.get_doc("Attendance Log", attendance_log_name)
    attendance_log.check_out = now_time
    attendance_log.check_out_lat = lat
    attendance_log.check_out_lng = lng
    attendance_log.status = "Pending Approval"
    
    # Get employee's reporting manager as approver
    approver = frappe.db.get_value("Employee", employee, "reports_to")
    
    # Add to Punch History
    attendance_log.append("punch_history", {
        "punch_type": "OUT",
        "punch_time": now_time,
        "latitude": lat,
        "longitude": lng,
        "location_type": "Outside Office",
        "office_location": None
    })
    
    # Calculate total working hours from history
    total_hours = 0
    last_in_time = None
    
    # Sort history by time to be safe
    sorted_history = sorted(attendance_log.punch_history, key=lambda x: get_datetime(x.punch_time))
    
    for punch in sorted_history:
        if punch.punch_type == "IN":
            last_in_time = get_datetime(punch.punch_time)
        elif punch.punch_type == "OUT" and last_in_time:
            out_time = get_datetime(punch.punch_time)
            duration = (out_time - last_in_time).total_seconds() / 3600
            total_hours += duration
            last_in_time = None
    
    attendance_log.working_hours = round(total_hours, 2)
    
    # Save attendance log
    attendance_log.save(ignore_permissions=True)
    
    # Create Punch Out Request
    punch_out_request = frappe.get_doc({
        "doctype": "Punch Out Request",
        "employee": employee,
        "attendance_date": attendance_date,
        "attendance_log": attendance_log_name,
        "check_out_time": now_time,
        "reason": reason.strip(),
        "latitude": lat,
        "longitude": lng,
        "approver": approver,
        "status": "Pending"
    })
    punch_out_request.insert(ignore_permissions=True)
    
    frappe.db.commit()
    
    return {
        "attendance_log": attendance_log.as_dict(),
        "punch_out_request": punch_out_request.as_dict(),
        "message": "Punch out request submitted successfully. Waiting for manager approval."
    }


@frappe.whitelist()
def create_remote_request(employee, from_date, to_date, reason, lat=None, lng=None):
    """
    Create remote working request
    
    Args:
        employee: Employee ID
        from_date: Start date
        to_date: End date
        reason: Reason for remote work
        lat: Latitude (optional)
        lng: Longitude (optional)
    
    Returns:
        dict: Remote Working Request document
    """
    # Validate that employee belongs to current user
    current_user = frappe.session.user
    employee_user = frappe.db.get_value("Employee", employee, "user_id")
    
    if employee_user != current_user:
        frappe.throw(_("You can only create requests for yourself"))
    
    # Get employee's manager as approver
    manager = frappe.db.get_value("Employee", employee, "reports_to")
    
    # Create remote working request
    remote_request = frappe.get_doc({
        "doctype": "Remote Working Request",
        "employee": employee,
        "from_date": from_date,
        "to_date": to_date,
        "reason": reason,
        "request_lat": float(lat) if lat else None,
        "request_lng": float(lng) if lng else None,
        "approver": manager,
        "status": "Pending"
    })
    remote_request.insert(ignore_permissions=True)
    frappe.db.commit()
    
    return remote_request.as_dict()


@frappe.whitelist()
def apply_for_leave(employee, leave_type, from_date, to_date, reason):
    """
    Create leave application with validation based on leave type.
    
    Rules:
    - Casual Leave: Cannot apply if balance <= 0
    - Sick Leave: Can apply even if balance <= 0 (can go negative)
    - Compensatory Leave: No balance check (no deduction)
    
    Args:
        employee: Employee ID
        leave_type: Leave Type
        from_date: Start date
        to_date: End date
        reason: Reason for leave
    
    Returns:
        dict: Leave Application document
    """
    # Validate that employee belongs to current user (unless HR Admin or System user)
    current_user = frappe.session.user
    employee_user = frappe.db.get_value("Employee", employee, "user_id")
    
    # Allow if: user is the employee, or user is HR Admin/System Manager, or employee has no user_id
    is_hr_admin = "HR Admin" in frappe.get_roles(current_user) or "System Manager" in frappe.get_roles(current_user)
    
    if employee_user and employee_user != current_user and not is_hr_admin:
        frappe.throw(_("You can only apply for leave for yourself"))
    
    # Validate leave balance for Casual Leave
    if leave_type == "Casual Leave":
        balances = get_leave_balances(employee)
        casual_balance = next((b for b in balances if b["leave_type"] == "Casual Leave"), None)
        
        if casual_balance and casual_balance["remaining"] <= 0:
            frappe.throw(_("Insufficient Casual Leave balance. You have {0} leaves remaining.").format(casual_balance['remaining']))
    
    # Create leave application
    leave_application = frappe.get_doc({
        "doctype": "Leave Application",
        "employee": employee,
        "leave_type": leave_type,
        "from_date": from_date,
        "to_date": to_date,
        "description": reason,
        "status": "Open",
        "docstatus": 0,
        "posting_date": today()
    })
    
    leave_application.insert(ignore_permissions=True)
    frappe.db.commit()
    
    return leave_application.as_dict()


@frappe.whitelist()
def get_leave_balances(employee):
    """
    Get leave balances for employee - shows 3 separate leave types.
    
    Args:
        employee: Employee ID
    
    Returns:
        list: [{
            leave_type: str,
            allocated: float,
            used: float,
            remaining: float
        }]
    """
    from datetime import datetime
    
    # Get current year
    current_year = datetime.now().year
    year_start = f"{current_year}-01-01"
    year_end = f"{current_year}-12-31"
    
    leave_types = ["Casual Leave", "Sick Leave", "Compensatory Leave"]
    balances = []
    
    for leave_type in leave_types:
        # Get total allocated leaves for this type
        # Use COALESCE to get whichever field has a value (not both)
        allocated = frappe.db.sql("""
            SELECT COALESCE(SUM(COALESCE(total_leaves_allocated, new_leaves_allocated, 0)), 0)
            FROM `tabLeave Allocation`
            WHERE employee = %s
            AND leave_type = %s
            AND from_date <= %s
            AND to_date >= %s
            AND docstatus = 1
        """, (employee, leave_type, year_end, year_start))[0][0] or 0
        
        # Get used leaves (approved applications)
        # For Compensatory Leave, we don't deduct, so used = 0
        if leave_type == "Compensatory Leave":
            used = 0
        else:
            used = frappe.db.sql("""
                SELECT COALESCE(SUM(total_leave_days), 0)
                FROM `tabLeave Application`
                WHERE employee = %s
                AND leave_type = %s
                AND status = 'Approved'
                AND from_date >= %s
                AND to_date <= %s
                AND docstatus = 1
            """, (employee, leave_type, year_start, year_end))[0][0] or 0
        
        balances.append({
            "leave_type": leave_type,
            "allocated": float(allocated),
            "used": float(used),
            "remaining": float(allocated) - float(used)
        })
    
    return balances


@frappe.whitelist()
def approve_request(doctype, name, status):
    """
    Approve or reject a request (Remote Working Request, Attendance Regularization Request, Leave Application)
    
    Args:
        doctype: DocType name
        name: Document name
        status: "Approved" or "Rejected"
    
    Returns:
        dict: Updated document
    """
    if doctype not in ["Remote Working Request", "Attendance Regularization Request", "Leave Application", "Punch Out Request"]:
        frappe.throw(_("Invalid doctype"))
    
    if status not in ["Approved", "Rejected"]:
        frappe.throw(_("Invalid status"))
    
    # Get document
    doc = frappe.get_doc(doctype, name)
    
    # Check if current user is the approver
    current_user = frappe.session.user
    
    # Determine approver field name based on DocType
    approver_field = "approver"
    if doctype == "Leave Application":
        approver_field = "leave_approver"
        
    # Get approver ID from the document
    approver_id = getattr(doc, approver_field, None)
    
    # Get approver's user ID
    approver_user = None
    if approver_id:
        approver_user = frappe.db.get_value("Employee", approver_id, "user_id")
    
    # Allow HR Admin or Manager to approve any request
    roles = frappe.get_roles(current_user)
    is_authorized = "HR Admin" in roles or "Manager" in roles
    
    if not is_authorized and approver_user != current_user:
        frappe.throw(_("You are not authorized to approve this request"))
    
    # Get current user's employee ID for tracking
    current_employee = frappe.db.get_value("Employee", {"user_id": current_user}, "name")
    
    # Update status
    doc.status = status
    doc.save(ignore_permissions=True)
    
    # Track approval history using Frappe's comment system
    try:
        frappe.get_doc({
            "doctype": "Comment",
            "comment_type": "Comment",
            "reference_doctype": doctype,
            "reference_name": name,
            "content": f"Request {status.lower()} by {current_user}" + (f" (Employee: {current_employee})" if current_employee else ""),
            "comment_by": current_user
        }).insert(ignore_permissions=True)
    except:
        # If comment fails, continue anyway
        pass
    
    # If approved and submittable, submit the document
    if status == "Approved" and doc.meta.is_submittable:
        doc.submit()
    
    # If it's an attendance regularization request and approved, update attendance log
    if doctype == "Attendance Regularization Request" and status == "Approved":
        update_attendance_from_regularization(doc)
    
    # If it's a Remote Working Request, update related attendance logs
    if doctype == "Remote Working Request":
        update_attendance_from_remote_approval(doc, status)
    
    # If it's a Punch Out Request, update the attendance log status
    if doctype == "Punch Out Request":
        update_attendance_from_punch_out_approval(doc, status)
    
    frappe.db.commit()
    
    return doc.as_dict()


def update_attendance_from_remote_approval(remote_request, approval_status):
    """
    Update attendance logs when remote work request is approved or rejected
    
    Args:
        remote_request: Remote Working Request document
        approval_status: "Approved" or "Rejected"
    """
    # Find all attendance logs linked to this remote request
    attendance_logs = frappe.get_all(
        "Attendance Log",
        filters={"working_remote_req": remote_request.name},
        fields=["name", "attendance_date", "check_in", "check_out", "working_hours"]
    )
    
    for log_data in attendance_logs:
        attendance_log = frappe.get_doc("Attendance Log", log_data.name)
        
        if approval_status == "Approved":
            # Calculate status based on working hours
            if attendance_log.working_hours and attendance_log.working_hours < 4:
                attendance_log.status = "Half Day"
            else:
                attendance_log.status = "Present"
        elif approval_status == "Rejected":
            # Mark as Absent when remote request is rejected
            attendance_log.status = "Absent"
        
        attendance_log.save(ignore_permissions=True)


def update_attendance_from_punch_out_approval(punch_out_request, approval_status):
    """
    Update attendance log when punch out request is approved or rejected
    
    Args:
        punch_out_request: Punch Out Request document
        approval_status: "Approved" or "Rejected"
    """
    # Get the linked attendance log
    if not punch_out_request.attendance_log:
        return
    
    attendance_log = frappe.get_doc("Attendance Log", punch_out_request.attendance_log)
    
    if approval_status == "Approved":
        # Calculate status based on working hours
        if attendance_log.working_hours and attendance_log.working_hours < 4:
            attendance_log.status = "Half Day"
        elif attendance_log.working_hours and attendance_log.working_hours >= 4:
            attendance_log.status = "Present"
        else:
            # Fallback calculation
            if attendance_log.check_in and attendance_log.check_out:
                check_in_time = get_datetime(attendance_log.check_in)
                check_out_time = get_datetime(attendance_log.check_out)
                duration_hours = (check_out_time - check_in_time).total_seconds() / 3600
                if duration_hours < 9:
                    attendance_log.status = "Half Day"
                else:
                    attendance_log.status = "Present"
    elif approval_status == "Rejected":
        # When rejected, mark as Absent
        attendance_log.status = "Absent"
    
    attendance_log.save(ignore_permissions=True)


def update_attendance_from_regularization(regularization_doc):
    """
    Update or create attendance log from approved regularization request
    
    Args:
        regularization_doc: Attendance Regularization Request document
    """
    from frappe.utils import get_datetime
    
    # Check if attendance log exists for that date
    attendance_log_name = frappe.db.get_value(
        "Attendance Log",
        {
            "employee": regularization_doc.employee,
            "attendance_date": regularization_doc.attendance_date
        }
    )
    
    if attendance_log_name:
        # Update existing log
        attendance_log = frappe.get_doc("Attendance Log", attendance_log_name)
        current_status = attendance_log.status
        
        # Do NOT override if status is "On Leave"
        if current_status == "On Leave":
            # Still update check_in/check_out but don't change status
            if regularization_doc.requested_in:
                attendance_log.check_in = regularization_doc.requested_in
            if regularization_doc.requested_out:
                attendance_log.check_out = regularization_doc.requested_out
            attendance_log.save(ignore_permissions=True)
            return
        
        # Only override status if it's one of these: Absent, Half Day, P:A, A:P, or WR
        overrideable_statuses = ["Absent", "Half Day", "Pending Approval"]
        
        # Check for partial attendance (P:A or A:P)
        has_check_in = bool(attendance_log.check_in)
        has_check_out = bool(attendance_log.check_out)
        is_partial = (has_check_in and not has_check_out) or (not has_check_in and has_check_out)
        
        # Check if it's remote work
        is_remote = attendance_log.location_type and (
            attendance_log.location_type == "Remote" or 
            attendance_log.location_type == "Work From Home"
        )
        
        # Update check_in and check_out
        if regularization_doc.requested_in:
            attendance_log.check_in = regularization_doc.requested_in
        if regularization_doc.requested_out:
            attendance_log.check_out = regularization_doc.requested_out
        
        # Override status if it's in the overrideable list, is partial, or is remote
        if current_status in overrideable_statuses or is_partial or is_remote:
            # Calculate status based on check_in/check_out times
            if attendance_log.check_in and attendance_log.check_out:
                check_in_time = get_datetime(attendance_log.check_in)
                check_out_time = get_datetime(attendance_log.check_out)
                duration_hours = (check_out_time - check_in_time).total_seconds() / 3600
                
                # Preserve remote location_type if it was remote
                if is_remote and not attendance_log.location_type:
                    attendance_log.location_type = "Remote"
                
                # Set status based on working hours
                if duration_hours < 4:
                    attendance_log.status = "Half Day"
                else:
                    attendance_log.status = "Present"
            elif attendance_log.check_in or attendance_log.check_out:
                # Partial attendance - keep as Present but might need adjustment
                attendance_log.status = "Present"
            else:
                attendance_log.status = "Present"
        
        attendance_log.save(ignore_permissions=True)
    else:
        # Create new log
        attendance_log = frappe.get_doc({
            "doctype": "Attendance Log",
            "employee": regularization_doc.employee,
            "attendance_date": regularization_doc.attendance_date,
            "check_in": regularization_doc.requested_in,
            "check_out": regularization_doc.requested_out,
            "status": "Present"
        })
        
        # Calculate status based on working hours if both times are provided
        if regularization_doc.requested_in and regularization_doc.requested_out:
            check_in_time = get_datetime(regularization_doc.requested_in)
            check_out_time = get_datetime(regularization_doc.requested_out)
            duration_hours = (check_out_time - check_in_time).total_seconds() / 3600
            
            if duration_hours < 4:
                attendance_log.status = "Half Day"
            else:
                attendance_log.status = "Present"
        
        attendance_log.insert(ignore_permissions=True)


@frappe.whitelist()
def get_attendance_logs(employee, date=None):
    """
    Get attendance logs for employee
    
    Args:
        employee: Employee ID
        date: Optional date filter
    """
    # Validate that employee belongs to current user
    current_user = frappe.session.user
    employee_user = frappe.db.get_value("Employee", employee, "user_id")
    
    if employee_user != current_user:
        frappe.throw(_("You can only view your own attendance logs"))
    
    filters = {"employee": employee}
    if date:
        filters["attendance_date"] = date
        
    logs = frappe.get_all(
        "Attendance Log",
        filters=filters,
        fields=["name", "check_in", "check_out", "location_type", "status", "attendance_date", "working_hours"],
        order_by="attendance_date desc"
    )
    
    # Fetch punch history for each log
    for log in logs:
        log["punch_history"] = frappe.get_all(
            "Attendance Punch History",
            filters={"parent": log.name},
            fields=["punch_type", "punch_time", "location_type", "office_location", "latitude", "longitude"],
            order_by="punch_time asc"
        )
        
        # Ensure working_hours is set (for old logs or if calculation failed)
    for log in logs:
        if not log.working_hours:
            if log.check_in and log.check_out:
                check_in = get_datetime(log.check_in)
                check_out = get_datetime(log.check_out)
                duration = (check_out - check_in).total_seconds() / 3600
                log["working_hours"] = round(duration, 2)
            else:
                log["working_hours"] = 0
            
    return logs


@frappe.whitelist()
def get_office_locations():
    """
    Get all office locations
    """
    # Allow HR Admin, HR Manager, System Manager, and Employee to view
    # For now, we'll just return all active locations for employees, 
    # and all locations for Admins.
    
    filters = {}
    
    # If not admin, only show active
    roles = frappe.get_roles(frappe.session.user)
    if "HR Admin" not in roles and "System Manager" not in roles and "HR Manager" not in roles:
        filters["is_active"] = 1
        
    locations = frappe.get_all(
        "Office Location",
        filters=filters,
        fields=["name", "office_name", "company", "latitude", "longitude", "radius_meters", "is_active", "address"],
        order_by="creation desc"
    )
    
    
    return locations


@frappe.whitelist()
def manage_office_location(data):
    """
    Create or update office location
    """
    if isinstance(data, str):
        data = frappe.parse_json(data)
        
    if not frappe.db.exists("Company", data.get("company")):
        # If company doesn't exist, default to first company or create dummy
        # For this app context, we'll just ensure a company exists or use the first one
        companies = frappe.get_all("Company")
        if companies:
            data["company"] = companies[0].name
        else:
            # Create a default company if none exists
            c = frappe.get_doc({"doctype": "Company", "company_name": "Nexchar", "default_currency": "INR"})
            c.insert(ignore_permissions=True)
            data["company"] = "Nexchar"

    if data.get("name"):
        # Update
        doc = frappe.get_doc("Office Location", data.get("name"))
        doc.update(data)
        doc.save(ignore_permissions=True)
        return doc
    else:
        # Create
        doc = frappe.get_doc({
            "doctype": "Office Location",
            **data
        })
        doc.insert(ignore_permissions=True)
        return doc


@frappe.whitelist()
def get_pending_requests():
    """
    Get pending requests for current user (Manager/HR)
    
    Returns:
        dict: {
            leave_applications: list,
            remote_requests: list,
            regularization_requests: list
        }
    """
    current_user = frappe.session.user
    
    # Get roles first (needed for punch out requests check)
    roles = frappe.get_roles(current_user)
    
    # Get employee linked to this user
    employee = frappe.db.get_value("Employee", {"user_id": current_user}, "name")
    
    if not employee:
        # If not an employee, check if HR Admin
        if "HR Admin" not in roles:
             return {
                "leave_applications": [],
                "remote_requests": [],
                "regularization_requests": [],
                "punch_out_requests": []
            }
        # HR Admin sees all pending
        filters = {"status": "Open"}
        remote_filters = {"status": "Pending"}
    else:
        # Manager sees requests where they are the approver
        # For Leave Application, approver is usually determined by workflow or 'reports_to'
        # For simplicity, we'll fetch requests from employees who report to this employee
        
        # Get direct reports
        direct_reports = frappe.get_all("Employee", filters={"reports_to": employee}, pluck="name")
        
        if not direct_reports:
             return {
                "leave_applications": [],
                "remote_requests": [],
                "regularization_requests": [],
                "punch_out_requests": []
            }
            
        filters = {"employee": ["in", direct_reports], "status": "Open"}
        remote_filters = {"approver": employee, "status": "Pending"}

    # Fetch Leave Applications
    leave_applications = frappe.get_all(
        "Leave Application",
        filters=filters,
        fields=["name", "employee", "employee_name", "leave_type", "from_date", "to_date", "total_leave_days", "description", "status", "posting_date"],
        order_by="posting_date desc"
    )
    
    # Fetch Remote Working Requests
    remote_requests = frappe.get_all(
        "Remote Working Request",
        filters=remote_filters,
        fields=["name", "employee", "from_date", "to_date", "reason", "status", "creation"],
        order_by="creation desc"
    )
    
    # Manually fetch employee names for remote requests
    for req in remote_requests:
        req["employee_name"] = frappe.db.get_value("Employee", req.employee, "employee_name")
    
    # Fetch Regularization Requests (assuming similar structure/logic)
    regularization_requests = []
    if frappe.db.exists("DocType", "Attendance Regularization Request"):
         regularization_requests = frappe.get_all(
            "Attendance Regularization Request",
            filters=remote_filters, # Assuming similar approver field
            fields=["name", "employee", "attendance_date", "reason", "status", "creation"],
            order_by="creation desc"
        )
         for req in regularization_requests:
            req["employee_name"] = frappe.db.get_value("Employee", req.employee, "employee_name")
    
    # Fetch Punch Out Requests
    punch_out_requests = []
    if frappe.db.exists("DocType", "Punch Out Request"):
        if not employee or "HR Admin" in roles:
            # HR Admin sees all pending
            punch_out_filters = {"status": "Pending"}
        else:
            # Manager sees requests where they are the approver
            direct_reports = frappe.get_all("Employee", filters={"reports_to": employee}, pluck="name")
            if direct_reports:
                punch_out_filters = {"approver": employee, "status": "Pending"}
            else:
                # No direct reports, return empty
                punch_out_filters = {"status": "Pending", "employee": "NONEXISTENT"}
        
        punch_out_requests = frappe.get_all(
            "Punch Out Request",
            filters=punch_out_filters,
            fields=["name", "employee", "attendance_date", "attendance_log", "check_out_time", "reason", "latitude", "longitude", "status", "creation"],
            order_by="creation desc"
        )
        for req in punch_out_requests:
            req["employee_name"] = frappe.db.get_value("Employee", req.employee, "employee_name")
    
    return {
        "leave_applications": leave_applications,
        "remote_requests": remote_requests,
        "regularization_requests": regularization_requests,
        "punch_out_requests": punch_out_requests
    }


@frappe.whitelist()
def get_approval_history():
    """
    Get approval history for current user (Manager/HR Admin)
    Shows all approved/rejected requests they have processed
    
    Returns:
        dict: {
            leave_applications: list,
            remote_requests: list,
            regularization_requests: list
        }
    """
    current_user = frappe.session.user
    roles = frappe.get_roles(current_user)
    
    # Get employee linked to this user
    employee = frappe.db.get_value("Employee", {"user_id": current_user}, "name")
    
    if not employee and "HR Admin" not in roles:
        return {
            "leave_applications": [],
            "remote_requests": [],
            "regularization_requests": [],
            "punch_out_requests": []
        }
    
    # For HR Admin: get all approved/rejected requests
    # For Manager: get requests from their direct reports
    if "HR Admin" in roles:
        leave_filters = {"status": ["in", ["Approved", "Rejected"]]}
        remote_filters = {"status": ["in", ["Approved", "Rejected"]]}
        reg_filters = {"status": ["in", ["Approved", "Rejected"]]}
        punch_out_filters = {"status": ["in", ["Approved", "Rejected"]]}
    else:
        # Manager: get requests from direct reports
        direct_reports = frappe.get_all("Employee", filters={"reports_to": employee}, pluck="name")
        if not direct_reports:
            return {
                "leave_applications": [],
                "remote_requests": [],
                "regularization_requests": [],
                "punch_out_requests": []
            }
        leave_filters = {"employee": ["in", direct_reports], "status": ["in", ["Approved", "Rejected"]]}
        remote_filters = {"approver": employee, "status": ["in", ["Approved", "Rejected"]]}
        reg_filters = {"approver": employee, "status": ["in", ["Approved", "Rejected"]]}
        punch_out_filters = {"approver": employee, "status": ["in", ["Approved", "Rejected"]]}
    
    # Fetch Leave Applications (approved/rejected)
    leave_applications = frappe.get_all(
        "Leave Application",
        filters=leave_filters,
        fields=["name", "employee", "leave_type", "from_date", "to_date", "total_leave_days", "description", "status", "posting_date", "modified", "modified_by"],
        order_by="modified desc",
        limit=100
    )
    
    # Add employee_name and approver info to leave applications
    for leave in leave_applications:
        # Get employee_name from Employee table
        leave["employee_name"] = frappe.db.get_value("Employee", leave.employee, "employee_name")
        
        # Get the approver from modified_by or from comments
        approver_user = leave.modified_by
        if approver_user:
            approver_employee = frappe.db.get_value("Employee", {"user_id": approver_user}, ["name", "employee_name"], as_dict=True)
            if approver_employee:
                leave["approved_by"] = approver_employee.employee_name
                leave["approved_by_id"] = approver_employee.name
            else:
                leave["approved_by"] = approver_user
                leave["approved_by_id"] = None
        leave["approved_at"] = leave.modified
    
    # Fetch Remote Working Requests (approved/rejected)
    remote_requests = []
    if frappe.db.exists("DocType", "Remote Working Request"):
        remote_requests = frappe.get_all(
            "Remote Working Request",
            filters=remote_filters,
            fields=["name", "employee", "from_date", "to_date", "reason", "status", "creation", "modified", "modified_by"],
            order_by="modified desc",
            limit=100
        )
        
        # Add employee_name and approver info
        for req in remote_requests:
            # Get employee_name from Employee table
            req["employee_name"] = frappe.db.get_value("Employee", req.employee, "employee_name")
            
            approver_user = req.modified_by
            if approver_user:
                approver_employee = frappe.db.get_value("Employee", {"user_id": approver_user}, ["name", "employee_name"], as_dict=True)
                if approver_employee:
                    req["approved_by"] = approver_employee.employee_name
                    req["approved_by_id"] = approver_employee.name
                else:
                    req["approved_by"] = approver_user
                    req["approved_by_id"] = None
            req["approved_at"] = req.modified
    
    # Fetch Attendance Regularization Requests (approved/rejected)
    regularization_requests = []
    if frappe.db.exists("DocType", "Attendance Regularization Request"):
        regularization_requests = frappe.get_all(
            "Attendance Regularization Request",
            filters=reg_filters,
            fields=["name", "employee", "attendance_date", "reason", "status", "creation", "modified", "modified_by"],
            order_by="modified desc",
            limit=100
        )
        
        # Add employee_name and approver info
        for req in regularization_requests:
            # Get employee_name from Employee table
            req["employee_name"] = frappe.db.get_value("Employee", req.employee, "employee_name")
            
            approver_user = req.modified_by
            if approver_user:
                approver_employee = frappe.db.get_value("Employee", {"user_id": approver_user}, ["name", "employee_name"], as_dict=True)
                if approver_employee:
                    req["approved_by"] = approver_employee.employee_name
                    req["approved_by_id"] = approver_employee.name
                else:
                    req["approved_by"] = approver_user
                    req["approved_by_id"] = None
            req["approved_at"] = req.modified
    
    # Fetch Punch Out Requests (approved/rejected)
    punch_out_requests = []
    if frappe.db.exists("DocType", "Punch Out Request"):
        punch_out_requests = frappe.get_all(
            "Punch Out Request",
            filters=punch_out_filters,
            fields=["name", "employee", "attendance_date", "attendance_log", "check_out_time", "reason", "latitude", "longitude", "status", "creation", "modified", "modified_by"],
            order_by="modified desc",
            limit=100
        )
        
        # Add employee_name and approver info
        for req in punch_out_requests:
            # Get employee_name from Employee table
            req["employee_name"] = frappe.db.get_value("Employee", req.employee, "employee_name")
            
            approver_user = req.modified_by
            if approver_user:
                approver_employee = frappe.db.get_value("Employee", {"user_id": approver_user}, ["name", "employee_name"], as_dict=True)
                if approver_employee:
                    req["approved_by"] = approver_employee.employee_name
                    req["approved_by_id"] = approver_employee.name
                else:
                    req["approved_by"] = approver_user
                    req["approved_by_id"] = None
            req["approved_at"] = req.modified
    
    return {
        "leave_applications": leave_applications,
        "remote_requests": remote_requests,
        "regularization_requests": regularization_requests,
        "punch_out_requests": punch_out_requests
    }


@frappe.whitelist(allow_guest=True)
def get_api_keys(usr, pwd):
    """
    Authenticate user and return API keys.
    Generates keys if they don't exist.
    Supports login with both username and email.
    """
    # Check if usr is an email or username
    # If it doesn't contain @, treat it as a username and look up the email
    if '@' not in usr:
        # It's a username, find the corresponding email
        user_email = frappe.db.get_value("User", {"username": usr}, "name")
        if not user_email:
            frappe.throw(_("Invalid username or password"))
        usr = user_email
    
    try:
        login_manager = frappe.auth.LoginManager()
        login_manager.authenticate(user=usr, pwd=pwd)
    except Exception:
        frappe.throw(_("Invalid username or password"))
        
    user = frappe.get_doc("User", usr)
    
    # Generate API Key if missing
    if not user.api_key:
        api_key = frappe.generate_hash(length=15)
        user.api_key = api_key
        user.save(ignore_permissions=True)
    
    # Generate API Secret (we can't retrieve existing one, so we must generate new one if requested)
    # NOTE: This invalidates old secrets!
    api_secret = frappe.generate_hash(length=15)
    user.api_secret = api_secret
    user.save(ignore_permissions=True)
    frappe.db.commit()
    
    return {
        "api_key": user.api_key,
        "api_secret": api_secret
    }


@frappe.whitelist()
def get_employee_requests(doctype):
    """
    Get requests for the current employee
    """
    user = frappe.session.user
    employee = frappe.db.get_value("Employee", {"user_id": user}, "name")
    
    if not employee:
        return []
        
    if doctype not in ["Remote Working Request", "Leave Application", "Attendance Regularization Request"]:
        frappe.throw(_("Invalid doctype"))
        
    return frappe.get_all(
        doctype,
        filters={"employee": employee},
        fields=["*"],
        order_by="creation desc"
    )


@frappe.whitelist()
def create_employee(first_name, last_name, email, password, designation, gender, date_of_birth, date_of_joining, company, reports_to, office=None, offices=None, roles=None, holiday_list=None):
    """
    Create a new User and Employee document.
    
    Args:
        roles: List of user roles - must include "Employee", can also include "Manager" and/or "HR Admin"
              If not provided, defaults to ["Employee"]
        holiday_list: Holiday List name to assign to the employee (optional)
    """
    # Check permissions
    if "HR Admin" not in frappe.get_roles(frappe.session.user):
        frappe.throw(_("Not authorized"))

    # Handle roles - default to Employee if not provided
    if roles is None:
        roles = ["Employee"]
    
    # If roles is a string (single role), convert to list
    if isinstance(roles, str):
        roles = [roles]
    
    # Validate that Employee role is always included
    if "Employee" not in roles:
        roles.append("Employee")
    
    # Validate all roles
    valid_roles = ["Employee", "Manager", "HR Admin"]
    for role in roles:
        if role not in valid_roles:
            frappe.throw(_("Invalid role: {0}. Must be one of: Employee, Manager, HR Admin").format(role))

    # 1. Create User
    if frappe.db.exists("User", email):
        frappe.throw(_("User with this email already exists"))
    
    # Prepare roles for user assignment
    roles_to_assign = [{"role": role} for role in set(roles)]  # Use set to remove duplicates
        
    user = frappe.get_doc({
        "doctype": "User",
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "send_welcome_email": 0,
        "enabled": 1,
        "roles": roles_to_assign
    })
    user.new_password = password
    user.insert(ignore_permissions=True)
    
    # 2. Create Employee
    # Handle office locations - support both single office (backward compatibility) and multiple offices
    allowed_locations = []
    if offices and isinstance(offices, list) and len(offices) > 0:
        # Multiple offices provided
        for office_name in offices:
            if office_name:  # Skip empty values
                allowed_locations.append({"office": office_name})
    elif office:
        # Single office provided (backward compatibility)
        allowed_locations.append({"office": office})
    
    employee = frappe.get_doc({
        "doctype": "Employee",
        "first_name": first_name,
        "last_name": last_name,
        "user_id": email,
        "designation": designation,
        "gender": gender,
        "date_of_birth": date_of_birth,
        "date_of_joining": date_of_joining,
        "company": company,
        "reports_to": reports_to,
        "status": "Active",
        "allowed_locations": allowed_locations
    })
    
    # Set holiday_list if provided
    if holiday_list:
        # Validate that holiday_list exists
        if not frappe.db.exists("Holiday List", holiday_list):
            frappe.throw(_("Holiday List '{0}' does not exist").format(holiday_list))
        employee.holiday_list = holiday_list
    
    employee.insert(ignore_permissions=True)
    
    # 3. Auto-assign Manager role to the person in reports_to if they don't have it
    if reports_to:
        manager_employee = frappe.get_doc("Employee", reports_to)
        if manager_employee.user_id:
            manager_user = frappe.get_doc("User", manager_employee.user_id)
            manager_roles = [r.role for r in manager_user.roles]
            
            # If manager doesn't have Manager role, add it
            if "Manager" not in manager_roles:
                manager_user.append("roles", {"role": "Manager"})
                manager_user.save(ignore_permissions=True)
                frappe.db.commit()
    
    frappe.db.commit()
    
    return employee.as_dict()


@frappe.whitelist()
def update_employee(employee_id, first_name, last_name, email, designation, reports_to, status, office=None, offices=None, company=None, password=None, holiday_list=None):
    """
    Update Employee and User details.
    
    Args:
        holiday_list: Holiday List name to assign to the employee (optional, can be empty string to clear)
        offices: List of office location names (optional, for multiple locations)
        office: Single office location name (optional, for backward compatibility)
        company: Company name (optional, to change employee's company)
    """
    # Check permissions
    if "HR Admin" not in frappe.get_roles(frappe.session.user):
        frappe.throw(_("Not authorized"))

    # 1. Update Employee
    employee = frappe.get_doc("Employee", employee_id)
    employee.first_name = first_name
    employee.last_name = last_name
    employee.designation = designation
    employee.reports_to = reports_to
    employee.status = status
    
    # Update Company if provided
    if company:
        employee.company = company
    
    # Update Office Locations - support both single office (backward compatibility) and multiple offices
    if offices and isinstance(offices, list) and len(offices) > 0:
        # Multiple offices provided
        employee.allowed_locations = []
        for office_name in offices:
            if office_name:  # Skip empty values
                employee.append("allowed_locations", {"office": office_name})
    elif office:
        # Single office provided (backward compatibility)
        employee.allowed_locations = []
        employee.append("allowed_locations", {"office": office})
    
    # Update holiday_list if provided
    if holiday_list is not None:
        if holiday_list:  # If not empty string
            # Validate that holiday_list exists
            if not frappe.db.exists("Holiday List", holiday_list):
                frappe.throw(_("Holiday List '{0}' does not exist").format(holiday_list))
            employee.holiday_list = holiday_list
        else:  # Empty string means clear the holiday_list
            employee.holiday_list = None
        
    employee.save(ignore_permissions=True)
    
    # 2. Update User
    if employee.user_id:
        user = frappe.get_doc("User", employee.user_id)
        user.first_name = first_name
        user.last_name = last_name
        user.email = email
        
        if password:
            user.new_password = password
            
        user.save(ignore_permissions=True)
        
        # If email changed, update user_id in Employee
        if email != employee.user_id:
            rename_doc("User", employee.user_id, email, ignore_permissions=True)
            employee.user_id = email
            employee.save(ignore_permissions=True)

    frappe.db.commit()
    return employee.as_dict()


@frappe.whitelist()
def upload_profile_photo(content, filename, dt=None, dn=None, fieldname=None):
    """
    Upload a profile photo as a public file (is_private=0).
    This is a wrapper around the standard file upload but ensures the file is public.
    """
    import base64
    import io
    from mimetypes import guess_type
    
    from PIL import Image, ImageOps
    
    from frappe.handler import ALLOWED_MIMETYPES
    
    decoded_content = base64.b64decode(content)
    content_type = guess_type(filename)[0]
    if content_type not in ALLOWED_MIMETYPES:
        frappe.throw(_("You can only upload JPG, PNG, PDF, TXT or Microsoft documents."))
    
    if content_type.startswith("image/jpeg"):
        # transpose the image according to the orientation tag, and remove the orientation data
        with Image.open(io.BytesIO(decoded_content)) as image:
            transpose_img = ImageOps.exif_transpose(image)
            # convert the image back to bytes
            file_content = io.BytesIO()
            transpose_img.save(file_content, format="JPEG")
            file_content = file_content.getvalue()
    else:
        file_content = decoded_content
    
    file_doc = frappe.get_doc(
        {
            "doctype": "File",
            "attached_to_doctype": dt,
            "attached_to_name": dn,
            "attached_to_field": fieldname,
            "folder": "Home",
            "file_name": filename,
            "content": file_content,
            "is_private": 0,  # Set to public (not private)
        }
    ).insert()
    
    frappe.db.commit()
    return file_doc


@frappe.whitelist()
def update_employee_profile_photo(image_url):
    """
    Update the current user's employee profile photo.
    Only allows updating own profile photo.
    
    Args:
        image_url: URL of the image (can be external URL like dicebear or file URL)
    """
    user = frappe.session.user
    
    # Get employee linked to this user
    employee_id = frappe.db.get_value("Employee", {"user_id": user}, "name")
    if not employee_id:
        frappe.throw(_("No employee record found for current user"))
    
    # Update employee image
    employee = frappe.get_doc("Employee", employee_id)
    employee.image = image_url
    employee.save(ignore_permissions=True)
    
    frappe.db.commit()
    return {"success": True, "image_url": image_url}


@frappe.whitelist()
def get_employee_stats(employee, from_date, to_date):
    """
    Get attendance statistics for an employee within a date range.
    """
    # Check permissions (HR Admin or self)
    current_user = frappe.session.user
    roles = frappe.get_roles(current_user)
    
    if "HR Admin" not in roles:
        # Check if requesting for self
        linked_employee = frappe.db.get_value("Employee", {"user_id": current_user}, "name")
        if linked_employee != employee:
            frappe.throw(_("Not authorized"))

    logs = frappe.get_all(
        "Attendance Log",
        filters={
            "employee": employee,
            "attendance_date": ["between", [from_date, to_date]]
        },
        fields=["attendance_date", "check_in", "check_out", "status", "working_hours"],
        order_by="attendance_date desc"
    )
    
    total_present = 0
    total_absent = 0
    total_late = 0
    total_hours = 0.0
    
    # Calculate stats
    for log in logs:
        if log.status == "Present":
            total_present += 1
            if log.working_hours:
                total_hours += log.working_hours
                
            # Check for late entry (assuming 9:30 AM is late)
            if log.check_in:
                check_in_time = get_datetime(log.check_in).time()
                # Hardcoded late threshold for now, can be made configurable
                if check_in_time.hour > 9 or (check_in_time.hour == 9 and check_in_time.minute > 30):
                    total_late += 1
        elif log.status == "Absent":
            total_absent += 1
            
    avg_hours = total_hours / total_present if total_present > 0 else 0
    
    return {
        "total_present": total_present,
        "total_absent": total_absent,
        "total_late": total_late,
        "total_hours": round(total_hours, 2),
        "avg_hours": round(avg_hours, 2),
        "logs": logs
    }


@frappe.whitelist()
def get_notifications(limit=20):
    """
    Get notifications for current user about leave and remote work requests.
    
    For managers: pending requests that need approval
    For employees: status updates on their requests
    """
    current_user = frappe.session.user
    employee = frappe.db.get_value("Employee", {"user_id": current_user}, "name")
    
    if not employee:
        return []
    
    notifications = []
    
    # Check if user is a manager
    roles = frappe.get_roles(current_user)
    is_manager = "Manager" in roles or "HR Admin" in roles
    
    # For managers: get pending requests from their reportees
    if is_manager:
        # Pending leave requests - wrapped in try-except in case field names differ
        try:
            leave_requests = frappe.get_all(
                "Leave Application",
                filters={
                    "leave_approver": employee,
                    "status": "Open"
                },
                fields=["name", "employee", "employee_name", "from_date", "to_date", "leave_type", "creation"],
                order_by="creation desc",
                limit=limit
            )
            
            for leave in leave_requests:
                notifications.append({
                    "id": f"leave-{leave.name}",
                    "type": "leave_pending",
                    "title": "Leave Request Pending",
                    "message": f"{leave.employee_name} requested {leave.leave_type} from {leave.from_date} to {leave.to_date}",
                    "timestamp": leave.creation,
                    "link": "/approvals",
                    "unread": True
                })
        except Exception as e:
            # Leave Application schema might be different, skip
            print(f"Error fetching leave requests for manager: {e}")
            pass
        
        # Pending remote work requests (only if DocType exists)
        try:
            remote_requests = frappe.get_all(
                "Remote Work Request",
                filters={
                    "approver": employee,
                    "status": "Pending"
                },
                fields=["name", "employee", "employee_name", "from_date", "to_date", "creation"],
                order_by="creation desc",
                limit=limit
            )
            
            for remote in remote_requests:
                notifications.append({
                    "id": f"remote-{remote.name}",
                    "type": "remote_pending",
                    "title": "Remote Work Request Pending",
                    "message": f"{remote.employee_name} requested remote work from {remote.from_date} to {remote.to_date}",
                    "timestamp": remote.creation,
                    "link": "/approvals",
                    "unread": True
                })
        except Exception:
            # Remote Work Request DocType doesn't exist, skip
            pass
        
        # Pending punch out requests (only if DocType exists)
        try:
            if frappe.db.exists("DocType", "Punch Out Request"):
                punch_out_requests = frappe.get_all(
                    "Punch Out Request",
                    filters={
                        "approver": employee,
                        "status": "Pending"
                    },
                    fields=["name", "employee", "attendance_date", "check_out_time", "creation"],
                    order_by="creation desc",
                    limit=limit
                )
                
                for punch_out in punch_out_requests:
                    employee_name = frappe.db.get_value("Employee", punch_out.employee, "employee_name")
                    notifications.append({
                        "id": f"punchout-{punch_out.name}",
                        "type": "punchout_pending",
                        "title": "Punch Out Request Pending",
                        "message": f"{employee_name} requested punch out outside office on {punch_out.attendance_date}",
                        "timestamp": punch_out.creation,
                        "link": "/approvals",
                        "unread": True
                    })
        except Exception:
            # Punch Out Request DocType doesn't exist, skip
            pass
    
    # For employees: get status updates on their own requests
    # Leave requests (approved/rejected in last 7 days)
    try:
        employee_leaves = frappe.get_all(
            "Leave Application",
            filters={
                "employee": employee,
                "status": ["in", ["Approved", "Rejected"]],
                "modified": [">=", frappe.utils.add_days(today(), -7)]
            },
            fields=["name", "status", "from_date", "to_date", "leave_type", "modified"],
            order_by="modified desc",
            limit=limit
        )
        
        for leave in employee_leaves:
            status_text = "approved" if leave.status == "Approved" else "rejected"
            notifications.append({
                "id": f"leave-status-{leave.name}",
                "type": f"leave_{status_text}",
                "title": f"Leave Request {leave.status}",
                "message": f"Your {leave.leave_type} from {leave.from_date} to {leave.to_date} was {status_text}",
                "timestamp": leave.modified,
                "link": "/leaves",
                "unread": True
            })
    except Exception as e:
        # Leave Application schema might be different, skip
        print(f"Error fetching employee leave status: {e}")
        pass
    
    # Remote work requests (approved/rejected in last 7 days) - only if DocType exists
    try:
        employee_remote = frappe.get_all(
            "Remote Work Request",
            filters={
                "employee": employee,
                "status": ["in", ["Approved", "Rejected"]],
                "modified": [">=", frappe.utils.add_days(today(), -7)]
            },
            fields=["name", "status", "from_date", "to_date", "modified"],
            order_by="modified desc",
            limit=limit
        )
        
        for remote in employee_remote:
            status_text = "approved" if remote.status == "Approved" else "rejected"
            notifications.append({
                "id": f"remote-status-{remote.name}",
                "type": f"remote_{status_text}",
                "title": f"Remote Work Request {remote.status}",
                "message": f"Your remote work request from {remote.from_date} to {remote.to_date} was {status_text}",
                "timestamp": remote.modified,
                "link": "/",
                "unread": True
            })
    except Exception:
        # Remote Work Request DocType doesn't exist, skip
        pass
    
    # Punch out requests (approved/rejected in last 7 days) - only if DocType exists
    try:
        if frappe.db.exists("DocType", "Punch Out Request"):
            employee_punch_out = frappe.get_all(
                "Punch Out Request",
                filters={
                    "employee": employee,
                    "status": ["in", ["Approved", "Rejected"]],
                    "modified": [">=", frappe.utils.add_days(today(), -7)]
                },
                fields=["name", "status", "attendance_date", "check_out_time", "modified"],
                order_by="modified desc",
                limit=limit
            )
            
            for punch_out in employee_punch_out:
                status_text = "approved" if punch_out.status == "Approved" else "rejected"
                notifications.append({
                    "id": f"punchout-status-{punch_out.name}",
                    "type": f"punchout_{status_text}",
                    "title": f"Punch Out Request {punch_out.status}",
                    "message": f"Your punch out request for {punch_out.attendance_date} was {status_text}",
                    "timestamp": punch_out.modified,
                    "link": "/attendance/logs",
                    "unread": True
                })
    except Exception:
        # Punch Out Request DocType doesn't exist, skip
        pass
    
    # Sort by timestamp descending
    notifications.sort(key=lambda x: x["timestamp"], reverse=True)
    
    # Get read notification IDs for current user
    read_notifications = frappe.cache.hget("read_notifications", current_user) or []
    if not isinstance(read_notifications, list):
        read_notifications = []
    
    # Mark notifications as read/unread based on stored read status
    for notification in notifications:
        notification["unread"] = notification["id"] not in read_notifications
    
    return notifications[:limit]

@frappe.whitelist()
def mark_notification_as_read(notification_id):
    """
    Mark a notification as read for the current user
    
    Args:
        notification_id: The ID of the notification to mark as read
    
    Returns:
        dict: Success message
    """
    current_user = frappe.session.user
    
    # Get existing read notifications
    read_notifications = frappe.cache.hget("read_notifications", current_user) or []
    if not isinstance(read_notifications, list):
        read_notifications = []
    
    # Add notification ID if not already in list
    if notification_id not in read_notifications:
        read_notifications.append(notification_id)
        # Store in cache
        frappe.cache.hset("read_notifications", current_user, read_notifications)
    
    return {"message": "Notification marked as read"}


@frappe.whitelist()
def get_employee_leave_allocations(employee_id):
    """
    Get all leave allocations for an employee.
    
    Args:
        employee_id: Employee ID
    
    Returns:
        list: Leave allocations with details
    """
    allocations = frappe.get_all(
        "Leave Allocation",
        filters={"employee": employee_id},
        fields=[
            "name", "leave_type", "from_date", "to_date", 
            "total_leaves_allocated", "new_leaves_allocated", 
            "docstatus", "creation"
        ],
        order_by="creation desc"
    )
    
    result = []
    for alloc in allocations:
        # Get used leaves for this allocation period
        used = frappe.db.sql("""
            SELECT COALESCE(SUM(total_leave_days), 0)
            FROM `tabLeave Application`
            WHERE employee = %s
            AND leave_type = %s
            AND status = 'Approved'
            AND from_date >= %s
            AND to_date <= %s
            AND docstatus = 1
        """, (employee_id, alloc.leave_type, alloc.from_date, alloc.to_date))[0][0] or 0
        
        result.append({
            "name": alloc.name,
            "leave_type": alloc.leave_type,
            "from_date": alloc.from_date,
            "to_date": alloc.to_date,
            "total_allocated": alloc.total_leaves_allocated or alloc.new_leaves_allocated or 0,
            "used": float(used),
            "remaining": (alloc.total_leaves_allocated or alloc.new_leaves_allocated or 0) - float(used),
            "status": "Active" if alloc.docstatus == 1 else "Draft"
        })
    
    return result


@frappe.whitelist()
def allocate_leave(employee_id, leave_type, from_date, to_date, total_leaves):
    """
    Create a new leave allocation for an employee.
    Only HR Admins can allocate leaves.
    """
    # Check permissions
    if "HR Admin" not in frappe.get_roles(frappe.session.user):
        frappe.throw(_("Not authorized to allocate leaves"))
    
    # Create Leave Allocation
    allocation = frappe.get_doc({
        "doctype": "Leave Allocation",
        "employee": employee_id,
        "leave_type": leave_type,
        "from_date": from_date,
        "to_date": to_date,
        "new_leaves_allocated": float(total_leaves),
        "description": f"Allocated by {frappe.session.user}"
    })
    
    allocation.insert(ignore_permissions=True)
    allocation.submit()
    frappe.db.commit()
    
    return {
        "success": True,
        "message": "Leave allocation created successfully",
        "allocation_id": allocation.name
    }


@frappe.whitelist()
def update_leave_allocation(allocation_id, total_leaves):
    """
    Update an existing leave allocation.
    Only HR Admins can update allocations.
    """
    # Check permissions
    if "HR Admin" not in frappe.get_roles(frappe.session.user):
        frappe.throw(_("Not authorized to update leave allocations"))
    
    # Get existing allocation
    allocation = frappe.get_doc("Leave Allocation", allocation_id)
    
    # Cancel the old allocation
    allocation.cancel()
    
    # Create new allocation with updated leaves
    new_allocation = frappe.get_doc({
        "doctype": "Leave Allocation",
        "employee": allocation.employee,
        "leave_type": allocation.leave_type,
        "from_date": allocation.from_date,
        "to_date": allocation.to_date,
        "new_leaves_allocated": float(total_leaves),
        "description": f"Updated by {frappe.session.user} (Original: {allocation.name})"
    })
    
    new_allocation.insert(ignore_permissions=True)
    new_allocation.submit()
    frappe.db.commit()
    
    return {
        "success": True,
        "message": "Leave allocation updated successfully",
        "allocation_id": new_allocation.name
    }


@frappe.whitelist()
def setup_default_leave_types():
    """
    Create default leave types if they don't exist.
    Only HR Admins can set up leave types.
    
    3 Leave Types:
    1. Casual Leave - 1/month, cannot go negative
    2. Sick Leave - 1/month, can go negative
    3. Compensatory Leave - request-based, no deduction
    """
    # Check permissions
    if "HR Admin" not in frappe.get_roles(frappe.session.user):
        frappe.throw(_("Not authorized to set up leave types"))
    
    default_leave_types = [
        {
            "name": "Casual Leave",
            "max_leaves_allowed": 0,
            "is_carry_forward": 1,
            "allow_negative": 0,  # Cannot go negative
            "include_holiday": 0,
            "is_lwp": 0
        },
        {
            "name": "Sick Leave",
            "max_leaves_allowed": 0,
            "is_carry_forward": 1,
            "allow_negative": 1,  # Can go negative
            "include_holiday": 0,
            "is_lwp": 0
        },
        {
            "name": "Compensatory Leave",
            "max_leaves_allowed": 0,
            "is_carry_forward": 0,
            "allow_negative": 0,
            "include_holiday": 0,
            "is_lwp": 0  # Not LWP, but special handling (no deduction)
        }
    ]
    
    created = []
    
    for leave_type_data in default_leave_types:
        # Check if leave type already exists
        if not frappe.db.exists("Leave Type", leave_type_data["name"]):
            leave_type = frappe.get_doc({
                "doctype": "Leave Type",
                **leave_type_data
            })
            leave_type.insert(ignore_permissions=True)
            created.append(leave_type_data["name"])
    
    frappe.db.commit()
    
    return {
        "success": True,
        "message": f"Created {len(created)} leave types" if created else "All leave types already exist",
        "created": created
    }


@frappe.whitelist()
def allocate_monthly_leaves():
    """
    Allocate 1 Casual Leave and 1 Sick Leave to all active employees for the current month.
    Should be run on the 1st of every month.
    """
    from datetime import datetime
    import calendar
    
    # Get current month dates
    now = datetime.now()
    year = now.year
    month = now.month
    
    # First and last day of current month
    _, last_day = calendar.monthrange(year, month)
    from_date = f"{year}-{month:02d}-01"
    to_date = f"{year}-{month:02d}-{last_day}"
    
    # Get all active employees
    employees = frappe.get_all("Employee", filters={"status": "Active"}, fields=["name", "employee_name"])
    
    allocations_created = 0
    
    for emp in employees:
        # 1. Allocate Casual Leave
        if not frappe.db.exists("Leave Allocation", {
            "employee": emp.name,
            "leave_type": "Casual Leave",
            "from_date": from_date,
            "to_date": to_date
        }):
            doc = frappe.get_doc({
                "doctype": "Leave Allocation",
                "employee": emp.name,
                "employee_name": emp.employee_name,
                "leave_type": "Casual Leave",
                "from_date": from_date,
                "to_date": to_date,
                "new_leaves_allocated": 1,
                "total_leaves_allocated": 0, # Explicitly 0 to avoid double counting
                "docstatus": 1 # Submit immediately
            })
            doc.insert(ignore_permissions=True)
            allocations_created += 1
            
        # 2. Allocate Sick Leave
        if not frappe.db.exists("Leave Allocation", {
            "employee": emp.name,
            "leave_type": "Sick Leave",
            "from_date": from_date,
            "to_date": to_date
        }):
            doc = frappe.get_doc({
                "doctype": "Leave Allocation",
                "employee": emp.name,
                "employee_name": emp.employee_name,
                "leave_type": "Sick Leave",
                "from_date": from_date,
                "to_date": to_date,
                "new_leaves_allocated": 1,
                "total_leaves_allocated": 0, # Explicitly 0 to avoid double counting
                "docstatus": 1 # Submit immediately
            })
            doc.insert(ignore_permissions=True)
            allocations_created += 1
            
    frappe.db.commit()
    return {"message": f"Allocated leaves for {len(employees)} employees. Total allocations: {allocations_created}"}

@frappe.whitelist()
def get_attendance_logs(employee=None, month=None, year=None):
    """
    Get attendance logs for a specific month/year
    """
    from datetime import datetime
    
    current_user = frappe.session.user
    print(f"DEBUG: get_attendance_logs called with employee={employee}, user={current_user}")
    
    # If employee is not provided, get employee for current user
    if not employee:
        employee = frappe.db.get_value("Employee", {"user_id": current_user}, "name")
        if not employee:
            frappe.throw(_("No employee found for the current user"))
    
    # Validate that employee belongs to current user
    employee_user = frappe.db.get_value("Employee", employee, "user_id")
    
    if employee_user != current_user:
        frappe.throw(_("You can only view your own attendance logs"))
    
    if not month or not year:
        now = datetime.now()
        month = now.month
        year = now.year
        
    start_date = f"{year}-{int(month):02d}-01"
    # Calculate end date
    import calendar
    _, last_day = calendar.monthrange(int(year), int(month))
    end_date = f"{year}-{int(month):02d}-{last_day}"
    
    logs = frappe.get_all("Attendance Log",
        filters={
            "employee": employee,
            "attendance_date": ["between", [start_date, end_date]]
        },
        fields=["name", "attendance_date", "check_in", "check_out", "status", "working_hours", "location_type"],
        order_by="attendance_date asc"
    )
    
    # Fetch punch history for each log
    for log in logs:
        log["punch_history"] = frappe.get_all(
            "Attendance Punch History",
            filters={"parent": log.name},
            fields=["punch_type", "punch_time", "location_type", "office_location"],
            order_by="punch_time asc"
        )
    
    return logs


@frappe.whitelist()
def get_all_employees_attendance_for_date(date):
    """
    Get all employees' attendance logs for a specific date
    Only accessible by HR Admin or Manager
    
    Args:
        date: Date in YYYY-MM-DD format
    
    Returns:
        list: List of attendance logs with employee information
    """
    current_user = frappe.session.user
    roles = frappe.get_roles(current_user)
    
    # Check permissions
    if "HR Admin" not in roles and "Manager" not in roles:
        frappe.throw(_("Not authorized. Only HR Admin or Manager can view all employees' attendance."))
    
    # Get employee linked to current user
    current_employee = frappe.db.get_value("Employee", {"user_id": current_user}, "name")
    
    # For Manager: get only direct reports
    # For HR Admin: get all active employees
    if "HR Admin" in roles:
        employee_filters = {"status": "Active"}
    else:
        # Manager: get direct reports
        direct_reports = frappe.get_all("Employee", filters={"reports_to": current_employee}, pluck="name")
        if not direct_reports:
            return []
        employee_filters = {"name": ["in", direct_reports], "status": "Active"}
    
    # Get all active employees (or direct reports for manager)
    employees = frappe.get_all(
        "Employee",
        filters=employee_filters,
        fields=["name", "employee_name"]
    )
    
    if not employees:
        return []
    
    employee_ids = [emp.name for emp in employees]
    
    # Get attendance logs for the date
    logs = frappe.get_all(
        "Attendance Log",
        filters={
            "employee": ["in", employee_ids],
            "attendance_date": date
        },
        fields=["name", "employee", "attendance_date", "check_in", "check_out", "status", "working_hours", "location_type"],
        order_by="employee asc"
    )
    
    # Fetch punch history for each log
    for log in logs:
        log["punch_history"] = frappe.get_all(
            "Attendance Punch History",
            filters={"parent": log.name},
            fields=["punch_type", "punch_time", "location_type", "office_location"],
            order_by="punch_time asc"
        )
    
    # Create a map of employee_id to employee info
    employee_map = {emp.name: emp for emp in employees}
    
    # Add employee information to each log
    for log in logs:
        emp_info = employee_map.get(log.employee, {})
        log["employee_name"] = emp_info.get("employee_name", "")
        log["employee_id"] = log.employee  # Use employee name as ID
    
    # Also include employees who don't have attendance logs (marked as absent or no record)
    logged_employee_ids = {log.employee for log in logs}
    missing_employees = [emp for emp in employees if emp.name not in logged_employee_ids]
    
    # Add missing employees as "No Record" entries
    for emp in missing_employees:
        logs.append({
            "employee": emp.name,
            "employee_name": emp.employee_name,
            "employee_id": emp.name,  # Use employee name as ID
            "attendance_date": date,
            "check_in": None,
            "check_out": None,
            "status": "Absent",
            "working_hours": 0,
            "location_type": None,
            "punch_history": []
        })
    
    # Sort by employee name
    logs.sort(key=lambda x: x.get("employee_name", ""))
    
    return logs

@frappe.whitelist()
def apply_for_regularization(employee=None, attendance_date=None, check_in=None, check_out=None, reason=None):
    """
    Create an Attendance Regularization Request
    """
    current_user = frappe.session.user
    
    # If employee is not provided (or passed as null/None from frontend), get employee for current user
    if not employee:
        employee = frappe.db.get_value("Employee", {"user_id": current_user}, "name")
        if not employee:
            frappe.throw(_("No employee found for the current user"))

    # Validate that employee belongs to current user
    employee_user = frappe.db.get_value("Employee", employee, "user_id")
    
    # Allow if: user is the employee, or user is HR Admin/System Manager/Manager
    roles = frappe.get_roles(current_user)
    is_authorized = "HR Admin" in roles or "System Manager" in roles or "Manager" in roles
    
    if employee_user and employee_user != current_user and not is_authorized:
        frappe.throw(_("You can only apply for regularization for yourself"))
        
    # Check if request already exists
    existing = frappe.db.exists("Attendance Regularization Request", {
        "employee": employee,
        "attendance_date": attendance_date,
        "status": ["in", ["Pending", "Approved"]]
    })
    
    if existing:
        frappe.throw(_("A regularization request already exists for this date"))
        
    # Get approver (Reports To)
    approver = frappe.db.get_value("Employee", employee, "reports_to")
    
    # Combine date and time
    if check_in and len(check_in) <= 5: # Format HH:MM
        check_in = f"{attendance_date} {check_in}:00"
    
    if check_out and len(check_out) <= 5: # Format HH:MM
        check_out = f"{attendance_date} {check_out}:00"

    doc = frappe.get_doc({
        "doctype": "Attendance Regularization Request",
        "employee": employee,
        "attendance_date": attendance_date,
        "requested_in": check_in,
        "requested_out": check_out,
        "reason": reason,
        "approver": approver,
        "status": "Pending"
    })
    
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    
    return doc.as_dict()

@frappe.whitelist()
def get_regularization_requests(employee=None):
    """
    Get regularization requests for an employee
    """
    current_user = frappe.session.user
    print(f"DEBUG: get_regularization_requests called with employee={employee}, user={current_user}")
    
    # If employee is not provided, get employee for current user
    if not employee:
        employee = frappe.db.get_value("Employee", {"user_id": current_user}, "name")
        print(f"DEBUG: Resolved employee for user {current_user}: {employee}")
        if not employee:
            print("DEBUG: No employee found for user")
            return [] # Return empty if no employee found
            
    requests = frappe.get_all("Attendance Regularization Request",
        filters={"employee": employee},
        fields=["name", "attendance_date", "requested_in", "requested_out", "reason", "status", "approver"],
        order_by="creation desc"
    )
    
    return requests

@frappe.whitelist()
def get_holidays_for_month(employee=None, month=None, year=None):
    """
    Get holidays for an employee for a specific month from the company's holiday list
    
    Args:
        employee: Employee ID (optional, defaults to current user's employee)
        month: Month number (1-12)
        year: Year (e.g., 2024)
    
    Returns:
        list: List of holiday dates in YYYY-MM-DD format
    """
    from datetime import datetime
    import calendar
    
    current_user = frappe.session.user
    
    # If employee is not provided, get employee for current user
    if not employee:
        employee = frappe.db.get_value("Employee", {"user_id": current_user}, "name")
        if not employee:
            return []
    
    # Validate that employee belongs to current user
    employee_user = frappe.db.get_value("Employee", employee, "user_id")
    if employee_user != current_user:
        frappe.throw(_("You can only view your own holidays"))
    
    # Get employee's company
    company = frappe.db.get_value("Employee", employee, "company")
    if not company:
        return []
    
    # Priority 1: Get holiday list from employee's holiday_list field (supports per-employee schedules)
    holiday_list = frappe.db.get_value("Employee", employee, "holiday_list")
    if not holiday_list:
        # Priority 2: Fallback to company's default_holiday_list
        holiday_list = frappe.get_cached_value("Company", company, "default_holiday_list")
        if not holiday_list:
            print(f"DEBUG: No holiday list found for employee {employee} or company {company}")
            return []
    
    # Calculate start and end dates for the month
    if not month or not year:
        now = datetime.now()
        month = now.month
        year = now.year
    
    # Get first and last day of month
    _, last_day = calendar.monthrange(int(year), int(month))
    start_date = f"{year}-{int(month):02d}-01"
    end_date = f"{year}-{int(month):02d}-{last_day}"
    
    # Get holidays for the month
    holidays = frappe.get_all(
        "Holiday",
        filters={
            "parent": holiday_list,
            "holiday_date": ["between", [start_date, end_date]]
        },
        fields=["holiday_date"],
        order_by="holiday_date asc"
    )
    
    # Return list of holiday dates as strings in YYYY-MM-DD format
    holiday_dates = []
    for h in holidays:
        if h.holiday_date:
            # Convert to string format YYYY-MM-DD
            if isinstance(h.holiday_date, str):
                # If it's already a string, use it directly (might be in YYYY-MM-DD format)
                holiday_dates.append(h.holiday_date.split(' ')[0])  # Take date part if time included
            else:
                # It's a date object, format it
                holiday_dates.append(h.holiday_date.strftime("%Y-%m-%d"))
    
    print(f"DEBUG: get_holidays_for_month - employee={employee}, company={company}, holiday_list={holiday_list}, month={month}, year={year}")
    print(f"DEBUG: Found {len(holiday_dates)} holidays: {holiday_dates}")
    
    return holiday_dates


@frappe.whitelist()
def get_attendance_report_for_csv(from_date=None, to_date=None, employees=None):
    """
    Get all employee attendance data for CSV export (HR Admin only)
    
    Args:
        from_date: Start date (YYYY-MM-DD format, optional, defaults to 30 days ago)
        to_date: End date (YYYY-MM-DD format, optional, defaults to today)
        employees: List of employee IDs to filter (optional, if None or empty, gets all active employees)
    
    Returns:
        list: List of attendance records with employee info, dates, check in/out, hours, and status
    """
    # Check permissions - only HR Admin can access
    if "HR Admin" not in frappe.get_roles(frappe.session.user):
        frappe.throw(_("Not authorized. Only HR Admin can access attendance reports."))
    
    from datetime import datetime, timedelta
    from frappe.utils import getdate
    
    # Set default date range if not provided
    if not to_date:
        to_date = today()
    if not from_date:
        from_date = (getdate(to_date) - timedelta(days=30)).strftime("%Y-%m-%d")
    
    # Handle employees parameter - can be list or string
    employee_filter = {"status": "Active"}
    if employees:
        if isinstance(employees, str):
            # Single employee ID
            employee_filter["name"] = employees
        elif isinstance(employees, list) and len(employees) > 0:
            # Multiple employee IDs
            employee_filter["name"] = ["in", employees]
    
    # Get employees based on filter
    employees_list = frappe.get_all(
        "Employee",
        filters=employee_filter,
        fields=["name", "employee_name"]
    )
    
    if not employees_list:
        return []
    
    # Get employee IDs for filtering attendance logs
    employee_ids = [emp.name for emp in employees_list]
    
    # Get all attendance logs in the date range for selected employees
    attendance_logs = frappe.get_all(
        "Attendance Log",
        filters={
            "attendance_date": ["between", [from_date, to_date]],
            "employee": ["in", employee_ids]
        },
        fields=[
            "name", "employee", "attendance_date", "check_in", "check_out", 
            "status", "working_hours", "location_type"
        ],
        order_by="attendance_date asc, employee asc"
    )
    
    # Create a dictionary for quick lookup
    logs_by_employee_date = {}
    for log in attendance_logs:
        key = f"{log.employee}_{log.attendance_date}"
        logs_by_employee_date[key] = log
    
    # Get all leave applications in the date range for selected employees
    leave_applications = frappe.get_all(
        "Leave Application",
        filters={
            "docstatus": 1,
            "status": "Approved",
            "from_date": ["<=", to_date],
            "to_date": [">=", from_date],
            "employee": ["in", employee_ids]
        },
        fields=["employee", "from_date", "to_date", "leave_type", "half_day", "half_day_date"]
    )
    
    # Create leave dates dictionary
    leave_dates = {}
    for leave in leave_applications:
        from_dt = getdate(leave.from_date)
        to_dt = getdate(leave.to_date)
        current_date = from_dt
        while current_date <= to_dt:
            date_str = current_date.strftime("%Y-%m-%d")
            if date_str >= from_date and date_str <= to_date:
                if leave.half_day and leave.half_day_date:
                    # Half day leave
                    if getdate(leave.half_day_date) == current_date:
                        leave_dates[f"{leave.employee}_{date_str}"] = {"type": "Half Day Leave", "leave_type": leave.leave_type}
                    else:
                        leave_dates[f"{leave.employee}_{date_str}"] = {"type": "On Leave", "leave_type": leave.leave_type}
                else:
                    leave_dates[f"{leave.employee}_{date_str}"] = {"type": "On Leave", "leave_type": leave.leave_type}
            current_date += timedelta(days=1)
    
    # Get holiday lists for selected employees
    employee_holidays = {}
    for emp in employees_list:
        # Get employee's holiday list
        holiday_list = frappe.db.get_value("Employee", emp.name, "holiday_list")
        if not holiday_list:
            company = frappe.db.get_value("Employee", emp.name, "company")
            if company:
                holiday_list = frappe.get_cached_value("Company", company, "default_holiday_list")
        
        if holiday_list:
            holidays = frappe.get_all(
                "Holiday",
                filters={
                    "parent": holiday_list,
                    "holiday_date": ["between", [from_date, to_date]]
                },
                fields=["holiday_date"],
                pluck="holiday_date"
            )
            for holiday_date in holidays:
                if isinstance(holiday_date, str):
                    date_str = holiday_date.split(' ')[0]
                else:
                    date_str = holiday_date.strftime("%Y-%m-%d")
                if date_str >= from_date and date_str <= to_date:
                    employee_holidays[f"{emp.name}_{date_str}"] = True
    
    # Build report data
    report_data = []
    
    # Generate date range
    from_dt = getdate(from_date)
    to_dt = getdate(to_date)
    current_date = from_dt
    
    while current_date <= to_dt:
        date_str = current_date.strftime("%Y-%m-%d")
        
        for emp in employees_list:
            key = f"{emp.name}_{date_str}"
            log = logs_by_employee_date.get(key)
            leave_info = leave_dates.get(key)
            is_holiday = employee_holidays.get(key, False)
            
            # Determine status code
            status_code = "A"  # Default to Absent
            
            if is_holiday:
                status_code = "O"
            elif leave_info:
                if leave_info["type"] == "Half Day Leave":
                    status_code = "HD"
                else:
                    status_code = "L"
            elif log:
                has_check_in = bool(log.check_in)
                has_check_out = bool(log.check_out)
                
                if log.location_type and (log.location_type == "Remote" or log.location_type == "Work From Home"):
                    status_code = "WR"
                elif log.status == "Present":
                    if has_check_in and not has_check_out:
                        status_code = "P:A"
                    elif not has_check_in and has_check_out:
                        status_code = "A:P"
                    else:
                        status_code = "P"
                elif log.status == "Half Day":
                    status_code = "HD"
                elif log.status == "Absent":
                    status_code = "A"
                elif log.status == "Pending Approval":
                    status_code = "PA"
                else:
                    status_code = log.status[:2].upper() if log.status else "A"
            
            # Get first check in and last check out from punch history
            first_check_in = None
            last_check_out = None
            
            if log:
                # Get punch history
                punch_history = frappe.get_all(
                    "Attendance Punch History",
                    filters={"parent": log.name},
                    fields=["punch_type", "punch_time"],
                    order_by="punch_time asc"
                )
                
                for punch in punch_history:
                    if punch.punch_type == "IN" and not first_check_in:
                        first_check_in = punch.punch_time
                    if punch.punch_type == "OUT":
                        last_check_out = punch.punch_time
                
                # Fallback to check_in/check_out if no history
                if not first_check_in and log.check_in:
                    first_check_in = log.check_in
                if not last_check_out and log.check_out:
                    last_check_out = log.check_out
            
            # Format times
            first_check_in_str = ""
            last_check_out_str = ""
            
            if first_check_in:
                if isinstance(first_check_in, str):
                    try:
                        dt = datetime.fromisoformat(first_check_in.replace('Z', '+00:00'))
                        first_check_in_str = dt.strftime("%H:%M:%S")
                    except:
                        first_check_in_str = str(first_check_in)
                else:
                    first_check_in_str = first_check_in.strftime("%H:%M:%S")
            
            if last_check_out:
                if isinstance(last_check_out, str):
                    try:
                        dt = datetime.fromisoformat(last_check_out.replace('Z', '+00:00'))
                        last_check_out_str = dt.strftime("%H:%M:%S")
                    except:
                        last_check_out_str = str(last_check_out)
                else:
                    last_check_out_str = last_check_out.strftime("%H:%M:%S")
            
            # Get working hours
            working_hours = log.working_hours if log and log.working_hours else 0.0
            
            report_data.append({
                "employee_name": emp.employee_name,
                "employee_id": emp.name,
                "date": date_str,
                "first_check_in": first_check_in_str,
                "last_check_out": last_check_out_str,
                "working_hours": round(working_hours, 2),
                "status": status_code
            })
        
        current_date += timedelta(days=1)
    
    # Calculate summary statistics for each employee
    summary_data = []
    employee_stats = {}
    
    # Initialize stats for each employee
    for emp in employees_list:
        employee_stats[emp.name] = {
            "employee_name": emp.employee_name,
            "employee_id": emp.name,
            "worked_days": 0,
            "total_days": 0,
            "total_working_hours": 0.0
        }
    
    # Calculate stats from report data
    for record in report_data:
        emp_id = record["employee_id"]
        if emp_id in employee_stats:
            employee_stats[emp_id]["total_days"] += 1
            
            # Count worked days (exclude Absent and Holiday/Off)
            if record["status"] not in ["A", "O"]:
                employee_stats[emp_id]["worked_days"] += 1
            
            # Sum working hours
            employee_stats[emp_id]["total_working_hours"] += record["working_hours"] or 0.0
    
    # Build summary data
    for emp_id, stats in employee_stats.items():
        # Calculate total working hours (worked days * 9 hours)
        total_working_hours_expected = stats["worked_days"] * 9.0
        
        summary_data.append({
            "employee_name": stats["employee_name"],
            "employee_id": stats["employee_id"],
            "worked_days": stats["worked_days"],
            "total_days": stats["total_days"],
            "working_hours": round(stats["total_working_hours"], 2),
            "total_working_hours": round(total_working_hours_expected, 2)
        })
    
    # Sort summary by employee name for better readability
    summary_data.sort(key=lambda x: x["employee_name"])
    
    return {
        "summary": summary_data,
        "details": report_data
    }
