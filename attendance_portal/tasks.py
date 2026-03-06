# Copyright (c) 2025, nexchar and contributors

import frappe


def monthly():
    """Run on 1st of every month: allocate monthly casual and sick leaves to all active employees."""
    from attendance_portal.api import allocate_monthly_leaves
    allocate_monthly_leaves()
