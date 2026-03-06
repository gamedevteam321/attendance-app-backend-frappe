import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


# Default designations to create on migrate (only if they don't exist)
DEFAULT_DESIGNATIONS = [
    # Farm Land
    "Head Farm Land",
    "Cluster Relationship Officer",
    "Executive - Farmland",
    "Assistant - Farmland",
    "Accountant - Farmland",
    # Farming Operations
    "Farm Manager",
    "Assistant Farm Manager",
    "Sr Cluster In-charge",
    "Cluster In-charge",
    "Assistant Cluster In-charge",
    "Sr Field Supervisor",
    "Field Supervisor",
    "Assistant Field Supervisor",
    "Field Assistant",
    # "Field Worker - Driver",
    "Cluster Inventory - Supervisor",
    "Cluster Inventory - Assistant",
    "Cluster Equipment Supervisor",
    "Cluster Machinery - Assistant",
    "Cluster Machinery - Technician",
    "Machinery - Operator",
    "Assistant Machiner Operator",
    "Cluster Labour Supervisor",
    "Cluster - Accouting Executive",
    "Field Intern",
    "Helper",
    # Farm Office
    "Farm Support Assistant",
    "Farm Support Executive",
    "Farm - Accounting Supervisor",
    "Biologicals - Assistant",
    "Biologicals - Supervisor",
    # Nursery Operations
    "Nursery Incharge",
    "Nursery Assistant",
    "Nursery Worker",
    "Nursery Helper",
    "Head - Nursery Management",
    "Assistant - Nursery Management",
    # # Scientific Support
    "Head - Research & Scientific",
    "Head - Biologicals",
    "Assistance - Biologicals",
    "Sr Entomologist",
    "Entomologist",
    "Assistant Entomologist",
    "Sr Pathologist",
    "Pathologist",
    "Assistant - Pathologist",
    "Sr Agronomist",
    "Agronomist",
    "Assistant - Agronomist",
    "Research Executive",
    "Research Intern",
    "Lab Technician",
    "Lab - Assistant",
    # Equipment Management
    "Head - Equipment Management",
    "Equipment - Assistant",
    # Fin & Accounts
    "CFO",
    "Finance Controller",
    "Accounts - Manager",
    "Accounts - Asst Manager",
    "Accounts Supervisor",
    "Accounts Executive",
    # # Healing Team
    "Head - Healing",
    "Sr Healer",
    "Healer",
    "Healing Assistant",
]


def after_migrate():
    create_custom_fields({
        "Employee": [
            {
                "fieldname": "allowed_locations",
                "label": "Allowed Locations",
                "fieldtype": "Table",
                "options": "Employee Office Location",
                "insert_after": "default_shift",
                "description": "Restrict attendance punch-in to these specific locations. If empty, allowed from anywhere (if no other restrictions apply)."
            },
            {
                "fieldname": "allowed_geo_areas",
                "label": "Allowed Geo Areas",
                "fieldtype": "Table",
                "options": "Employee Allowed Geo Area",
                "insert_after": "allowed_locations",
                "description": "Farm land (Geo Fencing Area) fields where this employee can mark attendance. Only Field-level areas are used for punch check."
            }
        ],
        "Leave Application": [
            {
                "fieldname": "half_day_slot",
                "label": "Half Day Slot",
                "fieldtype": "Select",
                "options": "First Half\nSecond Half",
                "insert_after": "half_day_date",
                "description": "First half or second half of the day when leave type is Half Day.",
            }
        ],
        "Attendance Log": [
            {
                "fieldname": "geo_fencing_area",
                "label": "Geo Fencing Area",
                "fieldtype": "Link",
                "options": "Geo Fencing Area",
                "insert_after": "office_location",
                "description": "Farm field where punch was recorded when location_type is Farm.",
            }
        ]
    })
    _ensure_half_day_leave_type()
    _ensure_default_designations()


def _ensure_half_day_leave_type():
    """Create Half Day leave type if it does not exist (so it appears in the Leave Type dropdown)."""
    if frappe.db.exists("Leave Type", "Half Day"):
        return
    frappe.get_doc({
        "doctype": "Leave Type",
        "leave_type_name": "Half Day",
        "max_leaves_allowed": 0,
        "is_carry_forward": 0,
        "allow_negative": 1,
        "include_holiday": 0,
        "is_lwp": 0,
    }).insert(ignore_permissions=True)
    frappe.db.commit()


def _ensure_default_designations():
    """Create default designations if they do not exist. Runs on migrate."""
    for designation_name in DEFAULT_DESIGNATIONS:
        if frappe.db.exists("Designation", designation_name):
            continue
        frappe.get_doc({
            "doctype": "Designation",
            "designation_name": designation_name,
        }).insert(ignore_permissions=True)
    frappe.db.commit()
