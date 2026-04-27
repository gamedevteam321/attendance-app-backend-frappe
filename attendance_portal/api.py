"""
API methods for Attendance Portal
All methods are whitelisted for frontend access via frappe-react-sdk
"""

import json
import frappe
from frappe import _
from frappe.utils import today, now_datetime, get_datetime
from frappe.model.rename_doc import rename_doc
from datetime import datetime
import math

from .geo_utils import distance_meters, is_point_in_polygon
from f2c.access.geo_scope_validate import FORCE_USER_ROLES_FOR_GEO_SCOPE_FLAG


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


def _geo_fencing_area_exists():
    return frappe.db.exists("DocType", "Geo Fencing Area")


def _gfa_is_farm(row):
    return (row.get("geo_fencing_type") or "").strip() == "Farm"


def _gfa_is_root_flag(row):
    try:
        return int(row.get("is_root") or 0)
    except (TypeError, ValueError):
        return 0


@frappe.whitelist()
def get_geo_fencing_hierarchy():
    """
    Return Farm -> Cluster -> Field tree for the frontend.
    If Geo Fencing Area doctype does not exist, return { "farms": [] }.

    Root farms are:
    - Rows with geo_fencing_type Farm and no parent (or parent not in the result set), or
    - Rows with geo_fencing_type Farm and is_root set (when the field exists), so extra farm
      sites are not dropped when parent_area points at another farm.

    Farms whose parent is also a Farm (and not promoted by is_root) are nested under
    ``child_farms`` on the parent.

    Farms whose parent is a Cluster/Field/Block (or any non-Farm in this tree) are appended
    as extra root entries so a second farm like ``Farm2`` is never dropped.
    """
    if not _geo_fencing_area_exists():
        return {"farms": []}
    type_names = ["Farm", "Cluster", "Field", "Block"]
    fields = ["name", "area_name", "geo_fencing_type", "parent_area"]
    try:
        if frappe.get_meta("Geo Fencing Area").has_field("is_root"):
            fields.append("is_root")
    except Exception:
        pass
    areas = frappe.get_all(
        "Geo Fencing Area",
        filters={"geo_fencing_type": ["in", type_names]},
        fields=fields,
        order_by="area_name",
        limit_page_length=0,
    )
    by_name = {a["name"]: a for a in areas}
    for a in areas:
        a.setdefault("clusters", [])
        a.setdefault("fields", [])
        a.setdefault("blocks", [])
        a.setdefault("child_farms", [])
    # Attach Cluster / Block / Field under their parent area
    for a in areas:
        parent_name = (a.get("parent_area") or "").strip()
        if not parent_name or parent_name not in by_name:
            continue
        parent = by_name[parent_name]
        gtype = (a.get("geo_fencing_type") or "").strip()
        if gtype == "Cluster":
            parent.setdefault("clusters", []).append(a)
        elif gtype == "Block":
            parent.setdefault("blocks", []).append(a)
        elif gtype == "Field":
            parent.setdefault("fields", []).append(a)

    roots = []
    root_names = set()
    for a in areas:
        if not _gfa_is_farm(a):
            continue
        parent_name = (a.get("parent_area") or "").strip()
        if _gfa_is_root_flag(a):
            roots.append(a)
            root_names.add(a["name"])
            continue
        if not parent_name or parent_name not in by_name:
            roots.append(a)
            root_names.add(a["name"])

    # Nest Farm -> Farm (child is not already a root and not is_root)
    for a in areas:
        if not _gfa_is_farm(a) or a["name"] in root_names:
            continue
        if _gfa_is_root_flag(a):
            continue
        parent_name = (a.get("parent_area") or "").strip()
        if not parent_name or parent_name not in by_name:
            continue
        parent = by_name[parent_name]
        if not _gfa_is_farm(parent):
            continue
        parent.setdefault("child_farms", []).append(a)

    # Farms whose parent is Cluster/Field/Block (not Farm) were skipped above — promote them as roots
    # so every Farm-type Geo Fencing Area appears (e.g. second farm under same project).
    nested_farm_names = set()

    def _collect_nested_farm_names(node):
        for cf in node.get("child_farms") or []:
            nested_farm_names.add(cf.get("name"))
            _collect_nested_farm_names(cf)

    for r in roots:
        _collect_nested_farm_names(r)

    for a in areas:
        if not _gfa_is_farm(a):
            continue
        if a["name"] in root_names or a["name"] in nested_farm_names:
            continue
        roots.append(a)
        root_names.add(a["name"])

    def cluster_fields(c):
        direct = c.get("fields", [])
        from_blocks = []
        for b in c.get("blocks", []):
            from_blocks.extend(b.get("fields", []))
        return direct + from_blocks

    def serialize_farm(r):
        out = {
            "name": r["name"],
            "area_name": r.get("area_name") or r["name"],
            "clusters": [
                {
                    "name": c["name"],
                    "area_name": c.get("area_name") or c["name"],
                    "fields": [
                        {"name": f["name"], "area_name": f.get("area_name") or f["name"]}
                        for f in cluster_fields(c)
                    ],
                }
                for c in r.get("clusters", [])
            ],
        }
        child_farms = r.get("child_farms") or []
        if child_farms:
            out["child_farms"] = [serialize_farm(cf) for cf in child_farms]
        return out

    farms = [serialize_farm(r) for r in roots]
    return {"farms": farms}


