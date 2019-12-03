# -*- coding: utf-8 -
#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

import io
import re
import socket
from errno import ENOTCONN

from gunicorn.http.unreader import SocketUnreader
from gunicorn.http.body import ChunkedReader, LengthReader, EOFReader, Body
from gunicorn.http.errors import (InvalidHeader, InvalidHeaderName, NoMoreData,
    InvalidRequestLine, InvalidRequestMethod, InvalidHTTPVersion,
    LimitRequestLine, LimitRequestHeaders)
from gunicorn.http.errors import InvalidProxyLine, ForbiddenProxyRequest
from gunicorn.http.errors import InvalidSchemeHeaders
from gunicorn.util import bytes_to_str, split_request_uri

MAX_REQUEST_LINE = 8190
MAX_HEADERS = 32768
DEFAULT_MAX_HEADERFIELD_SIZE = 8190

# 就不能改成 BAD_HEADER_RE 么
HEADER_RE = re.compile(r"[\x00-\x1F\x7F()<>@,;:\[\]={} \t\\\"]")
METH_RE = re.compile(r"[A-Z0-9$-_.]{3,20}")
VERSION_RE = re.compile(r"HTTP/(\d+)\.(\d+)")


class Message(object):
    """请求(报文)"""

    def __init__(self, cfg, unreader):
        self.cfg = cfg
        self.unreader = unreader
        self.version = None
        self.headers = []
        self.trailers = []
        self.body = None
        self.scheme = "https" if cfg.is_ssl else "http"

        # set headers limits
        _limit = self.limit_request_fields = cfg.limit_request_fields
        if _limit <= 0 or _limit > MAX_HEADERS:
            self.limit_request_fields = MAX_HEADERS
        self.limit_request_field_size = cfg.limit_request_field_size
        if self.limit_request_field_size < 0:
            self.limit_request_field_size = DEFAULT_MAX_HEADERFIELD_SIZE

        # set max header buffer size
        _max = self.limit_request_field_size or DEFAULT_MAX_HEADERFIELD_SIZE
        self.max_buffer_headers = self.limit_request_fields * (_max + 2) + 4

        unused = self.parse(self.unreader)  # (一般是)请求体
        self.unreader.unread(unused)
        self.set_body_reader()

    # 解析请求行, 请求头, 返回请求体
    def parse(self, unreader):
        raise NotImplementedError()

    # done 返回如: [('Connection', 'keep-alive')]
    def parse_headers(self, data):
        cfg = self.cfg
        headers = []

        # Split lines on \r\n keeping the \r\n on each line
        lines = [bytes_to_str(line) + "\r\n" for line in data.split(b"\r\n")]

        # handle scheme headers
        scheme_header = False
        secure_scheme_headers = {}
        if '*' in cfg.forwarded_allow_ips:
            secure_scheme_headers = cfg.secure_scheme_headers
        elif isinstance(self.unreader, SocketUnreader):
            remote_addr = self.unreader.sock.getpeername()
            if self.unreader.sock.family in (socket.AF_INET, socket.AF_INET6):
                remote_host = remote_addr[0]
                if remote_host in cfg.forwarded_allow_ips:
                    secure_scheme_headers = cfg.secure_scheme_headers
            elif self.unreader.sock.family == socket.AF_UNIX:
                secure_scheme_headers = cfg.secure_scheme_headers

        # Parse headers into key/value pairs paying attention
        # to continuation lines.
        while lines:
            if len(headers) >= self.limit_request_fields:
                raise LimitRequestHeaders("limit request headers fields")

            # Parse initial header name : value pair.
            curr = lines.pop(0)
            header_length = len(curr)
            if curr.find(":") < 0:
                raise InvalidHeader(curr.strip())
            name, value = curr.split(":", 1)
            name = name.rstrip(" \t").upper()
            if HEADER_RE.search(name):
                raise InvalidHeaderName(name)

            name, value = name.strip(), [value.lstrip()]

            # Consume value continuation lines
            while lines and lines[0].startswith((" ", "\t")):
                curr = lines.pop(0)
                header_length += len(curr)
                if header_length > self.limit_request_field_size > 0:
                    raise LimitRequestHeaders(
                        "limit request headers fields size")
                value.append(curr)
            value = ''.join(value).rstrip()

            if header_length > self.limit_request_field_size > 0:
                raise LimitRequestHeaders("limit request headers fields size")

            if name in secure_scheme_headers:
                secure = value == secure_scheme_headers[name]
                scheme = "https" if secure else "http"
                if scheme_header:
                    if scheme != self.scheme:
                        raise InvalidSchemeHeaders()
                else:
                    scheme_header = True
                    self.scheme = scheme

            headers.append((name, value))

        return headers

    # 不同的 Transfer-encoding 有不同的接受数据的方式
    def set_body_reader(self):
        chunked = False
        content_length = None
        for (name, value) in self.headers:
            if name == "CONTENT-LENGTH":
                content_length = value
            elif name == "TRANSFER-ENCODING":
                chunked = value.lower() == "chunked"
            elif name == "SEC-WEBSOCKET-KEY1":
                content_length = 8

        if chunked:
            self.body = Body(ChunkedReader(self, self.unreader))
        elif content_length is not None:
            try:
                content_length = int(content_length)
            except ValueError:
                raise InvalidHeader("CONTENT-LENGTH", req=self)

            if content_length < 0:
                raise InvalidHeader("CONTENT-LENGTH", req=self)

            self.body = Body(LengthReader(self.unreader, content_length))
        else:
            self.body = Body(EOFReader(self.unreader))

    # done
    def should_close(self):
        for (h, v) in self.headers:
            if h == "CONNECTION":
                v = v.lower().strip()
                if v == "close":
                    return True
                elif v == "keep-alive":
                    return False
                break
        return self.version <= (1, 0)


