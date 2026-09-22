import time
import unittest

from common.security import TokenError, create_upload_token, verify_upload_token


class UploadTokenTests(unittest.TestCase):
    def test_upload_token_round_trip(self):
        token = create_upload_token("task-1", "node-1", "secret", 30)
        claims = verify_upload_token(token, "secret")
        self.assertEqual(claims.task_id, "task-1")
        self.assertEqual(claims.node_id, "node-1")
        self.assertGreaterEqual(claims.expires_at, int(time.time()))

    def test_upload_token_rejects_tampering(self):
        token = create_upload_token("task-1", "node-1", "secret", 30)
        with self.assertRaises(TokenError):
            verify_upload_token(token + "x", "secret")


if __name__ == "__main__":
    unittest.main()