@frappe.whitelist()
def is_inside_geo_area(area_name, lat, lng):
    """
    Check if (lat, lng) is inside the given Geo Fencing Area (circle or polygon).
    Returns { "inside": bool, "area_name": str }.
    """
    if not _geo_fencing_area_exists() or not area_name:
        return {"inside": False, "area_name": area_name or ""}
    lat = float(lat)
    lng = float(lng)
    try:
        area = frappe.get_doc("Geo Fencing Area", area_name)
    except Exception:
        return {"inside": False, "area_name": area_name}
    display_name = area.get("area_name") or area_name
    if area.shape_type == "Circle":
        if area.get("center_latitude") is not None and area.get("center_longitude") is not None and area.get("radius") is not None:
            dist = distance_meters(lat, lng, float(area.center_latitude), float(area.center_longitude))
            if dist <= float(area.radius):
                return {"inside": True, "area_name": display_name}
        return {"inside": False, "area_name": display_name}
    if area.shape_type == "Polygon" and getattr(area, "geo_fencing_coordinates", None) and len(area.geo_fencing_coordinates) >= 3:
        coords = sorted(area.geo_fencing_coordinates, key=lambda x: (x.sequence or 0))
        polygon = [(float(c.latitude), float(c.longitude)) for c in coords]
        if is_point_in_polygon(lat, lng, polygon):
            return {"inside": True, "area_name": display_name}
    return {"inside": False, "area_name": display_name}


