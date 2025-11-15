"""sshubl.st_utils test module"""

import unittest
from pathlib import PurePosixPath, PureWindowsPath
from unittest.mock import MagicMock, patch

import sublime_plugin

from ..sshubl.commands import SshTerminalCommand
from ..sshubl.st_utils import (
    format_ip_addr,
    get_absolute_purepath_flavour,
    get_command_class,
    parse_ssh_connection,
    pretty_forward_target,
    validate_forward_target,
)


class TestStUtils(unittest.TestCase):
    """st_utils test cases"""

    def test_get_command_class(self) -> None:
        """get_command_class test cases"""

        class TestFakeCommand(sublime_plugin.WindowCommand):
            pass

        window_command_classes_mock = MagicMock()
        window_command_classes_mock.__iter__.return_value = [
            TestFakeCommand,
            SshTerminalCommand,
        ]

        with patch(
            "SSHubl.sshubl.st_utils.sublime_plugin.window_command_classes",
            window_command_classes_mock,
        ):
            # unknown command, looked up twice
            self.assertIsNone(get_command_class("_UnknownCommand"))
            self.assertIsNone(get_command_class("_UnknownCommand"))
            self.assertEqual(window_command_classes_mock.__iter__.call_count, 2)

            window_command_classes_mock.reset_mock()

            # SSHubl own `ssh_terminal` command, looked up and found once
            self.assertIs(get_command_class("SshTerminalCommand"), SshTerminalCommand)
            self.assertIs(get_command_class("SshTerminalCommand"), SshTerminalCommand)
            self.assertEqual(window_command_classes_mock.__iter__.call_count, 1)

            window_command_classes_mock.reset_mock()

    def test_format_ip_addr(self) -> None:
        """format_ip_addr test cases"""
        self.assertEqual(format_ip_addr("127.0.0.1"), "127.0.0.1")
        self.assertEqual(format_ip_addr("192.0.2.1"), "192.0.2.1")
        self.assertEqual(format_ip_addr("example.com"), "example.com")
        self.assertEqual(format_ip_addr("::1"), "[::1]")
        self.assertEqual(format_ip_addr("2001:db8::1"), "[2001:db8::1]")
        self.assertEqual(format_ip_addr("2001:0db8:0000:0000:0000:0000:0000:0001"), "[2001:db8::1]")

    def test_get_absolute_purepath_flavour(self) -> None:
        """get_absolute_purepath_flavour test cases"""
        self.assertIsInstance(get_absolute_purepath_flavour("/home/user"), PurePosixPath)
        self.assertIsInstance(get_absolute_purepath_flavour("c:/Program Files"), PureWindowsPath)
        self.assertIsInstance(get_absolute_purepath_flavour("//host/share"), PureWindowsPath)

        self.assertIsNone(get_absolute_purepath_flavour(""))
        self.assertIsNone(get_absolute_purepath_flavour("./rel/path"))

    @patch("SSHubl.sshubl.st_utils.getpass.getuser", return_value="login")
    def test_parse_ssh_connection(self, _) -> None:
        """parse_ssh_connection test cases"""
        self.assertTupleEqual(
            parse_ssh_connection("user:user@localhost:2200"),
            ("localhost", 2200, "user", "user"),
        )
        self.assertTupleEqual(
            parse_ssh_connection("user:@example.com"),
            ("example.com", 22, "user", ""),
        )
        self.assertTupleEqual(
            parse_ssh_connection("example.com:2200"),
            ("example.com", 2200, "login", None),
        )

        self.assertRaises(ValueError, parse_ssh_connection, "[::1]:test")
        self.assertRaises(ValueError, parse_ssh_connection, "::1]:22")

    def test_validate_forward_target(self) -> None:
        """validate_forward_target test cases"""
        self.assertTrue(validate_forward_target("42"))
        self.assertTrue(validate_forward_target("*:42"))
        self.assertTrue(validate_forward_target("[*]:42"))
        self.assertTrue(validate_forward_target("[::1]:42"))
        self.assertTrue(validate_forward_target("127.0.0.1:42"))
        self.assertTrue(validate_forward_target("localhost:42"))
        self.assertTrue(validate_forward_target("[localhost]:42"))
        self.assertTrue(validate_forward_target("example.com:42"))
        self.assertTrue(validate_forward_target("./unix.socket"))
        self.assertTrue(validate_forward_target("/path/to/unix.socket"))

        self.assertFalse(validate_forward_target("42:[::1]"))
        self.assertFalse(validate_forward_target("[::1]:42:42"))
        self.assertFalse(validate_forward_target("42:[::1]:42"))
        self.assertFalse(validate_forward_target("42:42:[::1]"))
        self.assertFalse(validate_forward_target("idon'tknowwhattotype:42"))
        self.assertFalse(validate_forward_target("example com:42"))

    def test_pretty_forward_target(self) -> None:
        """pretty_forward_target test cases"""
        self.assertEqual(pretty_forward_target("42"), "42")
        self.assertEqual(pretty_forward_target("*:42"), "42")
        self.assertEqual(pretty_forward_target("[*]:42"), "42")
        self.assertEqual(pretty_forward_target("[::1]:42"), "42")
        self.assertEqual(pretty_forward_target("127.0.0.1:42"), "42")
        self.assertEqual(pretty_forward_target("localhost:42"), "42")
        self.assertEqual(pretty_forward_target("[localhost]:42"), "42")
        self.assertEqual(pretty_forward_target("192.0.2.1:42"), "192.0.2.1:42")
        self.assertEqual(pretty_forward_target("example.com:42"), "example.com:42")
        self.assertEqual(pretty_forward_target("./unix.socket"), "./unix.socket")
        self.assertEqual(pretty_forward_target("/path/to/unix.socket"), "/path/to/unix.socket")
