from __future__ import annotations

import unittest

from she.models import EnvEffect, FileEffect, LlmAnswer
from she.redact import redact, redact_answer, shannon_entropy

# Fake secrets are assembled at runtime so the literal token never appears in source;
# otherwise GitHub push protection / secret scanners reject the push.
FAKE_AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
FAKE_GITHUB_TOKEN = "ghp" + "_" + "abcdefghijklmnopqrstuvwxyz0123"
FAKE_GITHUB_PAT = "github" + "_pat_" + "11ABCDEFG0123456789_abcdefghij"
FAKE_SLACK_TOKEN = "xox" + "b-" + "1234567890-abcdefghij"
FAKE_API_KEY = "sk" + "-" + "abcdefghijklmnop1234"
FAKE_GOOGLE_KEY = "AIza" + "B" * 35
FAKE_DATABRICKS_TOKEN = "da" + "pi" + "0123456789abcdef0123456789abcdef"
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxIn0" + "." + "abcdefgh"
FAKE_PRIVATE_KEY_HEADER = "-----BEGIN RSA " + "PRIVATE KEY-----"
FAKE_PRIVATE_KEY_FOOTER = "-----END RSA " + "PRIVATE KEY-----"


class TestRedact(unittest.TestCase):
    def test_known_formats(self) -> None:
        cases = {
            FAKE_AWS_KEY: "<REDACTED:aws-key>",
            FAKE_GITHUB_TOKEN: "<REDACTED:github-token>",
            FAKE_GITHUB_PAT: "<REDACTED:github-token>",
            FAKE_SLACK_TOKEN: "<REDACTED:slack-token>",
            FAKE_API_KEY: "<REDACTED:api-key>",
            FAKE_GOOGLE_KEY: "<REDACTED:google-api-key>",
            FAKE_DATABRICKS_TOKEN: "<REDACTED:databricks-token>",
            FAKE_JWT: "<REDACTED:jwt>",
        }
        for secret, expected in cases.items():
            with self.subTest(secret=secret):
                self.assertEqual(redact(f"use {secret} now"), f"use {expected} now")

    def test_private_key_block(self) -> None:
        text = f"key: {FAKE_PRIVATE_KEY_HEADER}\nabc\n{FAKE_PRIVATE_KEY_FOOTER} done"
        self.assertEqual(redact(text), "key: <REDACTED:private-key> done")

    def test_value_patterns_keep_context(self) -> None:
        self.assertEqual(redact("Authorization: Bearer abc.def"), "Authorization: Bearer <REDACTED:authorization>")
        self.assertEqual(redact("https://user:pw@host/x"), "https://user:<REDACTED:url-credentials>@host/x")
        self.assertEqual(redact("MY_PASSWORD=hunter2 run"), "MY_PASSWORD=<REDACTED:secret-assignment> run")
        self.assertEqual(redact("psql --password s3cret"), "psql --password <REDACTED:secret-flag>")

    def test_high_entropy_but_not_git_sha(self) -> None:
        self.assertEqual(redact("tok Zx9Qk3LmP0aB7cD1eF5gH2iJ4kL6mN8oP0qR"), "tok <REDACTED:high-entropy>")
        sha = "3f786850e387550fdab836ed7e6dc881de23001b"
        self.assertEqual(redact(f"commit {sha}"), f"commit {sha}")

    def test_plain_prose_untouched(self) -> None:
        text = "Deletes every file ending in .tmp; the password prompt is skipped."
        self.assertEqual(redact(text), text)

    def test_non_string_raises(self) -> None:
        with self.assertRaises(TypeError) as context:
            redact(None)  # type: ignore[arg-type]
        self.assertEqual(str(context.exception), "expected str to redact, got NoneType")

    def test_entropy(self) -> None:
        self.assertEqual(shannon_entropy(""), 0.0)
        self.assertEqual(shannon_entropy("aaaa"), 0.0)
        self.assertAlmostEqual(shannon_entropy("abcd"), 2.0)


class TestRedactAnswer(unittest.TestCase):
    def test_all_text_fields(self) -> None:
        answer = LlmAnswer(
            summary=f"Uses {FAKE_AWS_KEY}",
            files=(FileEffect(path=f"/x/{FAKE_GITHUB_TOKEN}", op="read", basis="inferred"),),
            env=(EnvEffect(name="TOKEN", op="set", basis="inferred", value=FAKE_API_KEY),),
        )
        redacted = redact_answer(answer)
        self.assertEqual(redacted.summary, "Uses <REDACTED:aws-key>")
        self.assertEqual(redacted.files[0].path, "/x/<REDACTED:github-token>")
        self.assertEqual(redacted.env[0].value, "<REDACTED:api-key>")
