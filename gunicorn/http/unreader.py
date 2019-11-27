# -*- coding: utf-8 -
#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

import io
import os


# done 改成 Unreadable 更合适
class Unreader(object):
    """
    Classes that can undo reading data from a given type of data source.
    """
    def __init__(self):
        self.buf = io.BytesIO()

    def chunk(self):
        # 读取数据
        raise NotImplementedError()

    def read(self, size=None):
        if size is not None and not isinstance(size, int):
            raise TypeError("size parameter must be an int or long.")

        if size is not None:
            if size == 0:
                return b""
            if size < 0:                # 不限制读取的数据长度
                size = None

        self.buf.seek(0, os.SEEK_END)   # 移到末尾

        # buf中有数据, 全部返回
        if size is None and self.buf.tell():
            ret = self.buf.getvalue()
            self.buf = io.BytesIO()
            return ret

        # buf中无数据, 读取全部数据返回
        if size is None:
            d = self.chunk()
            return d

        # 每次读取size长度的数据
        while self.buf.tell() < size:
            chunk = self.chunk()
            if not chunk:
                ret = self.buf.getvalue()
                self.buf = io.BytesIO()
                return ret
            # 上面seek到了末尾, 每次写入后tell的结果都是最新(最后)的位置
            self.buf.write(chunk)

        data = self.buf.getvalue()
        self.buf = io.BytesIO()
        # buf中的数据量>=size, 将0~size返回, 减将size~N写入buf
        self.buf.write(data[size:])
        return data[:size]

    def unread(self, data):
        self.buf.seek(0, os.SEEK_END)
        self.buf.write(data)


# done
class SocketUnreader(Unreader):
    def __init__(self, sock, max_chunk=8192):
        super().__init__()
        self.sock = sock
        self.mxchunk = max_chunk

    def chunk(self):
        return self.sock.recv(self.mxchunk)


# done
class IterUnreader(Unreader):
    def __init__(self, iterable):
        super().__init__()
        self.iter = iter(iterable)

    def chunk(self):
        if not self.iter:
            return b""
        try:
            return next(self.iter)
        except StopIteration:
            self.iter = None
            return b""
