# Copyright (c) 2025, Orgatek and contributors
# Geo helpers for attendance portal (point-in-polygon, circle check).
# Implemented here so attendance_portal does not depend on f2c.

import math


def distance_meters(lat1, lon1, lat2, lon2):
    """Haversine distance between two points in meters."""
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    return 6371000 * c


def is_point_in_polygon(lat, lon, polygon):
    """
    Ray-casting: point (lat, lon) inside polygon?
    polygon: list of (latitude, longitude) tuples.
    """
    if not polygon or len(polygon) < 3:
        return False
    x, y = lon, lat
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i][1], polygon[i][0]
        xj, yj = polygon[j][1], polygon[j][0]
        intersect = ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi)
        if intersect:
            inside = not inside
        j = i
    return inside
