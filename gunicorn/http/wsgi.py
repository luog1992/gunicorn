# -*- coding: utf-8 -
#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

import io
import logging
import os
import re
import sys

from gunicorn.http.message import HEADER_RE
from gunicorn.http.errors import InvalidHeader, InvalidHeaderName
from gunicorn import SERVER_SOFTWARE
import gunicorn.util as util

# Send files in at most 1GB blocks as some operating systems can have problems
# with sending files in blocks over 2GB.
BLKSIZE = 0x3FFFFFFF

# exclude control character, 就不能改成 BAD_HEADER_VALUE_RE 么
HEADER_VALUE_RE = re.compile(r'[\x00-\x1F\x7F]')

log = logging.getLogger(__name__)


class FileWrapper(object):

    def __init__(self, filelike, blksize=8192):
        self.filelike = filelike
        self.blksize = blksize
        if hasattr(filelike, 'close'):
            self.close = filelike.close

    def __getitem__(self, key):
        data = self.filelike.read(self.blksize)
        if data:
            return data
        raise IndexError


# done: 包装了一个IO, 可以将错误日志发的handlers
class WSGIErrorsWrapper(io.RawIOBase):

    def __init__(self, cfg):
        # There is no public __init__ method for RawIOBase so
        # we don't need to call super() in the __init__ method.
        # pylint: disable=super-init-not-called
        errorlog = logging.getLogger("gunicorn.error")
        handlers = errorlog.handlers
        self.streams = []

        if cfg.errorlog == "-":
            self.streams.append(sys.stderr)
            handlers = handlers[1:]  # 意思是第一个handler输出到了stderr

        for h in handlers:
            if hasattr(h, "stream"):
                self.streams.append(h.stream)

    def write(self, data):
        for stream in self.streams:
            try:
                stream.write(data)
            except UnicodeError:
                stream.write(data.encode("UTF-8"))
            stream.flush()


# done
def base_environ(cfg):
    return {
        "wsgi.errors": WSGIErrorsWrapper(cfg),
        "wsgi.version": (1, 0),
        "wsgi.multithread": False,
        "wsgi.multiprocess": (cfg.workers > 1),
        "wsgi.run_once": False,
        "wsgi.file_wrapper": FileWrapper,           # todo
        "wsgi.input_terminated": True,
        "SERVER_SOFTWARE": SERVER_SOFTWARE,
    }


# done
def default_environ(req, sock, cfg):
    env = base_environ(cfg)
    env.update({
        "wsgi.input": req.body,
        "gunicorn.socket": sock,
        "REQUEST_METHOD": req.method,
        "QUERY_STRING": req.query,
        "RAW_URI": req.uri,
        "SERVER_PROTOCOL": "HTTP/%s" % ".".join([str(v) for v in req.version])
    })
    return env


# done
def proxy_environ(req):
    info = req.proxy_protocol_info

    if not info:
        return {}

    return {
        "PROXY_PROTOCOL": info["proxy_protocol"],
        "REMOTE_ADDR": info["client_addr"],
        "REMOTE_PORT": str(info["client_port"]),
        "PROXY_ADDR": info["proxy_addr"],
        "PROXY_PORT": str(info["proxy_port"]),
    }


