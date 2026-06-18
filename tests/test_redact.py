"""Secret redaction unit tests (supports criterion 7 and the read API redaction)."""

import unittest

from core.redact import redact, redact_obj


class RedactTest(unittest.TestCase):
    def test_openai_key(self):
        self.assertNotIn("sk-abc", redact("token sk-abcdef0123456789ABCDEF here"))

    def test_anthropic_key(self):
        out = redact("key sk-ant-abcdef0123456789ABCDEFxyz")
        self.assertIn("REDACTED", out)
        self.assertNotIn("abcdef0123456789", out)

    def test_aws_key(self):
        self.assertIn("REDACTED", redact("AKIAIOSFODNN7EXAMPLE"))

    def test_bearer_token(self):
        out = redact("Authorization: Bearer abcdef123456ghijkl")
        self.assertNotIn("abcdef123456ghijkl", out)

    def test_assigned_secret(self):
        out = redact("PASSWORD=supersecretvalue")
        self.assertIn("REDACTED", out)
        self.assertNotIn("supersecretvalue", out)

    def test_url_basic_auth(self):
        out = redact("https://user:p4ssword@example.com/x")
        self.assertNotIn("p4ssword", out)
        self.assertIn("user:", out)

    def test_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4"
        self.assertNotIn("SflKxwRJSMeKKF2QT4", redact(jwt))

    def test_private_key_block(self):
        block = "-----BEGIN RSA PRIVATE KEY-----\nMIIBy) junk\n-----END RSA PRIVATE KEY-----"
        self.assertIn("REDACTED", redact(block))

    def test_passthrough_clean_text(self):
        self.assertEqual(redact("just a normal sentence."), "just a normal sentence.")

    def test_redact_obj_recurses(self):
        obj = {"a": "sk-abcdef0123456789ABCDEF", "b": ["AKIAIOSFODNN7EXAMPLE", 5], "n": 1}
        out = redact_obj(obj)
        self.assertNotIn("sk-abcdef0123456789ABCDEF", str(out))
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", str(out))
        self.assertEqual(out["n"], 1)
        self.assertEqual(out["b"][1], 5)


if __name__ == "__main__":
    unittest.main()
