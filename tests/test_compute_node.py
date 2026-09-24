import unittest
from pathlib import Path
from shutil import rmtree

from compute_node.processor import run_reconstruction


class ComputeProcessorTests(unittest.IsolatedAsyncioTestCase):
    async def test_placeholder_processor_writes_result(self):
        root = Path(".test-compute-node")
        rmtree(root, ignore_errors=True)
        root.mkdir()
        try:
            input_path = root / "input.bin"
            output_path = root / "result.txt"
            input_path.write_bytes(b"video")
            await run_reconstruction(input_path, output_path)
            self.assertIn("placeholder result", output_path.read_text(encoding="utf-8"))
        finally:
            rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
