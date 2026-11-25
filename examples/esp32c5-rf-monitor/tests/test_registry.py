"""模块契约与自动发现。"""
import unittest

from core.registry import ModePlugin, Output, Registry, RegistryError, ScanRequest, discover
from core.snapshot import Snapshot
from tests.mock_modes import ProbePlugin


class ContractTests(unittest.TestCase):
    def test_base_contract_is_abstract(self):
        plugin = ModePlugin()
        with self.assertRaises(NotImplementedError):
            plugin.scan_requests()
        with self.assertRaises(NotImplementedError):
            plugin.on_snapshot(None)  # type: ignore[arg-type]
        with self.assertRaises(NotImplementedError):
            plugin.produce()

    def test_scan_request_validation(self):
        with self.assertRaises(ValueError):
            ScanRequest(2_442_000_000, 80_000_000, dwell_ms=50)
        with self.assertRaises(ValueError):
            ScanRequest(7_000_000_000)
        with self.assertRaises(ValueError):
            ScanRequest(2_442_000_000, detector=2)
        self.assertIn("2442MHz", ScanRequest(2_442_000_000).describe())


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = Registry()

    def test_register_and_order(self):
        self.registry.register(ProbePlugin(id="zeta"))
        self.registry.register(ProbePlugin(id="alpha"))
        self.assertEqual([p.id for p in self.registry.plugins], ["alpha", "zeta"])
        self.assertEqual(len(self.registry), 2)
        self.assertEqual(self.registry.total_weight(), 2.0)

    def test_duplicate_and_invalid_rejected(self):
        self.registry.register(ProbePlugin(id="dup"))
        with self.assertRaises(RegistryError):
            self.registry.register(ProbePlugin(id="dup"))
        with self.assertRaises(RegistryError):
            self.registry.register(ProbePlugin(id=""))
        with self.assertRaises(RegistryError):
            self.registry.register(ProbePlugin(id="neg", slice_weight=-1.0))
        with self.assertRaises(RegistryError):
            self.registry.register(object())  # type: ignore[arg-type]

    def test_discover_empty_package_is_allowed(self):
        # 空目录不是错误：用临时空包验证，避免被真实 modes/ 的内容干扰（功能分支会往里加模块）。
        import pathlib
        import sys
        import tempfile

        tmp = tempfile.mkdtemp(prefix="c5mon-empty-")
        pathlib.Path(tmp, "empty_modes").mkdir()
        pathlib.Path(tmp, "empty_modes", "__init__.py").write_text("", encoding="utf-8")
        sys.path.insert(0, tmp)
        try:
            registry = discover("empty_modes")
            self.assertEqual(len(registry), 0)
            self.assertEqual(registry.errors, [])
        finally:
            sys.path.remove(tmp)
            sys.modules.pop("empty_modes", None)

    def test_discover_missing_package_reports_error(self):
        registry = discover("no_such_package")
        self.assertEqual(len(registry), 0)
        self.assertTrue(registry.errors)

    def test_discover_ignores_non_modes_modules(self):
        ProbePlugin(id="outside")
        registry = discover("modes")
        ids = {p.id for p in registry.plugins}
        self.assertNotIn("outside", ids, "测试替身不该被自动注册")
        self.assertNotIn("probe", ids)

    def test_output_render(self):
        output = Output(
            kind="table",
            title="示例",
            columns=["a", "b"],
            rows=[[1, 2.5]],
            notes=["边界声明"],
        )
        text = output.render()
        self.assertIn("[table] 示例", text)
        self.assertIn("边界声明", text)
        self.assertIn("2.5", text)


class PluginBehaviour(unittest.TestCase):
    def test_probe_plugin_collects_snapshots(self):
        plugin = ProbePlugin()
        snap = Snapshot(1.0, 2_442_000_000, 80_000_000, tuple([30] * 512))
        plugin.on_snapshot(snap)
        self.assertEqual(len(plugin.snapshots), 1)
        output = plugin.produce()
        self.assertEqual(output.kind, "table")
        self.assertEqual(len(output.rows), 1)
        plugin.reset()
        self.assertEqual(plugin.snapshots, [])


if __name__ == "__main__":
    unittest.main()