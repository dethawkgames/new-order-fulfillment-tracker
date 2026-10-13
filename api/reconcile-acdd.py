import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from _handlers import make_handler

handler = make_handler('acdd', 'shipped')
