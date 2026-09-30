"""勘探设备接口层测试：workspace 同版本读口径、动作幂等与乐观锁在 HTTP 层的行为。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services.equipment import equipment_service as svc  # noqa: E402


class EquipmentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        svc.reset()
        self.client = TestClient(app)

    def test_workspace_single_version(self) -> None:
        data = self.client.get("/api/equipment/workspace").json()
        self.assertIn("version", data)
        for plan in data["plans"]:
            self.assertLessEqual(plan["ledger_version"], data["version"])
        for todo in data["todos"]:
            self.assertLessEqual(todo["ledger_version"], data["version"])
        # 详情与侧栏同版本
        detail = self.client.get("/api/equipment/4/detail").json()
        self.assertEqual(detail["equipment"]["version"], 1)
        self.assertEqual(detail["allowed_actions"], ["送检检定"])

    def test_action_flow_http(self) -> None:
        r = self.client.post("/api/equipment/4/actions", json={
            "values": {"action": "送检检定", "检定结论": "检定合格"},
            "request_id": "http-1",
            "expected_version": 1,
        })
        body = r.json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["entry"]["status"], "在库可用")

        r = self.client.post("/api/equipment/4/actions", json={
            "values": {"action": "领用仪器", "经办人": "钱七"},
            "request_id": "http-2",
            "expected_version": 2,
        })
        self.assertTrue(r.json()["ok"])

        # 同 request_id 重放：状态不再变化
        r = self.client.post("/api/equipment/4/actions", json={
            "values": {"action": "领用仪器", "经办人": "孙八"},
            "request_id": "http-2",
            "expected_version": 2,
        })
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["entry"]["使用人员"], "钱七")

        # 过期版本
        r = self.client.post("/api/equipment/4/actions", json={
            "values": {"action": "归还仪器"},
            "request_id": "http-3",
            "expected_version": 1,
        })
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["code"], "version_conflict")

        # 编号串台
        r = self.client.post("/api/equipment/4/actions", json={
            "values": {"action": "归还仪器", "仪器编号": "EQUI-0003"},
            "request_id": "http-4",
        })
        self.assertEqual(r.json()["code"], "serial_mismatch")

        # 正常归还：上次经办人保留
        r = self.client.post("/api/equipment/4/actions", json={
            "values": {"action": "归还仪器"},
            "request_id": "http-5",
        })
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["entry"]["上次经办人"], "钱七")
        self.assertEqual(body["entry"]["使用人员"], "")

    def test_unknown_device_404(self) -> None:
        self.assertEqual(self.client.get("/api/equipment/999").status_code, 404)
        self.assertEqual(self.client.get("/api/equipment/999/detail").status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
