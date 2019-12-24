# -*- coding: utf-8 -
#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

# design:
# A threaded worker accepts connections in the main loop, accepted
# connections are added to the thread pool as a connection job.
# Keepalive connections are put back in the loop waiting for an event.
# If no event happen after the keep alive timeout, the connection is
# closed.
# pylint: disable=no-else-break

"""
简单测试了一下, 开4个worker, 每个worker中开5个线程, 然后压测 /user/num:
Requests per second:    421.29 [#/sec] (mean)
Time per request:       23.737 [ms] (mean)
"""

import concurrent.futures as futures
import errno
import os
import selectors
import socket
import ssl
import sys
import time
from collections import deque
from datetime import datetime
from functools import partial
from threading import RLock

from . import base
from .. import http
from .. import util
from ..http import wsgi


class TConn(object):

    def __init__(self, cfg, sock, client, server):
        self.cfg = cfg
        self.sock = sock        # client sock
        self.client = client    # client addr
        self.server = server    # server addr

        self.timeout = None
        self.parser = None      # http.RequestParser

        # set the socket to non blocking
        self.sock.setblocking(False)

    def init(self):
        self.sock.setblocking(True)
        if self.parser is None:
            # wrap the socket if needed
            if self.cfg.is_ssl:
                self.sock = ssl.wrap_socket(
                    self.sock, server_side=True, **self.cfg.ssl_options
                )
            # initialize the parser
            self.parser = http.RequestParser(self.cfg, self.sock)

    def set_timeout(self):
        # set the timeout, 默认超时时间 2s
        self.timeout = time.time() + self.cfg.keepalive

    def close(self):
        util.close(self.sock)


