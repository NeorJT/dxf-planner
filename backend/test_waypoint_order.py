import unittest

import numpy as np
from scipy.ndimage import distance_transform_edt

from routers.pathfinding_router import _optimize_waypoint_order


class WaypointOrderTests(unittest.TestCase):
    def setUp(self):
        self.dilated = np.zeros((40, 40), dtype=bool)
        self.dt = distance_transform_edt(~self.dilated)
        self.res = 1000.0
        self.clearance = 0.5

    def test_origin_stays_fixed_and_last_point_can_move(self):
        waypoints = [
            {"x": 0.0, "y": 0.0},
            {"x": 30000.0, "y": 30000.0},
            {"x": 12000.0, "y": 0.0},
        ]
        waypoint_grid_coords = [(0, 0), (30, 30), (12, 0)]

        order = _optimize_waypoint_order(
            waypoints,
            waypoint_grid_coords,
            self.dilated,
            self.dt,
            self.res,
            self.clearance
        )

        self.assertEqual(order, [0, 2, 1])

    def test_four_waypoints_are_reordered_by_traversable_cost(self):
        waypoints = [
            {"x": 0.0, "y": 0.0},
            {"x": 26000.0, "y": 26000.0},
            {"x": 10000.0, "y": 1000.0},
            {"x": 18000.0, "y": 4000.0},
        ]
        waypoint_grid_coords = [(0, 0), (26, 26), (10, 1), (18, 4)]

        order = _optimize_waypoint_order(
            waypoints,
            waypoint_grid_coords,
            self.dilated,
            self.dt,
            self.res,
            self.clearance
        )

        self.assertEqual(order[0], 0)
        self.assertEqual(order, [0, 2, 3, 1])


if __name__ == "__main__":
    unittest.main()
