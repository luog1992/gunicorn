# -*- coding: utf-8 -
#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

import errno
import os
import socket
import stat
import sys
import time

from gunicorn import util
from gunicorn.socketfromfd import fromfd


# done
class BaseSocket(object):

    def __init__(self, address, conf, log, fd=None):
        """
        :param tuple|str|bytes address: socket地址
        :param Config conf: 配置
        :param Logger log: 日志
        :param int fd: fd
        """
        self.log = log
        self.conf = conf

        self.cfg_addr = address
        if fd is None:
            sock = socket.socket(self.FAMILY, socket.SOCK_STREAM)
            bound = False
        else:
            sock = socket.fromfd(fd, self.FAMILY, socket.SOCK_STREAM)
            os.close(fd)
            bound = True    # fd存在, 说明sock已经绑定了

        self.sock = self.set_options(sock, bound=bound)

    def __str__(self):
        return "<socket %d>" % self.sock.fileno()

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def set_options(self, sock, bound=False):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if (self.conf.reuse_port
            and hasattr(socket, 'SO_REUSEPORT')):  # pragma: no cover
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except socket.error as err:
                # ENOPROTOOPT: protocol not available
                # EINVAL: invalid argument
                if err.errno not in (errno.ENOPROTOOPT, errno.EINVAL):
                    raise
        if not bound:
            self.bind(sock)
        sock.setblocking(0)     # set to non-blocking

        # todo: 确定 inheritable (对worker)意味着啥? worker也可以监听相同的sock?
        # make sure that the socket can be inherited
        if hasattr(sock, "set_inheritable"):
            sock.set_inheritable(True)

        sock.listen(self.conf.backlog)  # default is 2048
        return sock

    def bind(self, sock):
        sock.bind(self.cfg_addr)

    def close(self):
        if self.sock is None:
            return

        try:
            self.sock.close()
        except socket.error as e:
            self.log.info("Error while closing socket %s", str(e))

        self.sock = None


# done
class TCPSocket(BaseSocket):

    FAMILY = socket.AF_INET

    def __str__(self):
        if self.conf.is_ssl:
            scheme = "https"
        else:
            scheme = "http"

        addr = self.sock.getsockname()
        return "%s://%s:%d" % (scheme, addr[0], addr[1])

    def set_options(self, sock, bound=False):
        # 关闭Nagle算法
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return super().set_options(sock, bound=bound)


# done
class TCP6Socket(TCPSocket):

    FAMILY = socket.AF_INET6

    def __str__(self):
        (host, port, _, _) = self.sock.getsockname()
        return "http://[%s]:%d" % (host, port)


# done
class UnixSocket(BaseSocket):

    FAMILY = socket.AF_UNIX

    def __init__(self, addr, conf, log, fd=None):
        if fd is None:
            try:
                st = os.stat(addr)
            except OSError as e:
                if e.args[0] != errno.ENOENT:   # no such file or directory
                    raise
            else:
                if stat.S_ISSOCK(st.st_mode):
                    os.remove(addr)
                else:
                    raise ValueError("%r is not a socket" % addr)
        # todo: 为啥上面要删掉(?) addr?
        super().__init__(addr, conf, log, fd=fd)

    def __str__(self):
        return "unix:%s" % self.cfg_addr

    def bind(self, sock):
        old_umask = os.umask(self.conf.umask)
        sock.bind(self.cfg_addr)
        util.chown(self.cfg_addr, self.conf.uid, self.conf.gid)
        os.umask(old_umask)


# done
def _sock_type(addr):
    if isinstance(addr, tuple):
        if util.is_ipv6(addr[0]):
            sock_type = TCP6Socket
        else:
            sock_type = TCPSocket
    elif isinstance(addr, (str, bytes)):
        sock_type = UnixSocket
    else:
        raise TypeError("Unable to create socket from: %r" % addr)
    return sock_type


# done
def create_sockets(conf, log, fds=None):
    """
    Create a new socket for the configured addresses or file descriptors.

    If a configured address is a tuple then a TCP socket is created.
    If it is a string, a Unix socket is created. Otherwise, a TypeError is
    raised.
    """
    listeners = []

    # get it only once
    addr = conf.address

    # fd addr: [3, 4, 5,...]
    fdaddr = [bind for bind in addr if isinstance(bind, int)]
    if fds:
        fdaddr += list(fds)

    # bind addr: [('127.0.0.1', 8000)]
    laddr = [bind for bind in addr if not isinstance(bind, int)]

    # check ssl config early to raise the error on startup
    # only the certfile is needed since it can contains the keyfile
    if conf.certfile and not os.path.exists(conf.certfile):
        raise ValueError('certfile "%s" does not exist' % conf.certfile)

    if conf.keyfile and not os.path.exists(conf.keyfile):
        raise ValueError('keyfile "%s" does not exist' % conf.keyfile)

    # sockets are already bound
    # 从文件描述符(int)获得socket
    if fdaddr:
        for fd in fdaddr:
            #            fd --------> file
            #             ↓            ↑
            #  sock --> new_fd ---------

            # fromfd 函数中无论py2/py3都会复制fd
            # 所以! sock 对应的 new_fd 什么时候关闭? sock被回收后?
            sock = fromfd(fd)
            sock_name = sock.getsockname()
            sock_type = _sock_type(sock_name)
            # sock_type 中会复制fd, 并关闭(旧)fd
            listener = sock_type(sock_name, conf, log, fd=fd)
            listeners.append(listener)

        return listeners

    # no sockets is bound, first initialization of gunicorn in this env.
    # 从bind((127.0.0.1, 8000))创建socket
    for addr in laddr:
        sock_type = _sock_type(addr)
        sock = None
        for i in range(5):
            try:
                sock = sock_type(addr, conf, log)
            except socket.error as e:
                if e.args[0] == errno.EADDRINUSE:
                    log.error("Connection in use: %s", str(addr))
                if e.args[0] == errno.EADDRNOTAVAIL:
                    log.error("Invalid address: %s", str(addr))
                if i < 5:
                    msg = "connection to {addr} failed: {error}"
                    log.debug(msg.format(addr=str(addr), error=str(e)))
                    log.error("Retrying in 1 second.")
                    time.sleep(1)
            else:
                break

        if sock is None:
            log.error("Can't connect to %s", str(addr))
            sys.exit(1)

        listeners.append(sock)

    return listeners


def close_sockets(listeners, unlink=True):
    for sock in listeners:
        sock_name = sock.getsockname()
        sock.close()
        if unlink and _sock_type(sock_name) is UnixSocket:
            os.unlink(sock_name)
