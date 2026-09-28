"""实时看板只通过短 JSON 提供当前事实。"""
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

from codesign_lab.dashboard import DashboardHandler


class DashboardChecks(unittest.TestCase):
    def test_page_and_live_api_are_small_and_readable(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),DashboardHandler)
        worker=threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        try:
            root=f'http://127.0.0.1:{server.server_port}'
            with urlopen(root+'/',timeout=5) as response:
                page=response.read()
                self.assertIn('text/html',response.headers['Content-Type'])
            self.assertLess(len(page),30000)
            self.assertIn('实时优化看板'.encode(),page)
            with urlopen(root+'/api/status',timeout=5) as response:
                payload=response.read()
                self.assertIn('application/json',response.headers['Content-Type'])
            self.assertLess(len(payload),30000)
            data=json.loads(payload)
            self.assertIn('best',data)
            self.assertIn('trend',data)
            self.assertIn('tasks',data)
            self.assertEqual(response.headers['Cache-Control'],'no-store')
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
