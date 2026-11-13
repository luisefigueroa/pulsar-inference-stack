"""Start's port probe (scripts/lib.sh port_free) against real sockets on this
machine: free once only closing connections remain after a listener stops, in
use while a program listens, and not checked when the probe cannot run. The
remote form runs the same probe through an SSH double; nothing leaves
localhost."""
import errno
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
FREE, IN_USE, NOT_CHECKED = 0, 1, 2


class PortProbe(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # An SSH double: runs the remote command here, or fails as SSH does.
        self.ssh = self.root / "ssh"
        self.ssh.write_text('#!/usr/bin/env bash\n[ -z "${SSH_FAILS:-}" ] || exit 255\n'
                            'while [ "$1" != -- ]; do shift; done; shift 2\nexec bash -c "$*"\n')
        self.ssh.chmod(0o755)

    def probe(self, port, host=None, **env):
        script = f'. "{ROOT}/scripts/lib.sh"\nrc=0\nport_free {port} {host or ""} || rc=$?\necho "$rc"\n'
        result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, timeout=60,
                                env={**os.environ, "PULSAR_SSH": str(self.ssh), **env})
        return int(result.stdout.strip().splitlines()[-1])

    def both(self, port):
        return self.probe(port), self.probe(port, "node-b")

    def listener(self, address=("0.0.0.0", 0), reuse=True, family=socket.AF_INET):
        server = socket.socket(family)
        if reuse:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(address)
        server.listen()
        self.addCleanup(server.close)
        return server, server.getsockname()[1]

    @staticmethod
    def plain_bind(port):
        """What the earlier probe did: a bind without SO_REUSEADDR."""
        probe = socket.socket()
        try:
            probe.bind(("0.0.0.0", port))
            return True
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
            return False
        finally:
            probe.close()

    def test_a_port_freed_by_a_stop_while_connections_close_is_free(self):
        # The engine's master port with the other rank connected, then the listener side stops first.
        server, port = self.listener()
        client = socket.create_connection(("127.0.0.1", port))
        accepted, _ = server.accept()
        accepted.close()
        server.close()
        time.sleep(0.1)
        client.close()
        time.sleep(0.2)
        # The cause: the closing connection (TIME_WAIT) refuses a plain bind, which read as in use.
        self.assertFalse(self.plain_bind(port))
        self.assertEqual(self.both(port), (FREE, FREE))

    def test_a_port_a_program_listens_on_is_in_use(self):
        for name, address, reuse in (("any address, SO_REUSEADDR", ("0.0.0.0", 0), True),
                                     ("any address", ("0.0.0.0", 0), False),
                                     ("loopback only", ("127.0.0.1", 0), False)):
            with self.subTest(listener=name):
                server, port = self.listener(address, reuse)
                self.assertEqual(self.both(port), (IN_USE, IN_USE))
                server.close()
        if socket.has_ipv6:
            try:
                server, port = self.listener(("::", 0), True, socket.AF_INET6)
            except OSError:
                self.skipTest("no IPv6 here")
            # A dual-stack listener holds the IPv4 port too.
            if server.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 0:
                self.assertEqual(self.both(port), (IN_USE, IN_USE))

    def test_a_port_that_was_never_used_is_free(self):
        server, port = self.listener()
        server.close()
        self.assertEqual(self.both(port), (FREE, FREE))

    def test_a_node_where_the_probe_cannot_run_is_not_checked_not_in_use(self):
        _, port = self.listener()
        self.assertEqual(self.probe(port, "node-b", SSH_FAILS="1"), NOT_CHECKED)
        # The probe itself failing on the node (here, a port it cannot read) is not "in use" either.
        self.assertEqual(self.probe("not-a-port", "node-b"), NOT_CHECKED)
        self.assertEqual(self.probe("not-a-port"), NOT_CHECKED)


if __name__ == "__main__":
    unittest.main()
