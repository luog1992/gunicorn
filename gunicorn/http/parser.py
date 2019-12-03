# -*- coding: utf-8 -
#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

from gunicorn.http.message import Request
from gunicorn.http.unreader import SocketUnreader, IterUnreader


class Parser(object):

    mesg_class = None

    def __init__(self, cfg, source):
        self.cfg = cfg
        if hasattr(source, "recv"):
            self.unreader = SocketUnreader(source)
        else:
            self.unreader = IterUnreader(source)
        self.mesg = None
        # request counter (for keepalive connections)
        self.req_count = 0

    def __iter__(self):
        return self

    # done
    def __next__(self):
        # Stop if HTTP dictates a stop. 关闭连接
        if self.mesg and self.mesg.should_close():
            raise StopIteration()

        # Discard any unread body of the previous message
        if self.mesg:
            # 对于有Content-length的请求, 读取所有数据(到body.buf)
            # 对于chunked的请求, 也一直读取数据直到没有更多数据
            data = self.mesg.body.read(8192)
            while data:
                data = self.mesg.body.read(8192)

        # Parse the next request
        # 如果是长连接的话, 会在同一个连接中处理多个请求
        self.req_count += 1
        self.mesg = self.mesg_class(self.cfg, self.unreader, self.req_count)
        if not self.mesg:   # todo: self.mesg 必存在啊!?
            raise StopIteration()
        return self.mesg

    next = __next__


class RequestParser(Parser):

    mesg_class = Request