class ThreadWorker(base.Worker):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 最大并发连接数, default 1000
        self.worker_connections = self.cfg.worker_connections
        # todo: max_keepalived 为什么这么算?
        self.max_keepalived = self.cfg.worker_connections - self.cfg.threads
        # initialise the pool
        self.tpool = None
        self.poller = None
        self._lock = None
        self.futures = deque()
        self._keep = deque()        # keepalived connections
        self.nr_conns = 0

    @classmethod
    def check_config(cls, cfg, log):
        max_keepalived = cfg.worker_connections - cfg.threads

        if max_keepalived <= 0 and cfg.keepalive:
            log.warning("No keepalived connections can be handled. "
                        "Check the number of worker connections and threads.")

    # done: 线程池 poller
    def init_process(self):
        self.tpool = self.get_thread_pool()         # 默认 1 个线程
        self.poller = selectors.DefaultSelector()
        self._lock = RLock()
        super().init_process()

    # done
    def get_thread_pool(self):
        """Override this method to customize how the thread pool is created"""
        self._log('get_thread_pool size=%s' % self.cfg.threads)
        return futures.ThreadPoolExecutor(max_workers=self.cfg.threads)

    # done
    def handle_quit(self, sig, frame):
        self.alive = False
        # worker_int callback
        self.cfg.worker_int(self)
        self.tpool.shutdown(False)
        # self.poller 不 close?
        time.sleep(0.1)
        sys.exit(0)

    # done
    def _wrap_future(self, fs, conn):
        fs.conn = conn
        self.futures.append(fs)
        fs.add_done_callback(self.finish_request)

    # done
    def enqueue_req(self, conn):
        conn.init()
        # submit the connection to a worker
        fs = self.tpool.submit(self.handle, conn)
        self._wrap_future(fs, conn)

    # done 接受conn, 并放到tpool
    def accept(self, server, listener):
        try:
            sock, client = listener.accept()
            # initialize the connection object
            conn = TConn(self.cfg, sock, client, server)
            self.nr_conns += 1
            # enqueue the job
            self.enqueue_req(conn)
        except EnvironmentError as e:
            if e.errno not in (errno.EAGAIN,
                    errno.ECONNABORTED, errno.EWOULDBLOCK):
                raise

    # done: kill 超时的 sock
    def murder_keepalived(self):
        now = time.time()
        while True:
            with self._lock:
                try:
                    # remove the connection from the queue
                    conn = self._keep.popleft()
                except IndexError:
                    break

            delta = conn.timeout - now
            if delta > 0:
                # add the connection back to the queue
                with self._lock:
                    self._keep.appendleft(conn)
                break
            else:
                self.nr_conns -= 1
                # remove the socket from the poller
                with self._lock:
                    try:
                        self.poller.unregister(conn.sock)
                    except EnvironmentError as e:
                        if e.errno != errno.EBADF:
                            raise
                    except KeyError:
                        # already removed by the system, continue
                        pass

                # close the socket
                conn.close()

    # done
    def is_parent_alive(self):
        # If our parent changed then we shut down.
        if self.ppid != os.getppid():
            self.log.info("Parent changed, shutting down: %s", self)
            return False
        return True

    def run(self):
        # init listeners, add them to the event loop
        for sock in self.sockets:
            sock.setblocking(False)
            # a race condition during graceful shutdown may make the listener
            # name unavailable in the request handler so capture it once here
            server = sock.getsockname()  # e.g. ('127.0.0.1', 1234)
            acceptor = partial(self.accept, server)
            self.poller.register(sock, selectors.EVENT_READ, acceptor)

        while self.alive:
            # notify the arbiter we are alive
            self.notify()

            # can we accept more connections?
            if self.nr_conns < self.worker_connections:
                # wait for an event
                events = self.poller.select(1.0)
                for key, _ in events:
                    # e.g. 对于服务器sock来说, key.data -> acceptor
                    # key.fileobj -> listener fd描述符
                    # 调用的时候就是调用self.accept接受请求
                    callback = key.data
                    callback(key.fileobj)

                # check (but do not wait) for finished requests
                result = futures.wait(self.futures, timeout=0,
                        return_when=futures.FIRST_COMPLETED)
            else:
                # wait for a request to finish
                result = futures.wait(self.futures, timeout=1.0,
                        return_when=futures.FIRST_COMPLETED)

            # clean up finished requests
            # self._wrap_future会将处理conn时产生的 future
            # 对象放入self.futures; 当请求处理完毕后, remove
            for fut in result.done:
                self.futures.remove(fut)

            if not self.is_parent_alive():
                break

            # handle keepalive timeouts
            self.murder_keepalived()

        self.tpool.shutdown(False)
        self.poller.close()

        for s in self.sockets:
            s.close()

        futures.wait(self.futures, timeout=self.cfg.graceful_timeout)

    # done: 对于 keepalived 的连接, reuse_connection
    def finish_request(self, fs):
        # fs: future 对象
        if fs.cancelled():
            self.nr_conns -= 1
            fs.conn.close()
            return

        try:
            # fs.result() 是 self.handle 的返回值
            (keepalive, conn) = fs.result()
            # if the connection should be kept alived add it
            # to the eventloop and record it
            if keepalive:
                # flag the socket as non blocked
                conn.sock.setblocking(False)
                # register the connection
                conn.set_timeout()
                with self._lock:
                    # 将keepalived的连接放入self._keep, 然后注册回调事件
                    self._keep.append(conn)
                    # add the socket to the event loop
                    self.poller.register(conn.sock, selectors.EVENT_READ,
                            partial(self.reuse_connection, conn))
            else:
                self.nr_conns -= 1
                conn.close()
        except:
            # todo: 不应该记录日志么
            # make sure to close the socket
            self.nr_conns -= 1
            fs.conn.close()

    # done: keepalived 连接不会关闭, 可以发送多个请求, 故称为 reuse_connection
    def reuse_connection(self, conn, client):
        with self._lock:
            # finish_request 中会将 client sock 注册到 poller
            # unregister the client from the poller
            self.poller.unregister(client)
            # remove the connection from keepalive
            try:
                self._keep.remove(conn)
            except ValueError:
                # race condition
                return

        # submit the connection to a worker
        self.enqueue_req(conn)

    # done
    def handle(self, conn):
        req = None
        try:
            req = next(conn.parser)
            if not req:
                return False, conn

            # handle the request
            keepalive = self.handle_request(req, conn)
            if keepalive:
                return keepalive, conn

        # todo: 下面的逻辑和sync worker是一样的...
        except http.errors.NoMoreData as e:
            self.log.debug("Ignored premature client disconnection. %s", e)
        except StopIteration as e:
            #  finish_request 中会关闭连接
            self.log.debug("Closing connection. %s", e)
        except ssl.SSLError as e:
            if e.args[0] == ssl.SSL_ERROR_EOF:
                self.log.debug("ssl connection closed")
                conn.sock.close()
            else:
                self.log.debug("Error processing SSL request.")
                self.handle_error(req, conn.sock, conn.client, e)
        except EnvironmentError as e:
            if e.errno not in (errno.EPIPE, errno.ECONNRESET):
                self.log.exception("Socket error processing request.")
            else:
                if e.errno == errno.ECONNRESET:
                    self.log.debug("Ignoring connection reset")
                else:
                    self.log.debug("Ignoring connection epipe")
        except Exception as e:
            self.handle_error(req, conn.sock, conn.client, e)

        return False, conn

    # done
    def handle_request(self, req, conn):
        environ = {}
        resp = None
        try:
            self.cfg.pre_request(self, req)
            request_start = datetime.now()
            resp, environ = wsgi.create(
                req, conn.sock, conn.client, conn.server, self.cfg)
            environ["wsgi.multithread"] = True
            self.nr += 1
            if self.alive and self.nr >= self.max_requests:
                self.log.info("Auto restarting worker after current request.")
                resp.force_close()
                self.alive = False

            if not self.cfg.keepalive:
                resp.force_close()
            elif len(self._keep) >= self.max_keepalived:
                resp.force_close()

            # todo: 下面的逻辑和sync worker是一样的...
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

            if resp.should_close():
                self.log.debug("Closing connection.")
                return False
        except EnvironmentError:
            # pass to next try-except level
            util.reraise(*sys.exc_info())
        except Exception:
            if resp and resp.headers_sent:
                # If the requests have already been sent, we should close the
                # connection to indicate the error.
                self.log.exception("Error handling request")
                try:
                    conn.sock.shutdown(socket.SHUT_RDWR)
                    conn.sock.close()
                except EnvironmentError:
                    pass
                raise StopIteration()
            raise
        finally:
            try:
                self.cfg.post_request(self, req, environ, resp)
            except Exception:
                self.log.exception("Exception in post_request hook")

        return True
