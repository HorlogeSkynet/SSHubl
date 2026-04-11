"""sshubl.ssh_utils test module"""

import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from ..sshubl.ssh_utils import ssh_forward


class TestSshUtils(unittest.TestCase):
    """ssh_utils test cases"""

    def test_ssh_forward(self) -> None:
        """ssh_forward test cases"""

        identifier = uuid.uuid4()

        # below forwarding request/cancellation will succeed
        with patch("SSHubl.sshubl.ssh_utils.subprocess.check_output") as check_output_mock:
            check_output_mock.return_value = ""

            # "-O forward -L 127.0.0.1:8888:127.0.0.1:22"
            self.assertEqual(
                ssh_forward(
                    identifier,
                    do_open=True,
                    is_reverse=False,
                    target_1="127.0.0.1:8888",
                    target_2="127.0.0.1:22",
                ),
                {
                    "is_reverse": False,
                    "orig_target_1": "127.0.0.1:8888",
                    "orig_target_2": "127.0.0.1:22",
                    "target_local": "127.0.0.1:8888",
                    "target_remote": "127.0.0.1:22",
                },
            )

            # for below test case, mock a local UNIX domain socket (we had opened forward to) which
            # will get automatically removed
            with tempfile.NamedTemporaryFile(suffix=".unix", delete=False) as tmpfile:
                local_unix_socket = Path(tmpfile.name)

                # "-O cancel -L /tmp/remote.unix:$local_unix_socket"
                self.assertEqual(
                    ssh_forward(
                        identifier,
                        do_open=False,
                        is_reverse=False,
                        target_1=str(local_unix_socket),
                        target_2="/tmp/remote.unix",
                    ),
                    {
                        "is_reverse": False,
                        "orig_target_1": str(local_unix_socket),
                        "orig_target_2": "/tmp/remote.unix",
                        "target_local": str(local_unix_socket),
                        "target_remote": "/tmp/remote.unix",
                    },
                )
                self.assertFalse(local_unix_socket.exists())

        # below forwarding request causes 4242 port to be allocated by remote (printed to stdout)
        with patch("SSHubl.sshubl.ssh_utils.subprocess.check_output") as check_output_mock:
            check_output_mock.return_value = "4242\n"

            # "-O forward -R 127.0.0.1:0:[::1]:8888"
            self.assertEqual(
                ssh_forward(
                    identifier,
                    do_open=True,
                    is_reverse=True,
                    target_1="127.0.0.1:0",
                    target_2="[::1]:8888",
                ),
                {
                    "is_reverse": True,
                    "orig_target_1": "127.0.0.1:0",
                    "orig_target_2": "[::1]:8888",
                    "target_local": "[::1]:8888",
                    "target_remote": "127.0.0.1:4242",
                },
            )

        # below forwarding request fails
        with patch("SSHubl.sshubl.ssh_utils.subprocess.check_output") as check_output_mock:
            check_output_mock.side_effect = subprocess.CalledProcessError(
                1,
                "",
                "mux_client_forward: forwarding request failed:"
                " remote port forwarding failed for listen port 42",
            )

            self.assertIsNone(
                ssh_forward(
                    identifier,
                    do_open=True,
                    is_reverse=True,
                    target_1="127.0.0.1:42",
                    target_2="127.0.0.1:22",
                )
            )
