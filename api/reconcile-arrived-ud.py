import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _handlers import handle_options, handle_post


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        handle_post(self, 'ud', 'arrived')

    def do_OPTIONS(self):
        handle_options(self)