# environ example
"""
{'HTTP_ACCEPT': 'text/html,application/xhtml+xml,application/xml',
 'HTTP_ACCEPT_ENCODING': 'gzip, deflate, br',
 'HTTP_ACCEPT_LANGUAGE': 'zh,en-US;q=0.9,en;q=0.8,zh-CN;q=0.7',
 'HTTP_CACHE_CONTROL': 'max-age=0',
 'HTTP_CONNECTION': 'keep-alive',
 'HTTP_COOKIE': 'Pycharm-209c82f=83317ddd-a999-436e-a535-3239a0441752',
 'HTTP_DNT': '1',
 'HTTP_HOST': 'localhost:8000',
 'HTTP_SEC_FETCH_MODE': 'navigate',
 'HTTP_SEC_FETCH_SITE': 'none',
 'HTTP_SEC_FETCH_USER': '?1',
 'HTTP_UPGRADE_INSECURE_REQUESTS': '1',
 'HTTP_USER_AGENT': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_13_6)',
 'PATH_INFO': '/test-db/1',
 'QUERY_STRING': '',
 'RAW_URI': '/test-db/1',
 'REMOTE_ADDR': '127.0.0.1',
 'REMOTE_PORT': '56199',
 'REQUEST_METHOD': 'GET',
 'SCRIPT_NAME': '',
 'SERVER_NAME': '127.0.0.1',
 'SERVER_PORT': '8000',
 'SERVER_PROTOCOL': 'HTTP/1.1',
 'SERVER_SOFTWARE': 'gunicorn/20.0.0',
 'gunicorn.socket': <socket.socket fd=9, family=AddressFamily.AF_INET, type=SocketKind.SOCK_STREAM, proto=0, laddr=('127.0.0.1', 8000), raddr=('127.0.0.1', 56199)>,
 'wsgi.errors': <gunicorn.http.wsgi.WSGIErrorsWrapper object at 0x10e7484e0>,
 'wsgi.file_wrapper': <class 'gunicorn.http.wsgi.FileWrapper'>,
 'wsgi.input': <gunicorn.http.body.Body object at 0x10e7485f8>,
 'wsgi.input_terminated': True,
 'wsgi.multiprocess': False,
 'wsgi.multithread': False,
 'wsgi.run_once': False,
 'wsgi.url_scheme': 'http',
 'wsgi.version': (1, 0)}
"""


# done
def create(req, sock, client, server, cfg):
    """
    :param req: ``.message.Request`` 请求
    :param sock: client sock
    :param client: client addr, e.g. ``('127.0.0.1', 10000)``
    :param server: server sock, e.g. ``('127.0.0.1', 8000)``
    """
    resp = Response(req, sock, cfg)

    # set initial environ
    environ = default_environ(req, sock, cfg)

    # default variables
    host = None
    script_name = os.environ.get("SCRIPT_NAME", "")

    # add the headers to the environ
    for hdr_name, hdr_value in req.headers:
        if hdr_name == "EXPECT":
            # https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Expect
            if hdr_value.lower() == "100-continue":
                sock.send(b"HTTP/1.1 100 Continue\r\n\r\n")
        elif hdr_name == 'HOST':
            host = hdr_value
        elif hdr_name == "SCRIPT_NAME":
            script_name = hdr_value
        elif hdr_name == "CONTENT-TYPE":
            environ['CONTENT_TYPE'] = hdr_value
            continue
        elif hdr_name == "CONTENT-LENGTH":
            environ['CONTENT_LENGTH'] = hdr_value
            environ['wsgi.input_terminated'] = False
            continue

        # todo: 为啥要这么处理其他的请求头?
        key = 'HTTP_' + hdr_name.replace('-', '_')
        if key in environ:
            hdr_value = "%s,%s" % (environ[key], hdr_value)
        environ[key] = hdr_value

    # set the url scheme
    environ['wsgi.url_scheme'] = req.scheme

    # set the REMOTE_* keys in environ
    # authors should be aware that REMOTE_HOST and REMOTE_ADDR
    # may not qualify the remote addr: http://www.ietf.org/rfc/rfc3875
    if isinstance(client, str):
        environ['REMOTE_ADDR'] = client
    elif isinstance(client, bytes):
        environ['REMOTE_ADDR'] = client.decode()
    else:
        environ['REMOTE_ADDR'] = client[0]
        environ['REMOTE_PORT'] = str(client[1])

    # handle the SERVER_*
    # Normally only the application should use the Host header but since the
    # WSGI spec doesn't support unix sockets, we are using it to create
    # viable SERVER_* if possible.
    if isinstance(server, str):
        server = server.split(":")
        if len(server) == 1:
            # unix socket
            if host:
                server = host.split(':')
                if len(server) == 1:
                    if req.scheme == "http":
                        server.append(80)
                    elif req.scheme == "https":
                        server.append(443)
                    else:
                        server.append('')
            else:
                # no host header given which means that we are not behind a
                # proxy, so append an empty port.
                server.append('')
    environ['SERVER_NAME'] = server[0]
    environ['SERVER_PORT'] = str(server[1])

    # set the path and script name
    path_info = req.path
    if script_name:
        path_info = path_info.split(script_name, 1)[1]
    environ['PATH_INFO'] = util.unquote_to_wsgi_str(path_info)
    environ['SCRIPT_NAME'] = script_name

    # override the environ with the correct remote and server address if
    # we are behind a proxy using the proxy protocol.
    environ.update(proxy_environ(req))
    return resp, environ


