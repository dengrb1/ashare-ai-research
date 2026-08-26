"""Functional test for console_app._run_script: the script spawns a long-lived
child that keeps the output handle open, then exits. The old pipe-based reader
would block forever on EOF (buttons frozen); the new file+tailer design must
emit the 'done' event promptly."""

import queue
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, r"F:\code\qmt\tools\console")
import console_app as c  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="console_test_"))
script = tmp / "fake_start.ps1"
script.write_text(
    '\n'.join([
        'Write-Output "line1"',
        'Write-Output "line2 Chinese"',
        '$child = Start-Process -FilePath "powershell.exe" '
        '-ArgumentList "-NoProfile","-Command","Start-Sleep -Seconds 10" '
        '-WindowStyle Hidden -PassThru',
        'Write-Output "child started"',
        'Write-Output "script exiting"',
        'exit 0',
    ]) + "\n",
    encoding="utf-8",
)


class Fake:
    def __init__(self) -> None:
        self.repo_root = tmp
        self._shell: list[str] = []
        self._output_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()

    # 复用被测类的方法
    _tail_script_output = c.TraderConsole._tail_script_output

    def _log_console(self, *args) -> None:  # noqa: ANN002, ANN003
        pass


fake = Fake()
t0 = time.time()
c.TraderConsole._run_script(fake, script, [], label="测试")
done = False
lines: list[str] = []
while time.time() - t0 < 20:
    try:
        kind, value = fake._output_queue.get(timeout=0.5)
    except queue.Empty:
        continue
    if kind == "log":
        lines.append(str(value))
    elif kind == "done":
        done = True
        break
    elif kind == "error":
        lines.append("ERR: " + str(value))

assert done, f"done event never emitted (buttons would stay frozen)! lines={lines!r}"
assert any("line2 Chinese" in ln for ln in lines), lines
assert any("script exiting" in ln for ln in lines), lines
print(f"PASS: done emitted in {time.time() - t0:.1f}s while child still alive; "
      f"{len(lines)} log lines forwarded")
print("last line:", lines[-1])
