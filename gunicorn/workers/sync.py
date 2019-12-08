# -*- coding: utf-8 -
#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.
#

from datetime import datetime
import errno
import os
import select
import socket
import ssl
import sys

import gunicorn.http as http
import gunicorn.http.wsgi as wsgi
import gunicorn.util as util
import gunicorn.workers.base as base


class StopWaiting(Exception):
    """ exception raised to stop waiting for a connection """


class SyncWorker(base.Worker):

    # done
    def accept(self, listener):
        client, addr = listener.accept()    # 此处会阻塞

        client.setblocking(1)
        util.close_on_exec(client)
        self.handle(listener, client, addr)

    # done: wait以避免CPU空转
    def wait(self, timeout):
        try:
            self.notify()
            ret = select.select(self.wait_fds, [], [], timeout)
            if ret[0]:
                if self.PIPE[0] in ret[0]:
                    os.read(self.PIPE[0], 1)
                return ret[0]

        # 参见: http://man7.org/linux/man-pages/man2/select.2.html#ERRORS
        except select.error as e:
            if e.args[0] == errno.EINTR:    # 收到信号, 不一定是所有sock都有数据吧?
                return self.sockets
            if e.args[0] == errno.EBADF:    # wait_fds 有无效的描述符
                if self.nr < 0:
                    return self.sockets
                else:
                    raise StopWaiting
            raise

    # done
    def is_parent_alive(self):
        # If our parent changed then we shut down.
        if self.ppid != os.getppid():
            self.log.info("Parent changed, shutting down: %s", self)
            return False
        return True

    # done
    def run_for_one(self, timeout):
        listener = self.sockets[0]
        while self.alive:
            self.notify()
            # Accept a connection. If we get an error telling us
            # that no connection is waiting we fall down to the
            # select which is where we'll wait for a bit for new
            # workers to come give us some love.
            try:
                # If no pending connections are present on the queue, and the
                # socket is not marked as nonblocking, accept() blocks the
                # caller until a connection is present. If the socket is marked
                # nonblocking and no pending connections are present on the
                # queue, accept() fails with the error EAGAIN or EWOULDBLOCK.
                self.accept(listener)
                # Keep processing clients until no one is waiting. This
                # prevents the need to select() for every client that we
                # process.
                continue
            except EnvironmentError as e:
                # 如上所述, 对于nonblocking socket, 若一段时间(几秒)都没有请求,
                # accept 会抛出 EAGAIN 或 EWOULDBLOCK 错误
                if e.errno not in (errno.EAGAIN, errno.ECONNABORTED,
                                   errno.EWOULDBLOCK):
                    raise
            if not self.is_parent_alive():
                return
            try:
                self.wait(timeout)
            except StopWaiting:
                return

    # done
    def run_for_multiple(self, timeout):
        while self.alive:
            self.notify()

            try:
                ready = self.wait(timeout)
            except StopWaiting:
                return

            if ready is not None:
                for listener in ready:
                    if listener == self.PIPE[0]:
                        continue
                    try:
                        self.accept(listener)
                    except EnvironmentError as e:
                        if e.errno not in (errno.EAGAIN, errno.ECONNABORTED,
                                           errno.EWOULDBLOCK):
                            raise

            if not self.is_parent_alive():
                return

    # done
    def run(self):
        # if no timeout is given the worker will never wait and will
        # use the CPU for nothing. This minimal timeout prevent it.
        timeout = self.timeout or 0.5

        # self.socket appears to lose its blocking status after
        # we fork in the arbiter. Reset it here.
        for s in self.sockets:
            s.setblocking(0)

        # 下面会进入while循环, 在某些时刻(如: alive=False)时会退出
        if len(self.sockets) > 1:
            self.run_for_multiple(timeout)
        else:
            self.run_for_one(timeout)

    # done
    def handle(self, listener, client, addr):
        self._log('handle client=%s' % str(addr))
        req = None
        try:
            if self.cfg.is_ssl:
                client = ssl.wrap_socket(
                    client, server_side=True, **self.cfg.ssl_options)

            # 仅处理了一个请求, 没有考虑长连接中多个请求的情况?
            parser = http.RequestParser(self.cfg, client)
            # 解析HTTP报文得到一个请求对象, 会处理请求行, headers, 请求体
            req = next(parser)
            self.handle_request(listener, req, client, addr)

        except http.errors.NoMoreData as e:
            self._log("handle Ignored premature client disconnection. %s" % e)
        except StopIteration as e:
            self._log("handle Closing connection. %s" % e)
        except ssl.SSLError as e:
            self._log('handle SSLError %s' % e)
            if e.args[0] == ssl.SSL_ERROR_EOF:
                self.log.debug("ssl connection closed")
                client.close()
            else:
                self.log.debug("Error processing SSL request.")
                self.handle_error(req, client, addr, e)
        except EnvironmentError as e:
            self._log('handle EnvironmentError %s' % e)
            # EPIPE: Broken pipe
            # ECONNRESET: Connection reset by peer, 是 RST 么?
            if e.errno not in (errno.EPIPE, errno.ECONNRESET):
                self.log.exception("Socket error processing request.")
            else:
                if e.errno == errno.ECONNRESET:
                    self.log.debug("Ignoring connection reset")
                else:
                    self.log.debug("Ignoring EPIPE")
        except Exception as e:
            self.handle_error(req, client, addr, e)
        finally:
            util.close(client)

    # done
    def handle_request(self, listener, req, client, addr):
        environ = {}
        resp = None
        try:
            self.cfg.pre_request(self, req)
            request_start = datetime.now()
            resp, environ = wsgi.create(
                req=req, sock=client, client=addr,
                server=listener.getsockname(), cfg=self.cfg
            )
            # Force the connection closed until someone shows
            # a buffering proxy that supports Keep-Alive to
            # the backend. todo: 不懂
            resp.force_close()
            self.nr += 1
            if self.nr >= self.max_requests:
                self.log.info("Auto restarting worker after current request.")
                self.alive = False

            respiter = self.wsgi(environ, resp.start_response)
            try:
                if isinstance(respiter, environ['wsgi.file_wrapper']):
                    resp.write_file(respiter)
                else:
                    for item in respiter:
                        resp.write(item)
                resp.close()
                request_time = datetime.now() - request_start
                self.log.access(resp, req, environ, request_time)
            finally:
                if hasattr(respiter, "close"):
                    respiter.close()
        except EnvironmentError:
            # pass to next try-except level
            util.reraise(*sys.exc_info())
        except Exception:
            if resp and resp.headers_sent:
                # If the requests have already been sent, we should
                # close the connection to indicate the error.
                self.log.exception("Error handling request")
                try:
                    # Shut down one or both halves of the connection
                    # SHUT_RDWR: further sends and receives are disallowed
                    client.shutdown(socket.SHUT_RDWR)
                    client.close()
                except EnvironmentError:
                    pass
                raise StopIteration()
            raise
        finally:
            try:
                self.cfg.post_request(self, req, environ, resp)
            except Exception:
                self.log.exception("Exception in post_request hook")
