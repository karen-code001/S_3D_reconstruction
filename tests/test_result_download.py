import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from transfer_node import main


class ResultDownloadCacheTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "results" / "task-1").mkdir(parents=True)
        (self.root / "results" / "task-1" / "cloud.txt").write_text("0 0 0\n")
        patcher = patch.object(main.settings, "storage_root", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_result_is_cacheable_and_revalidates_with_304(self):
        response = main.download_result("task-1", "cloud.txt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], main.RESULT_CACHE_CONTROL)
        etag = response.headers["etag"]

        for header in (etag, f"W/{etag}", f'"other", {etag}', "*"):
            not_modified = main.download_result("task-1", "cloud.txt", if_none_match=header)
            self.assertEqual(not_modified.status_code, 304, header)
            self.assertEqual(not_modified.headers["etag"], etag)
            self.assertEqual(not_modified.body, b"")

    def test_changed_file_is_sent_again(self):
        etag = main.download_result("task-1", "cloud.txt").headers["etag"]
        (self.root / "results" / "task-1" / "cloud.txt").write_text("0 0 0\n1 1 1\n")
        self.assertEqual(main.download_result("task-1", "cloud.txt", if_none_match=etag).status_code, 200)


if __name__ == "__main__":
    unittest.main()
