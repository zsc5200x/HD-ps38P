import unittest

from ps38app.totp import normalize_secret, remaining_seconds, totp


class TotpTests(unittest.TestCase):
    def test_rfc6238_vectors(self):
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # "12345678901234567890"
        self.assertEqual(totp(secret, digits=8, at=59), "94287082")
        self.assertEqual(totp(secret, digits=8, at=1111111109), "07081804")
        self.assertEqual(totp(secret, digits=8, at=1234567890), "89005924")

    def test_secret_normalization_and_remaining(self):
        self.assertEqual(normalize_secret("jbsw y3dp ehpk 3pxp"), "JBSWY3DPEHPK3PXP")
        self.assertEqual(remaining_seconds(period=30, at=30), 30)
        self.assertEqual(remaining_seconds(period=30, at=59), 1)


if __name__ == "__main__":
    unittest.main()
