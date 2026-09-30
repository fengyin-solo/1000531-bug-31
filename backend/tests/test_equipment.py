"""勘探设备状态机测试：迁移、前置状态校验、三表回写、幂等、乐观锁与回滚。"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.equipment import (  # noqa: E402
    PLAN_CANCELLED,
    PLAN_PASSED,
    PLAN_PENDING,
    STATUS_AVAILABLE,
    STATUS_BORROWED,
    STATUS_PENDING,
    STATUS_REPAIR,
    STATUS_RETIRED,
    TODO_CLOSED,
    TODO_OPEN,
    EquipmentError,
    equipment_service as svc,
)


class EquipmentStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        svc.reset()

    def test_01_legacy_migration(self) -> None:
        """存量：待检定/已报废归一；状态不明迁移到待核验；借出派生待办与计划。"""
        self.assertEqual(svc.get_entry(1)["status"], STATUS_PENDING)          # 待检定 -> 待核验
        self.assertEqual(svc.get_entry(4)["status"], STATUS_PENDING)          # 外借未登记 -> 待核验
        self.assertEqual(svc.get_entry(5)["status"], STATUS_RETIRED)          # 已报废 -> 已停用
        e3 = svc.get_detail(3)
        self.assertEqual(e3["equipment"]["status"], STATUS_BORROWED)
        self.assertEqual(e3["open_todo"]["todo_status"], TODO_OPEN)
        self.assertEqual(e3["open_todo"]["经办人"], "王野外")
        self.assertEqual(e3["active_plan"]["plan_status"], PLAN_PASSED)
        ws = svc.workspace()
        pending_plans = {p["仪器编号"] for p in ws["plans"]}
        self.assertIn("EQUI-0001", pending_plans)
        self.assertIn("EQUI-0004", pending_plans)

    def test_02_full_lifecycle_writeback(self) -> None:
        """领用/检修/归还/停用都要回写台账、校准计划、领用待办。"""
        svc.run_action(4, "送检检定", {"检定结论": "检定合格"}, request_id="t1")
        svc.run_action(4, "领用仪器", {"经办人": "张工"}, request_id="t2")
        d = svc.get_detail(4)
        self.assertEqual(d["equipment"]["status"], STATUS_BORROWED)
        self.assertEqual(d["equipment"]["使用人员"], "张工")
        self.assertEqual(d["open_todo"]["todo_status"], TODO_OPEN)

        svc.run_action(4, "报修仪器", {}, request_id="t3")
        d = svc.get_detail(4)
        self.assertEqual(d["equipment"]["status"], STATUS_REPAIR)
        self.assertEqual(d["equipment"]["使用人员"], "张工")          # 报修保留经办人
        self.assertEqual(d["open_todo"]["todo_status"], TODO_OPEN)    # 待办不提前关闭
        self.assertEqual(d["active_plan"]["plan_status"], PLAN_PENDING)

        svc.run_action(4, "检修完成", {}, request_id="t4")
        d = svc.get_detail(4)
        self.assertEqual(d["equipment"]["status"], STATUS_BORROWED)   # 有未结待办回到领用人
        self.assertEqual(d["equipment"]["使用人员"], "张工")

        svc.run_action(4, "归还仪器", {}, request_id="t5")
        d = svc.get_detail(4)
        self.assertEqual(d["equipment"]["status"], STATUS_PENDING)    # 归还后进待核验复检
        self.assertEqual(d["equipment"]["使用人员"], "")
        self.assertEqual(d["equipment"]["上次经办人"], "张工")        # 历史经办人留痕
        self.assertIsNone(d["open_todo"])
        closed = d["todos"][-1]
        self.assertEqual(closed["todo_status"], TODO_CLOSED)
        self.assertTrue(closed["归还日期"])
        self.assertEqual(d["active_plan"]["plan_status"], PLAN_PENDING)

        svc.run_action(4, "送检检定", {"检定结论": "检定合格"}, request_id="t6")
        r = svc.run_action(4, "停用仪器", {}, request_id="t7")
        self.assertTrue(r["ok"])
        d = svc.get_detail(4)
        self.assertEqual(d["equipment"]["status"], STATUS_RETIRED)
        self.assertIsNone(d["active_plan"])
        self.assertTrue(all(p["plan_status"] == PLAN_CANCELLED for p in d["plans"]))

    def test_03_version_increments_each_transition(self) -> None:
        self.assertEqual(svc.get_entry(4)["version"], 1)
        svc.run_action(4, "送检检定", {"检定结论": "检定合格"}, request_id="v1")
        self.assertEqual(svc.get_entry(4)["version"], 2)
        svc.run_action(4, "领用仪器", {"经办人": "甲"}, request_id="v2")
        self.assertEqual(svc.get_entry(4)["version"], 3)

    def test_04_skip_step_rejected(self) -> None:
        """跳步归还与非法前置状态一律拒绝，且不产生任何写入。"""
        svc.run_action(4, "送检检定", {"检定结论": "检定合格"}, request_id="s0")
        svc.run_action(4, "领用仪器", {"经办人": "甲"}, request_id="s1")
        svc.run_action(4, "报修仪器", {}, request_id="s2")
        version_before = svc.get_entry(4)["version"]
        todos_before = len(svc.get_detail(4)["todos"])
        with self.assertRaises(EquipmentError) as ctx:
            svc.run_action(4, "归还仪器", {}, request_id="s3")
        self.assertEqual(ctx.exception.code, "illegal_transition")
        self.assertEqual(svc.get_entry(4)["version"], version_before)
        self.assertEqual(len(svc.get_detail(4)["todos"]), todos_before)
        with self.assertRaises(EquipmentError):
            svc.run_action(4, "领用仪器", {"经办人": "乙"}, request_id="s4")  # 维修中不可领用

    def test_05_serial_mismatch_blocks_wrong_device(self) -> None:
        """提交体仪器编号与台账不符（并发串台/写错设备）时拒绝。"""
        with self.assertRaises(EquipmentError) as ctx:
            svc.run_action(3, "归还仪器", {"仪器编号": "EQUI-0004"}, request_id="m1")
        self.assertEqual(ctx.exception.code, "serial_mismatch")

    def test_06_duplicate_serial_blocks_both_ledgers(self) -> None:
        """同型号设备以仪器编号为唯一依据：重复编号在去重前禁止流转。"""
        for entry_id in (6, 7):
            with self.assertRaises(EquipmentError) as ctx:
                svc.run_action(entry_id, "领用仪器", {"经办人": "x"}, request_id=f"dup{entry_id}")
            self.assertEqual(ctx.exception.code, "duplicate_serial")

    def test_07_create_rejects_duplicate_serial(self) -> None:
        entry, errors = svc.create_entry(
            {"仪器编号": "EQUI-0002", "仪器名称": "重复仪", "型号规格": "X1"}
        )
        self.assertIsNone(entry)
        self.assertTrue(errors)
        entry, errors = svc.create_entry(
            {"仪器编号": "EQUI-1001", "仪器名称": "新仪", "型号规格": "X1"}
        )
        self.assertEqual(errors, [])
        self.assertEqual(entry["status"], STATUS_PENDING)
        self.assertEqual(svc.get_detail(entry["id"])["active_plan"]["plan_status"], PLAN_PENDING)

    def test_08_idempotent_replay(self) -> None:
        """同一 request_id 重复提交只生效一次，并回放首次结果。"""
        first = svc.run_action(
            4, "送检检定", {"检定结论": "检定合格"}, request_id="same-id", expected_version=1
        )
        replay = svc.run_action(
            4, "送检检定", {"检定结论": "检定不合格"}, request_id="same-id", expected_version=1
        )
        self.assertEqual(first["entry"]["version"], replay["entry"]["version"])
        self.assertEqual(replay["entry"]["status"], STATUS_AVAILABLE)
        self.assertEqual(svc.get_entry(4)["version"], 2)
        # 失败结果同样幂等：重复提交得到同一失败结论，且不再写台账
        for _ in range(2):
            with self.assertRaises(EquipmentError) as ctx:
                svc.run_action(2, "归还仪器", {}, request_id="failed-id")
            self.assertEqual(ctx.exception.code, "illegal_transition")
        self.assertEqual(svc.get_entry(2)["status"], STATUS_AVAILABLE)
        # 同一 request_id 改作他用会被拒绝
        with self.assertRaises(EquipmentError) as ctx:
            svc.run_action(2, "停用仪器", {}, request_id="failed-id")
        self.assertEqual(ctx.exception.code, "idempotency_conflict")

    def test_09_optimistic_lock_concurrent_submit(self) -> None:
        """并发提交：相同 expected_version 只有一个能写入。"""
        svc.run_action(4, "送检检定", {"检定结论": "检定合格"}, request_id="o0")
        barrier = threading.Barrier(2)
        results: list[object] = []

        def submit(operator: str, rid: str) -> None:
            barrier.wait()
            try:
                results.append(svc.run_action(
                    4, "领用仪器", {"经办人": operator}, request_id=rid, expected_version=2
                ))
            except EquipmentError as exc:
                results.append(exc.code)

        t1 = threading.Thread(target=submit, args=("甲", "oc1"))
        t2 = threading.Thread(target=submit, args=("乙", "oc2"))
        t1.start(); t2.start(); t1.join(); t2.join()
        winners = [r for r in results if isinstance(r, dict)]
        self.assertEqual(len(winners), 1)
        self.assertIn("version_conflict", results)
        winner = winners[0]["entry"]["使用人员"]
        self.assertIn(winner, {"甲", "乙"})
        self.assertEqual(svc.get_entry(4)["使用人员"], winner)       # 台账只接受第一个写入
        open_todos = [t for t in svc.workspace()["todos"] if t["仪器编号"] == "EQUI-0004"]
        self.assertEqual(len(open_todos), 1)
        self.assertEqual(open_todos[0]["经办人"], winner)

    def test_10_failure_rolls_back_all_tables(self) -> None:
        """校验失败（如领用缺经办人）时台账、计划、待办全部回滚。"""
        svc.run_action(4, "送检检定", {"检定结论": "检定合格"}, request_id="rb0")
        before = svc.workspace()
        with self.assertRaises(EquipmentError):
            svc.run_action(4, "领用仪器", {"经办人": "  "}, request_id="rb1")
        after = svc.workspace()
        self.assertEqual(before["version"], after["version"])
        self.assertEqual(len(before["todos"]), len(after["todos"]))
        self.assertEqual(
            len([p for p in before["plans"] if p["仪器编号"] == "EQUI-0004"]),
            len([p for p in after["plans"] if p["仪器编号"] == "EQUI-0004"]),
        )
        self.assertEqual(svc.get_entry(4)["status"], STATUS_AVAILABLE)

    def test_11_sidebar_and_detail_share_version(self) -> None:
        """侧栏计划/待办携带的 ledger_version 不超过台账当前版本。"""
        ws = svc.workspace()
        for plan in ws["plans"]:
            self.assertLessEqual(plan["ledger_version"], ws["version"])
        for todo in ws["todos"]:
            self.assertLessEqual(todo["ledger_version"], ws["version"])
        svc.run_action(4, "送检检定", {"检定结论": "检定合格"}, request_id="w1")
        svc.run_action(4, "领用仪器", {"经办人": "赵六"}, request_id="w2")
        ws = svc.workspace()
        detail = svc.get_detail(4)
        self.assertEqual(detail["equipment"]["version"], 3)
        self.assertEqual(detail["open_todo"]["ledger_version"], 3)
        self.assertGreaterEqual(ws["version"], 3)

    def test_12_inspect_failed_goes_repair(self) -> None:
        svc.run_action(1, "送检检定", {"检定结论": "检定不合格"}, request_id="f1")
        d = svc.get_detail(1)
        self.assertEqual(d["equipment"]["status"], STATUS_REPAIR)
        self.assertEqual(d["active_plan"]["plan_status"], "检定不合格")

    def test_13_return_operator_mismatch_rejected(self) -> None:
        svc.run_action(1, "送检检定", {"检定结论": "检定合格"}, request_id="r0")
        svc.run_action(1, "领用仪器", {"经办人": "原领用人"}, request_id="r1")
        with self.assertRaises(EquipmentError) as ctx:
            svc.run_action(1, "归还仪器", {"经办人": "别人"}, request_id="r2")
        self.assertEqual(ctx.exception.code, "serial_mismatch")
        self.assertEqual(svc.get_entry(1)["status"], STATUS_BORROWED)

    def test_14_repair_without_todo_returns_to_pending(self) -> None:
        svc.run_action(2, "报修仪器", {}, request_id="q1")
        svc.run_action(2, "检修完成", {}, request_id="q2")
        self.assertEqual(svc.get_entry(2)["status"], STATUS_PENDING)

    def test_15_retire_cancels_open_todo(self) -> None:
        svc.run_action(3, "报修仪器", {}, request_id="u1")
        svc.run_action(3, "停用仪器", {}, request_id="u2")
        d = svc.get_detail(3)
        self.assertIsNone(d["open_todo"])
        self.assertEqual(d["todos"][-1]["todo_status"], "已关闭")


if __name__ == "__main__":
    unittest.main(verbosity=2)
