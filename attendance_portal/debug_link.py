import frappe

def execute():
    print("DEBUG: Checking Employee-User Link")
    
    # Check specifically for HR-EMP-00001
    if frappe.db.exists("Employee", "HR-EMP-00001"):
        emp = frappe.get_doc("Employee", "HR-EMP-00001")
        print(f"Employee: {emp.name}")
        print(f"User ID in DB: '{emp.user_id}'")
        print(f"User ID type: {type(emp.user_id)}")
        
        # Check if it matches 'Administrator'
        is_match = emp.user_id == 'Administrator'
        print(f"Matches 'Administrator'? {is_match}")
        
        # Check query
        found = frappe.db.get_value("Employee", {"user_id": "Administrator"}, "name")
        print(f"Query result for user_id='Administrator': {found}")
    else:
        print("Employee HR-EMP-00001 not found")
