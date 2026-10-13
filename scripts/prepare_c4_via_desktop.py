"""Prepare the exact C4 fixture through a temporary desktop network tunnel."""
import os
import select
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 18771


class Proxy(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_CONNECT(self):
        host, port = self.path.rsplit(':', 1)
        try:
            upstream = socket.create_connection((host, int(port)), timeout=30)
        except OSError:
            self.send_error(502)
            return
        self.send_response(200)
        self.end_headers()
        self.connection.setblocking(False)
        upstream.setblocking(False)
        try:
            while True:
                ready, _, _ = select.select([self.connection, upstream], [], [], 60)
                if not ready:
                    return
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    target = upstream if source is self.connection else self.connection
                    target.setblocking(True)
                    target.sendall(data)
                    target.setblocking(False)
        finally:
            upstream.close()


def main():
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Proxy)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tunnel = subprocess.Popen(['ssh', '-N', '-o', 'ExitOnForwardFailure=yes',
                               '-R', f'127.0.0.1:{PORT}:127.0.0.1:{PORT}', 'jinan40'])
    try:
        import time
        time.sleep(1)
        if tunnel.poll() is not None:
            raise RuntimeError('Fixture tunnel did not start')
        env = dict(os.environ, TB21_FIXTURE_PROXY=f'http://127.0.0.1:{PORT}')
        subprocess.run([sys.executable, str(Path(__file__).with_name('desktop_tb21_remote.py')),
                        'prepare-c4-cache'], env=env, check=True)
    finally:
        tunnel.terminate()
        tunnel.wait(timeout=10)
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
