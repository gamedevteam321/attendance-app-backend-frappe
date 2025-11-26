import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

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
            }
        ]
    })
