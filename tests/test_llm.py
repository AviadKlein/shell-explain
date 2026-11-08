from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import List
from unittest import mock

from she.llm import (
    AnswerStream,
    LlmError,
    ModelMissingError,
    OllamaClient,
    OllamaConfig,
    OllamaMissingError,
    manifest_path,
    models_dir,
    parse_effect_lines,
    server_source,
    server_url,
    without_visible,
)
from she.models import EnvEffect, FileEffect, LlmAnswer
from she.paths import Paths
from she.prompt import build_messages, system_prompt

REPLY = (
    "SUMMARY: Deletes every .tmp file\nunder the current dir. Ends by setting CLEANED.\n"
    "FILE: delete ./a.tmp\nENV: set CLEANED=1\nFILE: bogus x\nEND\nFILE: write ignored"
)


def stream_in_chunks(reply: str, size: int) -> tuple[str, LlmAnswer]:
    splitter = AnswerStream()
    released = [splitter.feed(reply[index : index + size]) for index in range(0, len(reply), size)]
    tail, answer = splitter.finish()
    return "".join(released) + tail, answer


class TestAnswerStream(unittest.TestCase):
    def test_marker_lines_never_leak_at_any_chunk_size(self) -> None:
        for size in (1, 2, 3, 7, 1000):
            with self.subTest(size=size):
                printed, answer = stream_in_chunks(REPLY, size)
                self.assertEqual(" ".join(printed.split()), "Deletes every .tmp file under the current dir. Ends by setting CLEANED.")
                self.assertEqual(answer.summary, "Deletes every .tmp file under the current dir. Ends by setting CLEANED.")
                self.assertEqual(answer.files, (FileEffect(path="./a.tmp", op="delete", basis="inferred"),))
                self.assertEqual(answer.env, (EnvEffect(name="CLEANED", op="set", basis="inferred", value="1"),))

    def test_reply_without_prefix_or_markers(self) -> None:
        printed, answer = stream_in_chunks("Just text.", 4)
        self.assertEqual((printed, answer.summary), ("Just text.", "Just text."))

    def test_bold_markers_are_recognised(self) -> None:
        _, answer = stream_in_chunks("**SUMMARY:** Lists files.\n**FILE:** read .\nEND", 5)
        self.assertEqual(answer.summary, "Lists files.")
        self.assertEqual(answer.files, (FileEffect(path=".", op="read", basis="inferred"),))

    def test_empty_summary_raises(self) -> None:
        splitter = AnswerStream()
        splitter.feed("SUMMARY:\nEND")
        with self.assertRaisesRegex(LlmError, r"^the model returned no summary"):
            splitter.finish()


class TestParseEffectLines(unittest.TestCase):
    def test_invalid_ops_and_names_ignored(self) -> None:
        files, env = parse_effect_lines("FILE: explode x\nENV: set 1BAD\nENV: read $HOME\nFILE: append `log`")
        self.assertEqual(files, (FileEffect(path="log", op="append", basis="inferred"),))
        self.assertEqual(env, (EnvEffect(name="HOME", op="read", basis="inferred"),))


class TestWithoutVisible(unittest.TestCase):
    def test_drops_repeats(self) -> None:
        answer = LlmAnswer(
            summary="s",
            files=(FileEffect(path="a", op="delete", basis="inferred"), FileEffect(path="b", op="read", basis="inferred")),
        )
        visible = (FileEffect(path="a", op="delete", basis="visible"),)
        self.assertEqual(without_visible(answer, visible, ()).files, (FileEffect(path="b", op="read", basis="inferred"),))


class FakeResponse(io.BytesIO):
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def respond(payload: object) -> FakeResponse:
    return FakeResponse(json.dumps(payload).encode())


