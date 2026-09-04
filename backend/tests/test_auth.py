"""Unit tests for auth.py — password hashing and JWT roundtrip."""

from __future__ import annotations

import unittest

from app.auth import create_access_token, decode_user_id, hash_password, verify_password


class PasswordHashing(unittest.TestCase):
    def test_hash_is_not_plaintext(self):
        h = hash_password("correct horse battery staple")
        self.assertNotEqual(h, "correct horse battery staple")

    def test_verify_correct_password(self):
        h = hash_password("hunter2")
        self.assertTrue(verify_password("hunter2", h))

    def test_verify_wrong_password_fails(self):
        h = hash_password("hunter2")
        self.assertFalse(verify_password("wrong", h))

    def test_same_password_different_hash_each_time(self):
        # pbkdf2_sha256 salts each hash — two hashes of the same input differ.
        h1 = hash_password("same-input")
        h2 = hash_password("same-input")
        self.assertNotEqual(h1, h2)
        self.assertTrue(verify_password("same-input", h1))
        self.assertTrue(verify_password("same-input", h2))


class TokenRoundtrip(unittest.TestCase):
    def test_encode_decode_roundtrip(self):
        token = create_access_token(user_id=42)
        self.assertEqual(decode_user_id(token), 42)

    def test_garbage_token_returns_none(self):
        self.assertIsNone(decode_user_id("not-a-real-token"))

    def test_empty_token_returns_none(self):
        self.assertIsNone(decode_user_id(""))

    def test_tampered_token_returns_none(self):
        token = create_access_token(user_id=7)
        tampered = token[:-4] + "abcd"
        self.assertIsNone(decode_user_id(tampered))


if __name__ == "__main__":
    unittest.main()
