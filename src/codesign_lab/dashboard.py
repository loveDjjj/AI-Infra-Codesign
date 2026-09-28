"""只读本地看板服务；浏览器每五秒读取精简状态。"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .report import live_snapshot

PAGE=Path(__file__).parent/'static/dashboard.html'


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path=urlsplit(self.path).path
        if path in {'/','/dashboard'}:
            body=PAGE.read_bytes();content_type='text/html; charset=utf-8'
        elif path=='/api/status':
            body=json.dumps(live_snapshot(),ensure_ascii=False,allow_nan=False).encode()
            content_type='application/json; charset=utf-8'
        else:
            self.send_error(404,'页面不存在')
            return
        self.send_response(200)
        self.send_header('Content-Type',content_type)
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self,format,*args):
        # 高频轮询不逐条刷终端；启动和错误由调用方可见。
        return


def serve(host='127.0.0.1',port=8765):
    with ThreadingHTTPServer((host,port),DashboardHandler) as server:
        print(f'看板已启动：http://{host}:{server.server_port}/',flush=True)
        try:
            server.serve_forever(poll_interval=.5)
        except KeyboardInterrupt:
            pass
