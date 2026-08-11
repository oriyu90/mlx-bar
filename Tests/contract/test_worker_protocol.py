from __future__ import annotations

import unittest

from mlxbar.workers.protocol import request, validate_message


class ProtocolTests(unittest.TestCase):
    def test_protocol_envelope(self):
        message = request("health")
        validate_message(message)
        self.assertEqual(message["protocol_version"], 1)

    def test_rejects_unknown_protocol(self):
        with self.assertRaises(ValueError):
            validate_message({"protocol_version": 99, "request_id": "x", "method": "health"})


if __name__ == "__main__":
    unittest.main()
