# Copyright (c) 2025, nexchar and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class AttendancePortalSettings(Document):
	def validate(self):
		if self.monthly_casual_leaves is not None and self.monthly_casual_leaves < 0:
			frappe.throw(frappe._("Monthly Casual Leaves cannot be negative"))
		if self.monthly_sick_leaves is not None and self.monthly_sick_leaves < 0:
			frappe.throw(frappe._("Monthly Sick Leaves cannot be negative"))