@frappe.whitelist()
def is_inside_office(employee, lat, lng):
    """
    Check if coordinates are within any allowed location (office or farm field).
    First checks corporate offices, then allowed geo (farm) fields.
    Returns dict with inside, office, distance, and when inside a farm: location_type="Farm", geo_fencing_area=...
    """
    lat = float(lat)
    lng = float(lng)
    employee_doc = frappe.get_doc("Employee", employee)

    # 1) Corporate offices
    if getattr(employee_doc, "allowed_locations", None):
        for location_row in employee_doc.allowed_locations:
            office = frappe.get_doc("Office Location", location_row.office)
            if not office.is_active:
                continue
            distance = haversine_distance(lat, lng, office.latitude, office.longitude)
            if distance <= office.radius_meters:
                return {
                    "inside": True,
                    "office": office.name,
                    "office_name": office.office_name,
                    "distance": round(distance, 2),
                    "location_type": "Office",
                    "geo_fencing_area": None,
                }
    elif not getattr(employee_doc, "allowed_geo_areas", None) or len(employee_doc.allowed_geo_areas) == 0:
        return {
            "inside": True,
            "office": "Anywhere",
            "distance": 0,
            "message": "No office locations assigned - allowing from anywhere",
            "location_type": "Office",
            "geo_fencing_area": None,
        }

    # 2) Farm fields (allowed_geo_areas)
    if _geo_fencing_area_exists() and getattr(employee_doc, "allowed_geo_areas", None):
        for row in employee_doc.allowed_geo_areas:
            area_name = getattr(row, "geo_fencing_area", None)
            if not area_name:
                continue
            result = is_inside_geo_area(area_name, lat, lng)
            if result.get("inside"):
                return {
                    "inside": True,
                    "office": None,
                    "distance": None,
                    "location_type": "Farm",
                    "geo_fencing_area": area_name,
                    "area_name": result.get("area_name", area_name),
                }
    return {
        "inside": False,
        "office": None,
        "distance": None,
        "location_type": None,
        "geo_fencing_area": None,
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
    geo_fencing_area = None
    remote_req = None
    remote_status = None

    if remote_check["has_remote"]:
        remote_req = remote_check["request_name"]
        remote_status = remote_check["request_status"]
        location_type = "Remote"
        office_check = is_inside_office(employee, lat, lng)
        if office_check["inside"]:
            office_location = office_check.get("office")
            geo_fencing_area = office_check.get("geo_fencing_area")
    else:
        office_check = is_inside_office(employee, lat, lng)
        if not office_check["inside"]:
            if action == "IN":
                frappe.throw(_("You are not within any allowed office or farm location. Please request remote work if working remotely."))
            else:
                frappe.throw(_("You must be within an office or farm location to punch out when not working remotely. Please use the punch out outside office feature."))
        location_type = office_check.get("location_type") or "Office"
        office_location = office_check.get("office")
        geo_fencing_area = office_check.get("geo_fencing_area")

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
                        if hasattr(attendance_log, "geo_fencing_area"):
                            attendance_log.geo_fencing_area = geo_fencing_area
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
                    if hasattr(attendance_log, "geo_fencing_area"):
                        attendance_log.geo_fencing_area = geo_fencing_area
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
                if hasattr(attendance_log, "geo_fencing_area"):
                    attendance_log.geo_fencing_area = geo_fencing_area
                attendance_log.working_remote_req = remote_req
                if remote_status == "Pending":
                    attendance_log.status = "Pending Approval"
                elif remote_status == "Approved":
                    attendance_log.status = "Present"
                else:
                    attendance_log.status = "Present"
            else:
                log_dict = {
                    "doctype": "Attendance Log",
                    "employee": employee,
                    "attendance_date": attendance_date,
                    "check_in": now_time,
                    "location_type": location_type,
                    "office_location": office_location,
                    "check_in_lat": lat,
                    "check_in_lng": lng,
                    "working_remote_req": remote_req,
                    "status": "Pending Approval" if remote_status == "Pending" else "Present",
                }
                if geo_fencing_area:
                    log_dict["geo_fencing_area"] = geo_fencing_area
                attendance_log = frappe.get_doc(log_dict)
            
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
        
        if office_location:
            attendance_log.office_location = office_location
        if geo_fencing_area and hasattr(attendance_log, "geo_fencing_area"):
            attendance_log.geo_fencing_area = geo_fencing_area

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
def apply_for_leave(employee, leave_type, from_date, to_date, reason, half_day_slot=None):
    """
    Create leave application with validation based on leave type.
    
    Rules:
    - Casual Leave: Cannot apply if balance <= 0
    - Sick Leave: Can apply even if balance <= 0 (can go negative)
    - Compensatory Leave: No balance check (no deduction)
    - Half Day: Single day only; requires half_day_slot "First Half" or "Second Half"
    
    Args:
        employee: Employee ID
        leave_type: Leave Type (e.g. Casual Leave, Sick Leave, Half Day)
        from_date: Start date
        to_date: End date
        reason: Reason for leave
        half_day_slot: "First Half" or "Second Half" when leave_type is "Half Day"
    
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
    
    # Half Day leave type: single day only, require first/second half
    if leave_type == "Half Day":
        if from_date != to_date:
            frappe.throw(_("Half Day leave must be for a single day. Set From Date and To Date to the same date."))
        if half_day_slot not in ("First Half", "Second Half"):
            frappe.throw(_("Please select First Half or Second Half for Half Day leave."))
    
    # Validate leave balance for Casual Leave
    if leave_type == "Casual Leave":
        balances = get_leave_balances(employee)
        casual_balance = next((b for b in balances if b["leave_type"] == "Casual Leave"), None)
        
        if casual_balance and casual_balance["remaining"] <= 0:
            frappe.throw(_("Insufficient Casual Leave balance. You have {0} leaves remaining.").format(casual_balance['remaining']))
    
    doc_dict = {
        "doctype": "Leave Application",
        "employee": employee,
        "leave_type": leave_type,
        "from_date": from_date,
        "to_date": to_date,
        "description": reason,
        "status": "Open",
        "docstatus": 0,
        "posting_date": today(),
    }
    
    if leave_type == "Half Day":
        doc_dict["half_day"] = 1
        doc_dict["half_day_date"] = from_date
        doc_dict["half_day_slot"] = half_day_slot
    else:
        doc_dict["half_day"] = 0
    
    leave_application = frappe.get_doc(doc_dict)
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
    
    leave_types = ["Casual Leave", "Sick Leave", "Compensatory Leave", "Half Day"]
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
def get_google_maps_api_key():
    """
    Return the Google Maps API key from site config (site_config.json key: google_maps_api_key).
    Used by the frontend for Work Locations map, search, and satellite view.
    """
    return frappe.conf.get("google_maps_api_key") or ""


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
def create_designation(designation_name, description=None):
    """
    Create a new Designation. Allowed for HR Admin so they can add designations from the employee form.
    """
    if "HR Admin" not in frappe.get_roles(frappe.session.user) and "System Manager" not in frappe.get_roles(frappe.session.user):
        frappe.throw(_("Not authorized to create designations"))
    designation_name = (designation_name or "").strip()
    if not designation_name:
        frappe.throw(_("Designation name is required"))
    if frappe.db.exists("Designation", designation_name):
        return designation_name
    doc = frappe.get_doc({
        "doctype": "Designation",
        "designation_name": designation_name,
        "description": description or "",
    })
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return doc.name


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
def create_employee(first_name, last_name, email, password, designation, gender, date_of_birth, date_of_joining, company, reports_to, office=None, offices=None, allowed_farm_fields=None, roles=None, holiday_list=None):
    """
    Create a new User and Employee document.

    Args:
        roles: Optional list (or JSON string) of role names from the portal: desk roles
            (Employee, Manager, HR Admin) plus operational roles (Field Supervisor, Driver, etc.).
            Same rules as ``update_employee``. If omitted or empty, defaults to ``["Employee"]``.
        holiday_list: Holiday List name to assign to the employee (optional)
    """
    # Check permissions
    if "HR Admin" not in frappe.get_roles(frappe.session.user):
        frappe.throw(_("Not authorized"))

    if roles is None:
        parsed_roles = ["Employee"]
    else:
        parsed_roles = _parse_str_list_param(roles)
        if not parsed_roles:
            parsed_roles = ["Employee"]
        elif "Employee" not in parsed_roles:
            parsed_roles = list(parsed_roles) + ["Employee"]

    # 1. Create User
    if frappe.db.exists("User", email):
        frappe.throw(_("User with this email already exists"))

    user = frappe.get_doc({
        "doctype": "User",
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "send_welcome_email": 0,
        "enabled": 1,
    })
    user.new_password = password
    _merge_user_roles_for_update_employee(user, parsed_roles, frappe.session.user)
    user.insert(ignore_permissions=True)
    
    # 2. Create Employee
    allowed_locations = []
    if offices and isinstance(offices, list) and len(offices) > 0:
        for office_name in offices:
            if office_name:
                allowed_locations.append({"office": office_name})
    elif office:
        allowed_locations.append({"office": office})

    allowed_geo_areas = []
    if _geo_fencing_area_exists() and allowed_farm_fields and isinstance(allowed_farm_fields, list):
        for area_name in allowed_farm_fields:
            if area_name and isinstance(area_name, str) and frappe.db.exists("Geo Fencing Area", area_name):
                allowed_geo_areas.append({"geo_fencing_area": area_name})
    if isinstance(allowed_farm_fields, str) and allowed_farm_fields.strip():
        try:
            parsed = json.loads(allowed_farm_fields)
            if isinstance(parsed, list):
                for area_name in parsed:
                    if area_name and frappe.db.exists("Geo Fencing Area", area_name):
                        allowed_geo_areas.append({"geo_fencing_area": area_name})
        except (json.JSONDecodeError, TypeError):
            pass

    if not allowed_locations and not allowed_geo_areas:
        frappe.throw(_("At least one work location is required: assign at least one corporate office or at least one farm field."))
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
    if getattr(employee, "allowed_geo_areas", None) is not None and allowed_geo_areas:
        employee.allowed_geo_areas = []
        for row in allowed_geo_areas:
            employee.append("allowed_geo_areas", row)
    
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


# Operational roles offered by the attendance portal Edit Employee UI (must match Role.name when assigned).
ATTENDANCE_PORTAL_OPERATIONAL_ROLE_NAMES = frozenset({
    "Field Supervisor",
    "Cluster Supervisor",
    "Farm Manager",
    "Project Manager",
    "Administrator",
    "Finance Head",
    "CEO/Operational Head",
    "Driver",
})
# Desk / portal roles created via Add Employee — always preserved when merging operational roles.
ATTENDANCE_PORTAL_DESK_ROLE_NAMES = frozenset({"Employee", "Manager", "HR Admin"})
# HR Admin must not assign these via the portal (System Manager may assign a wider set).
ATTENDANCE_PORTAL_ROLE_ASSIGN_BLOCKLIST_HR = frozenset({
    "System Manager",
    "Administrator",
    "All",
    "Guest",
})

# Named roles that always may load/assign portal-controlled User roles (broad HR access).
_PORTAL_USER_ROLES_GATE_BY_ROLE = frozenset({
    "HR Admin",
    "HR Manager",
    "HR User",
    "System Manager",
})


def _portal_session_may_edit_employees_via_named_roles():
    if frappe.session.user == "Administrator":
        return True
    return bool(_PORTAL_USER_ROLES_GATE_BY_ROLE & set(frappe.get_roles(frappe.session.user)))


def _portal_may_manage_linked_user_roles(user_id):
    """
    True if this session may read/update User.roles data for ``user_id`` in the portal.

    Allows standard HR roles plus anyone Frappe grants **write** on the Employee linked to
    that user (covers custom HR roles without hard-coding every Role name).
    """
    if _portal_session_may_edit_employees_via_named_roles():
        return True
    u = (user_id or "").strip() if isinstance(user_id, str) else ""
    if not u:
        return False
    employee_name = frappe.db.get_value("Employee", {"user_id": u}, "name")
    if not employee_name:
        return False
    return frappe.has_permission("Employee", "write", employee_name)


def _portal_may_update_employee_record(employee_name):
    if _portal_session_may_edit_employees_via_named_roles():
        return True
    if not employee_name:
        return False
    return frappe.has_permission("Employee", "write", employee_name)


@frappe.whitelist()
def get_user_roles_for_employee_edit(user_id=None):
    """
    Return role names assigned to a User (for Edit Employee checkboxes).

    Reads ``Has Role`` with ignore_permissions so HR Admin still receives roles when the
    standard REST User document omits or strips the ``roles`` child table.
    """
    if not _portal_may_manage_linked_user_roles(user_id):
        frappe.throw(
            _("Not permitted to load roles for this user."),
            frappe.PermissionError,
        )
    if not user_id or not isinstance(user_id, str):
        return {"roles": []}
    user_id = user_id.strip()
    if not user_id or not frappe.db.exists("User", user_id):
        return {"roles": []}
    rows = frappe.get_all(
        "Has Role",
        filters={"parenttype": "User", "parent": user_id},
        pluck="role",
        order_by="creation asc",
        ignore_permissions=True,
    )
    seen = set()
    out = []
    for r in rows or []:
        role = (r or "").strip()
        if not role or role in seen:
            continue
        seen.add(role)
        out.append(role)
    return {"roles": out}


def _parse_str_list_param(val):
    """Parse optional API list param from list or JSON string."""
    if val is None:
        return None
    if isinstance(val, str):
        s = (val or "").strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
        except (json.JSONDecodeError, TypeError):
            return []
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
        return []
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    return []


def _merge_user_roles_for_update_employee(user_doc, selected_roles, session_user):
    """
    Apply User role selection from the Edit Employee portal.

    ``selected_roles`` should list desk roles (Employee, Manager, HR Admin) and operational
    roles (Field Supervisor, Driver, etc.). If no desk role is included in the list (legacy
    clients), desk roles are taken from the user's existing roles instead.
    """
    existing = {r.role for r in (user_doc.roles or []) if getattr(r, "role", None)}
    other_kept = existing - ATTENDANCE_PORTAL_OPERATIONAL_ROLE_NAMES - ATTENDANCE_PORTAL_DESK_ROLE_NAMES

    is_system_manager = "System Manager" in frappe.get_roles(session_user)

    controlled_desk = set()
    for role in selected_roles:
        r = (role or "").strip()
        if r in ATTENDANCE_PORTAL_DESK_ROLE_NAMES:
            if not frappe.db.exists("Role", r):
                frappe.throw(_("Role '{0}' does not exist").format(r))
            controlled_desk.add(r)

    if not controlled_desk:
        controlled_desk = existing & ATTENDANCE_PORTAL_DESK_ROLE_NAMES

    controlled_op = []
    for role in selected_roles:
        r = (role or "").strip()
        if r in ATTENDANCE_PORTAL_DESK_ROLE_NAMES:
            continue
        if r not in ATTENDANCE_PORTAL_OPERATIONAL_ROLE_NAMES:
            frappe.throw(
                _("Invalid role: {0}. Must be Employee, Manager, HR Admin, or an operational role from the portal list.").format(r)
            )
        if not frappe.db.exists("Role", r):
            frappe.throw(_("Role '{0}' does not exist. Create it in Role or run F2C seed.").format(r))
        if not is_system_manager and r in ATTENDANCE_PORTAL_ROLE_ASSIGN_BLOCKLIST_HR:
            continue
        controlled_op.append(r)

    final = set(controlled_op) | controlled_desk | other_kept
    if user_doc.name != "Administrator" and "Employee" not in final:
        if frappe.db.exists("Role", "Employee"):
            final.add("Employee")

    if not is_system_manager:
        final -= ATTENDANCE_PORTAL_ROLE_ASSIGN_BLOCKLIST_HR

    user_doc.roles = []
    for role in sorted(final):
        user_doc.append("roles", {"role": role})


@frappe.whitelist()
def update_employee(employee_id, first_name, last_name, email, designation, reports_to, status, office=None, offices=None, allowed_farm_fields=None, company=None, password=None, holiday_list=None, roles=None):
    """
    Update Employee and User details.
    
    Args:
        holiday_list: Holiday List name to assign to the employee (optional, can be empty string to clear)
        offices: List of office location names (optional, for multiple locations)
        office: Single office location name (optional, for backward compatibility)
        company: Company name (optional, to change employee's company)
        roles: Optional list of role names from the Edit Employee UI: desk roles (Employee, Manager, HR Admin)
            plus operational roles (Field Supervisor, Driver, etc.). When provided, these replace the previous
            portal-controlled roles; other roles on the User (e.g. Report Manager) are kept.
    """
    if not _portal_may_update_employee_record(employee_id):
        frappe.throw(_("Not permitted to update this employee."), frappe.PermissionError)

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
    if getattr(employee, "allowed_locations", None) is not None:
        # Parse offices if sent as JSON string (e.g. from some API clients)
        if isinstance(offices, str):
            try:
                offices = json.loads(offices) if offices.strip() else []
            except (json.JSONDecodeError, AttributeError):
                offices = []
        if offices is not None and isinstance(offices, list):
            employee.allowed_locations = []
            for office_name in offices:
                if office_name and isinstance(office_name, str):
                    if frappe.db.exists("Office Location", office_name):
                        employee.append("allowed_locations", {"office": office_name})
        elif office:
            employee.allowed_locations = []
            if frappe.db.exists("Office Location", office):
                employee.append("allowed_locations", {"office": office})

    # Update farm fields (allowed_geo_areas) only if the custom field exists on Employee
    if _geo_fencing_area_exists() and frappe.get_meta("Employee").has_field("allowed_geo_areas"):
        if isinstance(allowed_farm_fields, str):
            try:
                allowed_farm_fields = json.loads(allowed_farm_fields) if (allowed_farm_fields or "").strip() else []
            except (json.JSONDecodeError, AttributeError, TypeError):
                allowed_farm_fields = []
        if allowed_farm_fields is not None and isinstance(allowed_farm_fields, list):
            # Normalize: accept string or dict with name/geo_fencing_area
            area_names = []
            for item in allowed_farm_fields:
                if isinstance(item, str) and item.strip():
                    area_names.append(item.strip())
                elif isinstance(item, dict):
                    name = item.get("name") or item.get("geo_fencing_area")
                    if name:
                        area_names.append(str(name).strip())
            # Clear and set only valid Geo Fencing Area names
            employee.allowed_geo_areas = []
            for area_name in area_names:
                if frappe.db.exists("Geo Fencing Area", area_name):
                    employee.append("allowed_geo_areas", {"geo_fencing_area": area_name})

    # Update holiday_list if provided
    if holiday_list is not None:
        if holiday_list:  # If not empty string
            # Validate that holiday_list exists
            if not frappe.db.exists("Holiday List", holiday_list):
                frappe.throw(_("Holiday List '{0}' does not exist").format(holiday_list))
            employee.holiday_list = holiday_list
        else:  # Empty string means clear the holiday_list
            employee.holiday_list = None

    roles_list = _parse_str_list_param(roles)
    if (
        roles_list is not None
        and employee.user_id
        and employee.user_id != "Administrator"
    ):
        # Merge roles in memory before Employee.save so F2C geo hooks validate against the
        # portal selection (User DB roles are still the previous set until user.save below).
        # This doc is only for computing the flag; Employee.on_update will save User again — we
        # reload User from DB after employee.save() before applying portal changes.
        role_preview_user = frappe.get_doc("User", employee.user_id)
        role_preview_user.first_name = first_name
        role_preview_user.last_name = last_name
        role_preview_user.email = email
        if password:
            role_preview_user.new_password = password
        _merge_user_roles_for_update_employee(role_preview_user, roles_list, frappe.session.user)
        setattr(
            frappe.flags,
            FORCE_USER_ROLES_FOR_GEO_SCOPE_FLAG,
            [r.role for r in (role_preview_user.roles or []) if getattr(r, "role", None)],
        )

    try:
        employee.save(ignore_permissions=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Attendance Portal update_employee (save)")
        raise
    finally:
        setattr(frappe.flags, FORCE_USER_ROLES_FOR_GEO_SCOPE_FLAG, None)

    # 2. Update User
    if employee.user_id:
        # Employee.on_update() runs update_user() which does user.save() on a fresh User doc.
        # Never reuse the pre-employee-save User object here — it will hit TimestampMismatchError.
        user = frappe.get_doc("User", employee.user_id)
        user.first_name = first_name
        user.last_name = last_name
        if employee.user_id != "Administrator":
            user.email = email
        if password:
            user.new_password = password
        if roles_list is not None and user.name != "Administrator":
            _merge_user_roles_for_update_employee(user, roles_list, frappe.session.user)

        try:
            user.save(ignore_permissions=True)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Attendance Portal update_employee (user save)")
            raise

        # If email changed, update user_id in Employee (never rename Administrator)
        if employee.user_id != "Administrator" and email != employee.user_id:
            rename_doc("User", employee.user_id, email, ignore_permissions=True)
            employee.user_id = email
            employee.save(ignore_permissions=True)

    frappe.db.commit()
    return employee.as_dict()


@frappe.whitelist()
def create_sample_data():
    """
    Create sample users and employees for testing the attendance portal.
    Requires System Manager or HR Admin. Uses default password Sample@123 for all.
    Creates: 1 HR Admin, 1 Manager, 2 Employees; ensures a Company and Work Location exist.
    """
    roles = frappe.get_roles(frappe.session.user)
    if "System Manager" not in roles and "HR Admin" not in roles:
        frappe.throw(_("Not authorized. Only System Manager or HR Admin can create sample data."))

    company = _get_or_create_sample_company()
    work_location = _get_or_create_sample_work_location(company)
    default_password = "Sample@123"

    samples = [
        {
            "email": "sample_hradmin@example.com",
            "first_name": "Sample",
            "last_name": "HR Admin",
            "designation": "HR Manager",
            "gender": "Female",
            "roles": ["Employee", "HR Admin"],
        },
        {
            "email": "sample_manager@example.com",
            "first_name": "Sample",
            "last_name": "Manager",
            "designation": "Team Lead",
            "gender": "Male",
            "roles": ["Employee", "Manager"],
        },
        {
            "email": "sample_employee1@example.com",
            "first_name": "Rahul",
            "last_name": "Sharma",
            "designation": "Developer",
            "gender": "Male",
            "roles": ["Employee"],
        },
        {
            "email": "sample_employee2@example.com",
            "first_name": "Priya",
            "last_name": "Singh",
            "designation": "Designer",
            "gender": "Female",
            "roles": ["Employee"],
        },
    ]

    created = []
    manager_employee_id = None

    for s in samples:
        if frappe.db.exists("User", s["email"]):
            created.append({"email": s["email"], "status": "already_exists"})
            if "Manager" in s["roles"]:
                manager_employee_id = frappe.db.get_value("Employee", {"user_id": s["email"]}, "name")
            continue
        try:
            reports_to = manager_employee_id if ("Manager" not in s["roles"] and manager_employee_id) else None
            emp = create_employee(
                first_name=s["first_name"],
                last_name=s["last_name"],
                email=s["email"],
                password=default_password,
                designation=s["designation"],
                gender=s["gender"],
                date_of_birth="1990-01-15",
                date_of_joining=today(),
                company=company,
                reports_to=reports_to,
                offices=[work_location] if work_location else None,
                roles=s["roles"],
            )
            if "Manager" in s["roles"]:
                manager_employee_id = emp.get("name")
            created.append({"email": s["email"], "status": "created", "employee": emp.get("name")})
        except Exception as e:
            created.append({"email": s["email"], "status": "error", "message": str(e)})

    frappe.db.commit()
    return {
        "message": "Sample data created. Use password: Sample@123 for all sample users.",
        "company": company,
        "work_location": work_location,
        "created": created,
    }


def _get_or_create_sample_company():
    companies = frappe.get_all("Company", pluck="name")
    if companies:
        return companies[0]
    doc = frappe.get_doc({
        "doctype": "Company",
        "company_name": "Sample Company",
        "default_currency": "INR",
        "abbr": "SC",
    })
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return doc.name


def _get_or_create_sample_work_location(company):
    locations = frappe.get_all(
        "Office Location",
        filters={"company": company, "is_active": 1},
        pluck="name",
        limit=1,
    )
    if locations:
        return locations[0]
    doc = frappe.get_doc({
        "doctype": "Office Location",
        "office_name": "Sample Work Location",
        "company": company,
        "latitude": 28.6139,
        "longitude": 77.209,
        "radius_meters": 200,
        "is_active": 1,
        "address": "Sample Address, New Delhi",
    })
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return doc.name


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

    log_fields = ["name", "attendance_date", "check_in", "check_out", "status", "working_hours", "location_type", "office_location", "geo_fencing_area"]
    logs = frappe.get_all(
        "Attendance Log",
        filters={
            "employee": employee,
            "attendance_date": ["between", [from_date, to_date]]
        },
        fields=log_fields,
        order_by="attendance_date desc"
    )

    # Enrich with regularization and punch-out-of-office info
    log_names = [log.name for log in logs]
    reg_by_date = {}
    if frappe.db.exists("DocType", "Attendance Regularization Request") and logs:
        reg_list = frappe.get_all(
            "Attendance Regularization Request",
            filters={
                "employee": employee,
                "attendance_date": ["between", [from_date, to_date]],
                "status": "Approved"
            },
            fields=["attendance_date", "reason", "requested_in", "requested_out", "status"]
        )
        for r in reg_list:
            reg_by_date[str(r.attendance_date)] = {
                "reason": r.reason,
                "requested_in": r.requested_in,
                "requested_out": r.requested_out,
                "status": r.status,
            }
    punch_out_by_log = {}
    if frappe.db.exists("DocType", "Punch Out Request") and log_names:
        punch_list = frappe.get_all(
            "Punch Out Request",
            filters={"attendance_log": ["in", log_names]},
            fields=["attendance_log", "reason", "status", "latitude", "longitude", "check_out_time"]
        )
        for p in punch_list:
            punch_out_by_log[p.attendance_log] = {
                "reason": p.reason,
                "status": p.status,
                "latitude": p.latitude,
                "longitude": p.longitude,
                "check_out_time": p.check_out_time,
            }
    for log in logs:
        log["is_regularized"] = str(log.attendance_date) in reg_by_date
        log["regularization"] = reg_by_date.get(str(log.attendance_date))
        log["punch_out_request"] = punch_out_by_log.get(log.name)
    
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
        },
        {
            "name": "Half Day",
            "max_leaves_allowed": 0,
            "is_carry_forward": 0,
            "allow_negative": 1,
            "include_holiday": 0,
            "is_lwp": 0
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


def _get_monthly_leave_values():
    """Get monthly casual and sick leave values from Attendance Portal Settings. Defaults to 1 if not set."""
    try:
        settings = frappe.get_single("Attendance Portal Settings")
        casual = float(settings.monthly_casual_leaves) if settings.monthly_casual_leaves is not None else 1
        sick = float(settings.monthly_sick_leaves) if settings.monthly_sick_leaves is not None else 1
        return casual, sick
    except Exception:
        return 1.0, 1.0


@frappe.whitelist()
def get_leave_settings():
    """
    Get monthly leave allocation settings. HR Admin only for write; anyone can read for display.
    """
    try:
        settings = frappe.get_single("Attendance Portal Settings")
        return {
            "monthly_casual_leaves": float(settings.monthly_casual_leaves) if settings.monthly_casual_leaves is not None else 1,
            "monthly_sick_leaves": float(settings.monthly_sick_leaves) if settings.monthly_sick_leaves is not None else 1,
        }
    except Exception:
        return {"monthly_casual_leaves": 1, "monthly_sick_leaves": 1}


@frappe.whitelist()
def set_leave_settings(monthly_casual_leaves=None, monthly_sick_leaves=None):
    """
    Set monthly leave allocation values. HR Admin only.
    Values can be decimals (e.g. 1.5).
    """
    if "HR Admin" not in frappe.get_roles(frappe.session.user):
        frappe.throw(_("Not authorized to update leave settings"))
    settings = frappe.get_single("Attendance Portal Settings")
    if monthly_casual_leaves is not None:
        settings.monthly_casual_leaves = float(monthly_casual_leaves)
    if monthly_sick_leaves is not None:
        settings.monthly_sick_leaves = float(monthly_sick_leaves)
    settings.save(ignore_permissions=True)
    frappe.db.commit()
    return {"message": "Leave settings updated", "monthly_casual_leaves": settings.monthly_casual_leaves, "monthly_sick_leaves": settings.monthly_sick_leaves}


@frappe.whitelist()
def allocate_monthly_leaves():
    """
    Allocate configured Casual and Sick leaves to all active employees for the current month.
    Uses values from Attendance Portal Settings (default 1 each). Run on the 1st of every month.
    """
    from datetime import datetime
    import calendar

    monthly_casual, monthly_sick = _get_monthly_leave_values()
    if monthly_casual <= 0 and monthly_sick <= 0:
        return {"message": "Monthly casual and sick leave values are both 0; no allocations created."}

    now = datetime.now()
    year = now.year
    month = now.month
    _, last_day = calendar.monthrange(year, month)
    from_date = f"{year}-{month:02d}-01"
    to_date = f"{year}-{month:02d}-{last_day}"

    employees = frappe.get_all("Employee", filters={"status": "Active"}, fields=["name", "employee_name"])
    allocations_created = 0

    for emp in employees:
        if monthly_casual > 0 and not frappe.db.exists("Leave Allocation", {
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
                "new_leaves_allocated": monthly_casual,
                "total_leaves_allocated": 0,
                "docstatus": 1
            })
            doc.insert(ignore_permissions=True)
            allocations_created += 1

        if monthly_sick > 0 and not frappe.db.exists("Leave Allocation", {
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
                "new_leaves_allocated": monthly_sick,
                "total_leaves_allocated": 0,
                "docstatus": 1
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
