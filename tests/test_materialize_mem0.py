import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "materialize_mem0.py"
SPEC = importlib.util.spec_from_file_location("materialize_mem0", SCRIPT)
materializer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(materializer)


class MaterializeMem0Tests(unittest.TestCase):
    def test_命令超时转换为稳定错误(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            materializer.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["git", "status"], 30),
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "命令执行超时.*超过 30 秒"):
                materializer._run(["git", "status"], Path(directory))

        self.assertEqual(run.call_args.kwargs["timeout"], materializer.LOCAL_GIT_TIMEOUT_SECONDS)
        self.assertEqual(run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_网络获取使用独立超时(self):
        commit = "a" * 40

        def fake_run(arguments, _cwd, **kwargs):
            if arguments[1] == "fetch":
                self.assertEqual(
                    kwargs["timeout_seconds"], materializer.FETCH_GIT_TIMEOUT_SECONDS
                )
            return commit if arguments[1:3] == ["rev-parse", "HEAD"] else ""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patch_path = root / "source.patch"
            patch_path.write_text("", encoding="utf-8")
            target = root / "output"
            with mock.patch.object(
                materializer,
                "_load_manifest",
                return_value=(
                    {
                        "repository": "https://github.com/example/repo.git",
                        "commit": commit,
                    },
                    patch_path,
                ),
            ), mock.patch.object(materializer, "_run", side_effect=fake_run):
                materializer.materialize(target)

            self.assertTrue(target.is_dir())

    def test_物化失败清理临时目录(self):
        commit = "b" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patch_path = root / "source.patch"
            patch_path.write_text("", encoding="utf-8")
            target = root / "output"
            outcomes = ["", "", "", "", RuntimeError("网络获取超时")]
            with mock.patch.object(
                materializer,
                "_load_manifest",
                return_value=(
                    {
                        "repository": "https://github.com/example/repo.git",
                        "commit": commit,
                    },
                    patch_path,
                ),
            ), mock.patch.object(materializer, "_run", side_effect=outcomes):
                with self.assertRaisesRegex(RuntimeError, "网络获取超时"):
                    materializer.materialize(target)

            self.assertFalse(target.exists())
            self.assertEqual(list(root.glob(".mem0-materialize-*")), [])


if __name__ == "__main__":
    unittest.main()