class Request(Message):
    """请求"""

    def __init__(self, cfg, unreader, req_number=1):
        self.method = None
        self.uri = None
        self.path = None
        self.query = None
        self.fragment = None

        # get max request line size
        _max = self.limit_request_line = cfg.limit_request_line
        if _max < 0 or _max >= MAX_REQUEST_LINE:
            self.limit_request_line = MAX_REQUEST_LINE

        # 一个长连接中处理的请求数量
        self.req_number = req_number
        self.proxy_protocol_info = None
        super().__init__(cfg, unreader)

    # done, 该是改成 read_data or fetch_data 吧
    def get_data(self, unreader, buf, stop=False):
        data = unreader.read()
        if not data:
            if stop:
                raise StopIteration()
            raise NoMoreData(buf.getvalue())
        buf.write(data)

    # done 处理请求行, 请求头, 返回请求体数据
    def parse(self, unreader):
        buf = io.BytesIO()
        self.get_data(unreader, buf, stop=True)

        # get request line, rbuf: 一行 \r\n 后面多余的数据
        line, rbuf = self.read_line(unreader, buf, self.limit_request_line)

        # todo: proxy protocol
        if self.proxy_protocol(bytes_to_str(line)):
            # get next request line
            buf = io.BytesIO()
            buf.write(rbuf)
            line, rbuf = self.read_line(unreader, buf, self.limit_request_line)

        # 处理请求行, 如: b'GET /test-db/1 HTTP/1.1'
        self.parse_request_line(line)
        buf = io.BytesIO()
        buf.write(rbuf)

        # data 如: b'Host: localhost:8000\r\nAccept: */*\r\n\r\na=1&b=2'
        # 既包括请求头, 也包括请求体(a=1&b=2)
        data = buf.getvalue()
        idx = data.find(b"\r\n\r\n")
        done = data[:2] == b"\r\n"
        while True:
            idx = data.find(b"\r\n\r\n")
            # todo: 什么时候data的前两位会是\r\n呢?
            done = data[:2] == b"\r\n"
            if idx < 0 and not done:
                self.get_data(unreader, buf)
                data = buf.getvalue()
                if len(data) > self.max_buffer_headers:
                    raise LimitRequestHeaders("max buffer headers")
            else:  # idx >= 0 or done
                break

        if done:
            self.unreader.unread(data[2:])
            return b""

        # 请求头
        self.headers = self.parse_headers(data[:idx])
        # 请求体
        ret = data[idx + 4:]
        buf = None
        return ret

    # done
    def read_line(self, unreader, buf, limit=0):
        data = buf.getvalue()

        while True:
            idx = data.find(b"\r\n")
            if idx >= 0:
                # check if the request line is too large
                # 这个limit默认值我也是醉了...
                if idx > limit > 0:
                    raise LimitRequestLine(idx, limit)
                # 循环读取, 直到出现 \r\n
                break
            if len(data) - 2 > limit > 0:
                raise LimitRequestLine(len(data), limit)
            # 否则继续读取数据
            self.get_data(unreader, buf)
            data = buf.getvalue()

        # 读取到的数据可能如 abc \r\n def
        return (data[:idx],      # request line,
                data[idx + 2:])  # residue in the buffer, skip \r\n

    # done
    def proxy_protocol(self, line):
        """\
        Detect, check and parse proxy protocol.

        :raises: ForbiddenProxyRequest, InvalidProxyLine.
        :return: True for proxy protocol line else False
        """
        if not self.cfg.proxy_protocol:
            return False
        if self.req_number != 1:
            return False
        if not line.startswith("PROXY"):
            return False

        self.proxy_protocol_access_check()
        self.parse_proxy_protocol(line)
        return True

    # done
    def proxy_protocol_access_check(self):
        # check in allow list
        if not isinstance(self.unreader, SocketUnreader):
            return
        try:
            remote_host = self.unreader.sock.getpeername()[0]
        except socket.error as e:
            if e.args[0] == ENOTCONN:  # Transport endpoint is not connected
                raise ForbiddenProxyRequest("UNKNOW")
            raise
        _ips = self.cfg.proxy_allow_ips
        if "*" not in  _ips and remote_host not in _ips:
            raise ForbiddenProxyRequest(remote_host)

    # done
    def parse_proxy_protocol(self, line):
        # PROXY 协议栈 源IP 目的IP 源端口 目的端口rn
        bits = line.split()
        if len(bits) != 6:
            raise InvalidProxyLine(line)

        # Extract data
        proto = bits[1]
        s_addr = bits[2]
        d_addr = bits[3]

        # Validation
        if proto not in ["TCP4", "TCP6"]:
            raise InvalidProxyLine("protocol '%s' not supported" % proto)
        if proto == "TCP4":
            try:
                socket.inet_pton(socket.AF_INET, s_addr)
                socket.inet_pton(socket.AF_INET, d_addr)
            except socket.error:
                raise InvalidProxyLine(line)
        elif proto == "TCP6":
            try:
                socket.inet_pton(socket.AF_INET6, s_addr)
                socket.inet_pton(socket.AF_INET6, d_addr)
            except socket.error:
                raise InvalidProxyLine(line)

        try:
            s_port = int(bits[4])
            d_port = int(bits[5])
        except ValueError:
            raise InvalidProxyLine("invalid port %s" % line)

        if not ((0 <= s_port <= 65535) and (0 <= d_port <= 65535)):
            raise InvalidProxyLine("invalid port %s" % line)

        # Set data
        self.proxy_protocol_info = {
            "proxy_protocol": proto,
            "client_addr": s_addr,
            "client_port": s_port,
            "proxy_addr": d_addr,
            "proxy_port": d_port
        }

    # done: 处理请求行, 如: b'GET /books/1 HTTP/1.1'
    def parse_request_line(self, line_bytes):
        bits = [bytes_to_str(bit) for bit in line_bytes.split(None, 2)]
        if len(bits) != 3:
            raise InvalidRequestLine(bytes_to_str(line_bytes))

        if not METH_RE.match(bits[0]):
            raise InvalidRequestMethod(bits[0])
        self.method = bits[0].upper()

        self.uri = bits[1]
        try:
            parts = split_request_uri(self.uri)
        except ValueError:
            raise InvalidRequestLine(bytes_to_str(line_bytes))
        self.path = parts.path or ""
        self.query = parts.query or ""
        self.fragment = parts.fragment or ""

        # HTTP Version
        match = VERSION_RE.match(bits[2])
        if match is None:
            raise InvalidHTTPVersion(bits[2])
        self.version = (int(match.group(1)), int(match.group(2)))

    def set_body_reader(self):
        super().set_body_reader()
        if isinstance(self.body.reader, EOFReader):
            # todo EOFReader 转换成 0 LengthReader why?
            self.body = Body(LengthReader(self.unreader, 0))