class Response(object):

    def __init__(self, req, sock, cfg):
        self.req = req
        self.sock = sock    # client
        self.version = SERVER_SOFTWARE
        self.status = None
        self.status_code = None
        self.chunked = False
        self.must_close = False
        self.headers = []
        self.headers_sent = False
        self.response_length = None
        self.sent = 0               # 已发送数据长度

        # 是否upgrade连接. 连接可以以常用的协议启动（如HTTP/1.1），随后可通过upgrade
        # 协商再升级到HTTP2甚至是WebSockets. 比如要升级成websocket, 请求头可能如:
        # Connection: Upgrade
        # Upgrade: websocket
        # https://developer.mozilla.org/zh-CN/docs/Web/HTTP/Protocol_upgrade_mechanism
        self.upgrade = False
        self.cfg = cfg

    # done
    def force_close(self):
        self.must_close = True

    # done
    def should_close(self):
        if self.must_close or self.req.should_close():
            return True
        if self.response_length is not None or self.chunked:
            return False
        # todo: HEAD 204 304 为啥是 False
        if self.req.method == 'HEAD':
            return False
        if self.status_code < 200 or self.status_code in (204, 304):
            return False
        return True

    # done
    def start_response(self, status, headers, exc_info=None):
        if exc_info:
            try:
                if self.status and self.headers_sent:
                    util.reraise(exc_info[0], exc_info[1], exc_info[2])
            finally:
                exc_info = None
        elif self.status is not None:
            raise AssertionError("Response headers already set!")

        self.status = status

        # get the status code from the response here so we can use it to check
        # the need for the connection header later without parsing the string
        # each time.
        try:
            self.status_code = int(self.status.split()[0])
        except ValueError:
            self.status_code = None

        self.process_headers(headers)
        self.chunked = self.is_chunked()
        return self.write

    # done
    def process_headers(self, headers):
        for name, value in headers:
            if not isinstance(name, str):
                raise TypeError('%r is not a string' % name)
            if HEADER_RE.search(name):  # bad header
                raise InvalidHeaderName('%r' % name)
            lname = name.lower().strip()

            if not isinstance(value, str):
                raise TypeError('%r is not a string' % value)
            if HEADER_VALUE_RE.search(value):   # bad header value
                raise InvalidHeader('%r' % value)
            value = value.strip()

            if lname == "content-length":
                # 指定了content长度的请求
                self.response_length = int(value)
            elif util.is_hoppish(name):
                # 处理hop-by-hop请求头, 下面只处理了websocket的情况
                if lname == "connection":
                    # handle websocket
                    if value.lower().strip() == "upgrade":
                        self.upgrade = True
                elif lname == "upgrade":
                    if value.lower().strip() == "websocket":
                        self.headers.append((name.strip(), value))

                # ignore hopbyhop headers
                continue
            self.headers.append((name.strip(), value))

    # done
    def is_chunked(self):
        # Only use chunked responses when the client is speaking HTTP/1.1
        # or newer and there was no Content-Length header set.
        if self.response_length is not None:
            return False
        elif self.req.version <= (1, 0):
            return False
        elif self.req.method == 'HEAD':
            # Responses to a HEAD request MUST NOT contain a response body.
            return False
        elif self.status_code in (204, 304):
            # Do not use chunked responses when the response is guaranteed to
            # not have a response body.
            return False
        return True

    # done
    def default_headers(self):
        # set the connection header
        if self.upgrade:
            connection = "upgrade"
        elif self.should_close():
            connection = "close"
        else:
            connection = "keep-alive"

        headers = [
            "HTTP/%s.%s %s\r\n" % (self.req.version[0],
                self.req.version[1], self.status),
            "Server: %s\r\n" % self.version,
            "Date: %s\r\n" % util.http_date(),
            "Connection: %s\r\n" % connection
        ]
        if self.chunked:
            headers.append("Transfer-Encoding: chunked\r\n")
        return headers

    # done
    def send_headers(self):
        if self.headers_sent:
            return
        tosend = self.default_headers()
        tosend.extend(["%s: %s\r\n" % (k, v) for k, v in self.headers])

        header_str = "%s\r\n" % "".join(tosend)
        util.write(self.sock, util.to_bytestring(header_str, "latin-1"))
        self.headers_sent = True

    # done
    def write(self, arg):
        self.send_headers()
        if not isinstance(arg, bytes):
            raise TypeError('%r is not a byte' % arg)

        total = len(arg)
        remain = total
        if self.response_length is not None:
            if self.sent >= self.response_length:
                # Never write more than self.response_length bytes
                return

            remain = min(self.response_length - self.sent, remain)
            if remain < total:
                arg = arg[:remain]

        # Sending an empty chunk signals the end of the
        # response and prematurely closes the response
        if self.chunked and remain == 0:
            return

        self.sent += remain
        util.write(self.sock, arg, self.chunked)

    # done
    def can_sendfile(self):
        return self.cfg.sendfile is not False

    # sendfile() copies data between one file descriptor and another.
    # Because this copying is done within the kernel, sendfile() is more
    # efficient than the combination of read(2) and write(2), which would
    # require transferring data to and from user space.
    def sendfile(self, respiter):
        # 如果是https, 不能使用sendfile, 因为数据需要加密
        # https://stackoverflow.com/questions/50792406/issue-when-using-sendfile-with-ssl
        if self.cfg.is_ssl or not self.can_sendfile():
            return False

        if not util.has_fileno(respiter.filelike):
            return False

        fileno = respiter.filelike.fileno()
        try:
            offset = os.lseek(fileno, 0, os.SEEK_CUR)
            if self.response_length is None:
                filesize = os.fstat(fileno).st_size

                # The file may be special and sendfile will fail.
                # It may also be zero-length, but that is okay.
                if filesize == 0:
                    return False

                nbytes = filesize - offset
            else:
                nbytes = self.response_length
        except (OSError, io.UnsupportedOperation):
            return False

        self.send_headers()

        if self.is_chunked():
            chunk_size = "%X\r\n" % nbytes
            self.sock.sendall(chunk_size.encode('utf-8'))

        sockno = self.sock.fileno()
        sent = 0

        while sent != nbytes:
            count = min(nbytes - sent, BLKSIZE)
            sent += os.sendfile(sockno, fileno, offset + sent, count)

        if self.is_chunked():
            self.sock.sendall(b"\r\n")

        os.lseek(fileno, offset, os.SEEK_SET)

        return True

    # done
    def write_file(self, respiter):
        if not self.sendfile(respiter):
            for item in respiter:
                self.write(item)

    # done
    def close(self):
        if not self.headers_sent:
            self.send_headers()
        if self.chunked:
            # Sending an empty chunk signals the end of the
            # response and prematurely closes the response
            util.write_chunk(self.sock, b"")