class TestOllamaClient(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.paths = Paths(cache_dir=root / "c", state_dir=root / "s", config_dir=root / "g")
        self.client = OllamaClient(OllamaConfig(model="qwen3.5:4b"), self.paths)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_model_digest(self) -> None:
        tags = {"models": [{"name": "other:1b", "digest": "zzz"}, {"name": "qwen3.5:4b", "digest": "abc123"}]}
        with mock.patch("she.llm.urllib.request.urlopen", return_value=respond(tags)):
            self.assertEqual(self.client.model_digest(), "abc123")

    def test_missing_model_tells_user_to_pull(self) -> None:
        with mock.patch("she.llm.urllib.request.urlopen", return_value=respond({"models": []})):
            with self.assertRaises(ModelMissingError) as context:
                self.client.model_digest()
        self.assertEqual(
            str(context.exception),
            "Model required: 'qwen3.5:4b' is not installed in Ollama (no models installed). "
            "Pull it with `ollama pull qwen3.5:4b`, or set SHE_MODEL to an installed model.",
        )

    def test_missing_model_lists_installed_ones(self) -> None:
        tags = {"models": [{"name": "llama3.2:3b", "digest": "d"}, {"name": "gemma3:4b", "digest": "e"}]}
        with mock.patch("she.llm.urllib.request.urlopen", return_value=respond(tags)):
            with self.assertRaisesRegex(ModelMissingError, r"\(installed: llama3\.2:3b, gemma3:4b\)"):
                self.client.model_digest()

    def test_missing_default_model_says_it_is_the_default(self) -> None:
        client = OllamaClient(OllamaConfig.from_env(None, {}), self.paths)
        with mock.patch("she.llm.urllib.request.urlopen", return_value=respond({"models": []})):
            with self.assertRaisesRegex(ModelMissingError, r"^Model required: 'qwen3\.5:4b' \(the default; no SHE_MODEL set\) is not"):
                client.model_digest()

    def test_no_ollama_installed(self) -> None:
        with mock.patch.object(OllamaClient, "is_up", return_value=False), mock.patch(
            "she.llm.shutil.which", return_value=None
        ), mock.patch("she.llm.subprocess.Popen") as popen:
            with self.assertRaisesRegex(OllamaMissingError, r"^No Ollama installed: nothing answers at http://127\.0\.0\.1:11435"):
                self.client.ensure_running()
        popen.assert_not_called()

    def test_chat_stream_request_and_chunks(self) -> None:
        lines = [
            {"message": {"content": "SUMMARY: Hi"}, "done": False},
            {"message": {"content": " there."}, "done": False},
            {"message": {"content": ""}, "done": True},
        ]
        body = b"".join(json.dumps(line).encode() + b"\n" for line in lines)
        captured: List[object] = []

        def fake_urlopen(request: object, timeout: float) -> FakeResponse:
            captured.append(request)
            return FakeResponse(body)

        with mock.patch("she.llm.urllib.request.urlopen", side_effect=fake_urlopen):
            chunks = list(self.client.chat_stream(build_messages("ls", "facts", ""), deadline=None))
        self.assertEqual(chunks, ["SUMMARY: Hi", " there."])
        sent = json.loads(captured[0].data)  # type: ignore[attr-defined]
        self.assertEqual(sent["think"], False)
        self.assertEqual(sent["stream"], True)
        self.assertEqual(sent["keep_alive"], "10m")
        self.assertEqual(sent["options"], {"temperature": 0, "seed": 0, "num_predict": 160})

    def test_unreachable_server_raises_llm_error(self) -> None:
        import urllib.error

        with mock.patch("she.llm.urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            self.assertFalse(self.client.is_up())
            with self.assertRaisesRegex(LlmError, r"^cannot reach Ollama at http://127\.0\.0\.1:11435"):
                self.client.model_digest()

    def test_ensure_running_starts_server_and_writes_pid(self) -> None:
        process = mock.Mock(pid=4242)
        process.poll.return_value = None
        with mock.patch.object(OllamaClient, "is_up", side_effect=[False, False, True]), mock.patch(
            "she.llm.shutil.which", return_value="/opt/homebrew/bin/ollama"
        ), mock.patch("she.llm.subprocess.Popen", return_value=process) as popen, mock.patch("she.llm.time.sleep"):
            self.assertTrue(self.client.ensure_running())
        self.assertEqual(popen.call_args.args[0], ["/opt/homebrew/bin/ollama", "serve"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(popen.call_args.kwargs["env"]["OLLAMA_HOST"], "127.0.0.1:11435")
        self.assertEqual(self.paths.ollama_pid_file.read_text(), "4242 http://127.0.0.1:11435\n")

    def test_ensure_running_starts_server_on_configured_port(self) -> None:
        client = OllamaClient(OllamaConfig.from_env(None, {"SHE_OLLAMA_URL": "http://localhost:12000"}), self.paths)
        process = mock.Mock(pid=7)
        process.poll.return_value = None
        with mock.patch.object(OllamaClient, "is_up", side_effect=[False, True]), mock.patch(
            "she.llm.shutil.which", return_value="/opt/homebrew/bin/ollama"
        ), mock.patch("she.llm.subprocess.Popen", return_value=process) as popen:
            self.assertTrue(client.ensure_running())
        self.assertEqual(popen.call_args.kwargs["env"]["OLLAMA_HOST"], "localhost:12000")

    def test_remote_server_down_is_not_started(self) -> None:
        client = OllamaClient(OllamaConfig.from_env(None, {"SHE_OLLAMA_URL": "http://gpu-box:11434"}), self.paths)
        with mock.patch.object(OllamaClient, "is_up", return_value=False), mock.patch("she.llm.subprocess.Popen") as popen:
            with self.assertRaises(LlmError) as context:
                client.ensure_running()
        self.assertEqual(
            str(context.exception),
            "cannot reach Ollama at http://gpu-box:11434: it is not on this machine, so she does not start it. "
            "Start it there, or unset SHE_OLLAMA_URL to use she's own local server.",
        )
        popen.assert_not_called()

    def test_ensure_running_is_noop_when_up(self) -> None:
        with mock.patch.object(OllamaClient, "is_up", return_value=True), mock.patch("she.llm.subprocess.Popen") as popen:
            self.assertFalse(self.client.ensure_running())
        popen.assert_not_called()

    def test_config_from_env(self) -> None:
        config = OllamaConfig.from_env(None, {"SHE_MODEL": "m:1", "SHE_KEEP_ALIVE": "5m", "SHE_OLLAMA_URL": "http://h:1/"})
        self.assertEqual((config.model, config.keep_alive, config.base_url), ("m:1", "5m", "http://h:1"))
        self.assertEqual(OllamaConfig.from_env("cli:2", {"SHE_MODEL": "m:1"}).model, "cli:2")


class TestServerUrl(unittest.TestCase):
    def test_default_is_shes_own_port(self) -> None:
        self.assertEqual(server_url({}), "http://127.0.0.1:11435")

    def test_port_builds_local_url(self) -> None:
        self.assertEqual(server_url({"SHE_OLLAMA_PORT": "12000"}), "http://127.0.0.1:12000")

    def test_url_wins_over_port(self) -> None:
        self.assertEqual(server_url({"SHE_OLLAMA_URL": "http://gpu-box:11434/", "SHE_OLLAMA_PORT": "12000"}), "http://gpu-box:11434")

    def test_port_out_of_range_raises(self) -> None:
        with self.assertRaises(ValueError) as context:
            server_url({"SHE_OLLAMA_PORT": "70000"})
        self.assertEqual(str(context.exception), "expected a port 1-65535 in $SHE_OLLAMA_PORT, got raw_port='70000'")

    def test_port_not_a_number_raises(self) -> None:
        with self.assertRaises(ValueError) as context:
            server_url({"SHE_OLLAMA_PORT": "abc"})
        self.assertEqual(str(context.exception), "expected a port 1-65535 in $SHE_OLLAMA_PORT, got raw_port='abc'")

    def test_url_without_scheme_raises(self) -> None:
        with self.assertRaises(ValueError) as context:
            server_url({"SHE_OLLAMA_URL": "gpu-box:11434"})
        self.assertEqual(
            str(context.exception), "expected an http(s) URL with a host in $SHE_OLLAMA_URL, got raw_url='gpu-box:11434'"
        )

    def test_url_with_bad_port_raises(self) -> None:
        with self.assertRaises(ValueError) as context:
            server_url({"SHE_OLLAMA_URL": "http://gpu-box:99999"})
        self.assertEqual(str(context.exception), "expected a valid port in $SHE_OLLAMA_URL, got raw_url='http://gpu-box:99999'")

    def test_source_says_which_setting_won(self) -> None:
        self.assertEqual(server_source({"SHE_OLLAMA_URL": "http://h:1", "SHE_OLLAMA_PORT": "2"}), "$SHE_OLLAMA_URL; $SHE_OLLAMA_PORT ignored")
        self.assertEqual(server_source({"SHE_OLLAMA_PORT": "2"}), "$SHE_OLLAMA_PORT")

    def test_local_and_listen_address(self) -> None:
        local = OllamaConfig.from_env(None, {"SHE_OLLAMA_URL": "http://[::1]:9000"})
        self.assertTrue(local.is_local)
        self.assertEqual(local.listen_address, "[::1]:9000")
        self.assertFalse(OllamaConfig.from_env(None, {"SHE_OLLAMA_URL": "https://ollama.example.com"}).is_local)


class TestPrompt(unittest.TestCase):
    def test_untrusted_data_and_instructions_appended(self) -> None:
        prompt = system_prompt("Answer in French.")
        self.assertIn("untrusted data", prompt)
        self.assertTrue(prompt.endswith("Answer in French."))
        self.assertNotIn("User instructions", system_prompt("  "))

    def test_user_message_contains_command_and_facts_only(self) -> None:
        messages = build_messages("rm -rf x", "Pieces:\n- rm", "")
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        self.assertIn("rm -rf x", messages[1]["content"])
        self.assertIn("Pieces:\n- rm", messages[1]["content"])


class TestModelLocation(unittest.TestCase):
    def test_models_dir(self) -> None:
        self.assertEqual(models_dir({"OLLAMA_MODELS": "/data/ollama"}), Path("/data/ollama"))
        self.assertEqual(models_dir({}), Path.home() / ".ollama" / "models")

    def test_manifest_path(self) -> None:
        root = Path("/m")
        cases = {
            "qwen3.5:4b": "/m/manifests/registry.ollama.ai/library/qwen3.5/4b",
            "gemma3": "/m/manifests/registry.ollama.ai/library/gemma3/latest",
            "user/tuned:q4": "/m/manifests/registry.ollama.ai/user/tuned/q4",
            "hf.co/org/repo:Q4_K_M": "/m/manifests/hf.co/org/repo/Q4_K_M",
        }
        for model, expected in cases.items():
            with self.subTest(model=model):
                self.assertEqual(manifest_path(root, model), Path(expected))
