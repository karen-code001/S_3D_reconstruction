import unittest

from control_plane.main import merge_compute_reports
from control_plane.schemas import ComputeNodeStatus


def report(node_id, transfer, reachable=True, available=True, jobs=0, reason=None):
    return ComputeNodeStatus(
        node_id=node_id,
        transfer_node_id=transfer,
        reachable=reachable,
        available=available,
        active_jobs=jobs if reachable else None,
        capacity=1 if reachable else None,
        reason=reason,
    )


class ComputeMergeTests(unittest.TestCase):
    def test_same_compute_node_from_two_transfers_is_one_row_with_both_paths(self):
        merged = merge_compute_reports([
            report("Server-1", "3090C"), report("Server-1", "ZBY"),
            report("Server-7", "3090C"), report("Server-7", "ZBY", reachable=False, available=False, reason="unreachable_or_invalid_response"),
        ])
        self.assertEqual([n.node_id for n in merged], ["Server-1", "Server-7"])
        seven = merged[1]
        self.assertTrue(seven.reachable and seven.available)
        self.assertEqual(seven.transfer_node_id, "3090C")
        self.assertEqual([(p.transfer_node_id, p.reachable) for p in seven.paths], [("3090C", True), ("ZBY", False)])

    def test_status_comes_from_the_best_path(self):
        busy = report("Server-2", "3090C", available=False, jobs=1, reason="at_capacity")
        down = report("Server-2", "ZBY", reachable=False, available=False, reason="unreachable_or_invalid_response")
        merged = merge_compute_reports([down, busy])[0]
        self.assertEqual((merged.transfer_node_id, merged.reachable, merged.available, merged.reason, merged.active_jobs), ("3090C", True, False, "at_capacity", 1))

        gone = merge_compute_reports([down, down.model_copy(update={"transfer_node_id": "3090C"})])[0]
        self.assertFalse(gone.reachable)
        self.assertEqual(len(gone.paths), 2)

    def test_natural_order_and_counts(self):
        reports = [report(f"Server-{i}", t) for i in (10, 2, 12, 1) for t in ("3090C", "ZBY")]
        merged = merge_compute_reports(reports)
        self.assertEqual([n.node_id for n in merged], ["Server-1", "Server-2", "Server-10", "Server-12"])
        self.assertEqual(sum(n.available for n in merged), 4)
        self.assertEqual(merge_compute_reports([]), [])


if __name__ == "__main__":
    unittest.main()
