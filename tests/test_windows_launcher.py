"""Exercise the actual cmd.exe entry point without starting Telegram or the model."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows cmd.exe entry point")


@pytest.fixture
def launcher_checkout(tmp_path):
    project = tmp_path / "проект с пробелами"
    venv.EnvBuilder(with_pip=False).create(project / ".venv")
    scripts = project / ".venv" / "Scripts"
    python = scripts / "python.exe"
    (project / "run_miniapp.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path('launcher-called.json').write_text(json.dumps({'cwd': os.getcwd(), 'args': sys.argv[1:]}), encoding='utf-8')\n"
        "print('IIDouble launch probe OK')\n"
        "raise SystemExit(int(os.environ.get('IID_TEST_EXIT', '0')))\n",
        encoding="utf-8",
    )
    # Establish that the isolated interpreter works before testing the batch file.
    probe = subprocess.run([str(python), "-c", "print('interpreter OK')"], cwd=project,
                           capture_output=True, timeout=10)
    assert probe.returncode == 0, probe.stderr.decode(errors="replace")
    shutil.copyfile(ROOT / "Start Mini App.cmd", project / "Start Mini App.cmd")
    outside = tmp_path / "outside"
    outside.mkdir()
    return project, outside


def run_batch(project, outside, exit_code=0):
    command = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
    launcher = project / "Start Mini App.cmd"
    environment = dict(os.environ, IID_TEST_EXIT=str(exit_code))
    return subprocess.run(f'"{command}" /d /s /c ""{launcher}" --check-only"', executable=command,
                          cwd=outside, env=environment, input="\n", text=True,
                          encoding="utf-8", errors="replace", capture_output=True, timeout=15)


@pytest.mark.parametrize("line_ending", [b"\r\n", b"\n"])
def test_windows_cmd_reaches_python_from_another_directory(launcher_checkout, line_ending):
    project, outside = launcher_checkout
    launcher = project / "Start Mini App.cmd"
    launcher.write_bytes(launcher.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", line_ending))
    result = run_batch(project, outside)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "IIDouble launch probe OK" in result.stdout, result.stdout + result.stderr
    assert not result.stderr, result.stderr
    called = json.loads((project / "launcher-called.json").read_text(encoding="utf-8"))
    assert Path(called["cwd"]) == project
    assert called["args"] == ["--check-only"]


def test_windows_cmd_preserves_python_failure(launcher_checkout):
    project, outside = launcher_checkout
    result = run_batch(project, outside, exit_code=7)
    assert "IIDouble launch probe OK" in result.stdout, result.stdout + result.stderr
    assert result.returncode == 7, result.stdout + result.stderr
