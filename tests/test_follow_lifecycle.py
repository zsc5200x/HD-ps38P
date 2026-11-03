"""跟单占用者存活检查；完全模拟操作系统调用，不接触真实进程。"""

import unittest
from unittest.mock import MagicMock, patch

from ps38app.follow.service import _owner_alive, _windows_process_alive


class ProcessLivenessTests(unittest.TestCase):
    def test_windows_owner_uses_readonly_query_without_os_kill(self):
        for alive in (True, False):
            with (
                self.subTest(alive=alive),
                patch("ps38app.follow.service.os.name", "nt"),
                patch("ps38app.follow.service.os.getpid", return_value=100),
                patch("ps38app.follow.service.os.kill") as kill,
                patch("ps38app.follow.service._windows_process_alive", return_value=alive) as query,
            ):
                self.assertIs(_owner_alive("foreign-owner", 200), alive)
                query.assert_called_once_with(200)
                kill.assert_not_called()

    def test_nonpositive_owner_pid_is_dead_without_process_calls(self):
        for pid in (0, -1):
            with (
                self.subTest(pid=pid),
                patch("ps38app.follow.service.os.name", "nt"),
                patch("ps38app.follow.service.os.getpid", return_value=100),
                patch("ps38app.follow.service.os.kill") as kill,
                patch("ps38app.follow.service._windows_process_alive") as query,
            ):
                self.assertFalse(_owner_alive("invalid-owner", pid))
                query.assert_not_called()
                kill.assert_not_called()

    def test_windows_invalid_pid_is_dead_and_ambiguous_open_failure_stays_alive(self):
        for error, expected in ((87, False), (1168, False), (5, True), (1, True)):
            kernel = MagicMock()
            kernel.OpenProcess.return_value = None
            with (
                self.subTest(error=error),
                patch("ctypes.WinDLL", return_value=kernel, create=True) as loader,
                patch("ctypes.get_last_error", return_value=error, create=True),
                patch("ps38app.follow.service.os.kill") as kill,
            ):
                self.assertIs(_windows_process_alive(200), expected)
                loader.assert_called_once_with("kernel32", use_last_error=True)
                kernel.OpenProcess.assert_called_once_with(0x1000, False, 200)
                kernel.GetExitCodeProcess.assert_not_called()
                kernel.CloseHandle.assert_not_called()
                kill.assert_not_called()

    def test_windows_exit_code_query_always_closes_open_handle(self):
        for exit_code, expected in ((259, True), (0, False), (1, False)):
            kernel = MagicMock()
            kernel.OpenProcess.return_value = 1234

            def get_exit_code(handle, pointer):
                self.assertEqual(handle, 1234)
                pointer._obj.value = exit_code
                return True

            kernel.GetExitCodeProcess.side_effect = get_exit_code
            with (
                self.subTest(exit_code=exit_code),
                patch("ctypes.WinDLL", return_value=kernel, create=True),
                patch("ps38app.follow.service.os.kill") as kill,
            ):
                self.assertIs(_windows_process_alive(200), expected)
                kernel.GetExitCodeProcess.assert_called_once()
                kernel.CloseHandle.assert_called_once_with(1234)
                kill.assert_not_called()

    def test_windows_query_failure_stays_alive_and_closes_open_handle(self):
        for failure in (False, OSError("模拟查询失败")):
            kernel = MagicMock()
            kernel.OpenProcess.return_value = 1234
            if isinstance(failure, Exception):
                kernel.GetExitCodeProcess.side_effect = failure
            else:
                kernel.GetExitCodeProcess.return_value = failure
            with (
                self.subTest(failure=failure),
                patch("ctypes.WinDLL", return_value=kernel, create=True),
                patch("ps38app.follow.service.os.kill") as kill,
            ):
                self.assertTrue(_windows_process_alive(200))
                kernel.CloseHandle.assert_called_once_with(1234)
                kill.assert_not_called()

    def test_unavailable_windows_api_conservatively_preserves_owner(self):
        for failure in (AttributeError("模拟缺少Windows接口"), OSError("模拟接口不可用")):
            with (
                self.subTest(failure=failure),
                patch("ctypes.WinDLL", side_effect=failure, create=True),
                patch("ps38app.follow.service.os.kill") as kill,
            ):
                self.assertTrue(_windows_process_alive(200))
                kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
