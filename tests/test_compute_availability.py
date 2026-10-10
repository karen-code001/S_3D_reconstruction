import unittest

import httpx

from transfer_node.clients import choose_compute_node, query_compute_node_availability
from transfer_node.config import ComputeTarget


class ComputeNodeAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_reports_available_busy_and_unreachable_nodes(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "available.test":
                return httpx.Response(
                    200,
                    json={"accepting_tasks": True, "active_jobs": 1, "capacity": 2},
                )
            if request.url.host == "busy.test":
                return httpx.Response(
                    200,
                    json={"accepting_tasks": False, "active_jobs": 2, "capacity": 2},
                )
            raise httpx.ConnectError("connection refused", request=request)

        targets = [
            ComputeTarget("available", "http://available.test"),
            ComputeTarget("busy", "http://busy.test"),
            ComputeTarget("offline", "http://offline.test"),
        ]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            nodes = await query_compute_node_availability(client, targets)

        self.assertTrue(nodes[0].reachable)
        self.assertTrue(nodes[0].available)
        self.assertEqual(nodes[0].active_jobs, 1)
        self.assertFalse(nodes[1].available)
        self.assertEqual(nodes[1].reason, "not_accepting_tasks")
        self.assertFalse(nodes[2].reachable)
        self.assertEqual(nodes[2].reason, "unreachable_or_invalid_response")

    async def test_choose_compute_node_uses_the_same_availability_rules(self):
        def handler(request: httpx.Request) -> httpx.Response:
            payloads = {
                "full.test": {"accepting_tasks": True, "active_jobs": 1, "capacity": 1},
                "free.test": {"accepting_tasks": True, "active_jobs": 0, "capacity": 1},
            }
            return httpx.Response(200, json=payloads[request.url.host])

        targets = [
            ComputeTarget("full", "http://full.test"),
            ComputeTarget("free", "http://free.test"),
        ]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            selected = await choose_compute_node(client, targets)

        self.assertEqual(selected, targets[1])

    async def test_no_configured_nodes_returns_an_empty_result(self):
        async with httpx.AsyncClient() as client:
            nodes = await query_compute_node_availability(client, [])

        self.assertEqual(nodes, [])


if __name__ == "__main__":
    unittest.main()
