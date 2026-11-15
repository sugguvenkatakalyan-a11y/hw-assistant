import os, unittest
os.environ["HWA_DEMO"] = "1"
import assistant as A

class T(unittest.TestCase):
    def test_cpu(self):
        d = A.call_tool("get_cpu_info"); self.assertEqual((d["physical_cores"], d["logical_cpus"]), (4, 8))
    def test_memory_upgrade(self):
        d = A.call_tool("get_memory_topology")
        self.assertEqual((d["max_supported_gb"], d["free_slots"], d["upgrade_headroom_gb"]), (64.0, 1, 48.0))
    def test_storage_kinds(self):
        self.assertEqual([x["kind"] for x in A.call_tool("get_storage_info")], ["NVMe SSD", "HDD"])
    def test_rejects_unknown_tool(self): self.assertIn("error", A.call_tool("rm -rf /"))
    def test_rejects_bad_args(self):
        self.assertIn("error", A.call_tool("get_cpu_info", {"x": 1}))
        self.assertIn("error", A.call_tool("get_storage_info", {"device": "sda; rm -rf /"}))
    def test_allowlist(self):
        with self.assertRaises(A.ToolError): A.run("x", ["rm", "-rf", "/"])
        with self.assertRaises(A.ToolError): A.run("x", ["sudo", "-n", "/bin/bash"])
    def test_path_traversal(self):
        with self.assertRaises(A.ToolError): A.read_file("/etc/shadow")
    def test_offline_upgrade_answer(self):
        self.assertIn("48", A.chat_offline("Can I upgrade my RAM, and by how much?"))
    def test_missing_data_is_not_guessed(self):
        self.assertIn("cannot determine", A.say("get_cpu_info", {"error": "lscpu is not installed"}))

if __name__ == "__main__": unittest.main()
