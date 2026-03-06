# Copyright (c) 2025, nexchar and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class OfficeLocation(Document):
	def on_trash(self):
		"""Remove this work location from all employees it is assigned to."""
		# Get all Employee IDs that have this office in allowed_locations (Employee Office Location child table)
		rows = frappe.get_all(
			"Employee Office Location",
			filters={"office": self.name},
			fields=["parent"],
			pluck="parent",
		)
		employee_ids = list(set(rows or []))
		for employee_id in employee_ids:
			try:
				employee = frappe.get_doc("Employee", employee_id)
				to_remove = [r for r in employee.allowed_locations if r.office == self.name]
				for row in to_remove:
					employee.remove(row)
				employee.save(ignore_permissions=True)
			except Exception:
				frappe.log_error(
					title=f"Remove work location from Employee {employee_id}",
					message=frappe.get_traceback(),
				)
				# Continue with other employees; deletion of Office Location still proceeds
