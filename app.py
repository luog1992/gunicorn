# encoding: utf-8

"""
一个用于调试的WSGI App
"""


class Application(object):

    def __call__(self, environ, start_fn):
        start_fn('200 OK', [('Content-Type', 'text/plain')])
        return [b"Hello, Gunicorn!\n"]


app = Application()
