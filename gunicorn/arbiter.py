# -*- coding: utf-8 -
#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

"""
TODO:

1. 如何简单明了通俗的理解 sleep 和 wakeup 的作用
"""

import errno
import os
import random
import select
import signal
import sys
import time
import threading
import traceback

from gunicorn.errors import HaltServer, AppImportError
from gunicorn.pidfile import Pidfile
from gunicorn import sock, systemd, util

from gunicorn import __version__, SERVER_SOFTWARE

from colorama import Fore


class Arbiter(object):
    """
    Arbiter maintain the workers processes alive. It launches or
    kills them if needed. It also manages application reloading
    via SIGHUP/USR2.
    """

    # A flag indicating if a worker failed to
    # to boot. If a worker process exist with
    # this error code, the arbiter will terminate.
    WORKER_BOOT_ERROR = 3

    # A flag indicating if an application failed to be loaded
    APP_LOAD_ERROR = 4

    START_CTX = {}

    LISTENERS = []
    WORKERS = {}
    PIPE = []

    # I love dynamic languages
    SIG_QUEUE = []
    SIGNALS = [getattr(signal, "SIG%s" % x)
               for x in "HUP QUIT INT TERM TTIN TTOU USR1 USR2 WINCH".split()]
    SIG_NAMES = dict(
        (getattr(signal, name), name[3:].lower()) for name in dir(signal)
        if name[:3] == "SIG" and name[3] != "_"
    )

    def __init__(self, app):
        os.environ["SERVER_SOFTWARE"] = SERVER_SOFTWARE

        self._num_workers = None
        self._last_logged_active_worker_count = None
        self.log = None

        # 一下属性会在 setup 中赋值
        self.app = None
        self.cfg = None
        self.worker_class = None
        self.address = None
        self.timeout = None
        self.proc_name = None
        self.setup(app)

        self.pid = None         # 在 start 中赋值
        self.pidfile = None
        self.systemd = False
        self.worker_age = 0
        self.reexec_pid = 0
        self.master_pid = 0
        self.master_name = "Master"

        args = sys.argv[:]
        args.insert(0, sys.executable)

        # init start context
        self.START_CTX = {
            # e.g. ['python3.7', 'gunicorn', '-w', '1', 'app:app']
            "args": args,
            "cwd": util.getcwd(),
            0: sys.executable       # e.g. python3.7
        }
        self._log('START_CTX: %s' % self.START_CTX)

    # done
    def _get_num_workers(self):
        return self._num_workers

    def _set_num_workers(self, value):
        old_value = self._num_workers
        self._num_workers = value
        self.cfg.nworkers_changed(self, value, old_value)
    num_workers = property(_get_num_workers, _set_num_workers)

    # done
    def setup(self, app):
        self.app = app
        self.cfg = app.cfg

        if self.log is None:
            self.log = self.cfg.logger_class(app.cfg)
        self._log('setup')

        # todo: reexec后, reopen_files, 是为了?
        if 'GUNICORN_FD' in os.environ:
            self.log.reopen_files()

        self.worker_class = self.cfg.worker_class
        self.address = self.cfg.address
        self.num_workers = self.cfg.workers
        self.timeout = self.cfg.timeout
        self.proc_name = self.cfg.proc_name
        self._log('setup proc_name: %s' % self.proc_name)  # e.g.: app:app

        self.log.debug('Current configuration:\n{0}'.format(
            '\n'.join(
                '  {0}: {1}'.format(config, value.value)
                for config, value in
                sorted(self.cfg.settings.items(), key=lambda s: s[1])
            )
        ))

        # set environment variables
        if self.cfg.env:
            for k, v in self.cfg.env.items():
                os.environ[k] = v

        # 如果preload, 则所有worker会共享同一个app对象
        if self.cfg.preload_app:
            self.app.wsgi()

    # done & todo: 还需仔细研究
    def start(self):
        """Initialize the arbiter. Start listening and set pidfile if needed.
        """
        self._log("start Starting gunicorn %s" % __version__)

        # reexec之后, promote master之前, master_pid != 0
        if 'GUNICORN_PID' in os.environ:
            self.master_pid = int(os.environ.get('GUNICORN_PID'))
            self.proc_name = self.proc_name + ".2"
            self.master_name = "Master.2"
        self._log('start master %s master_pid=%s reexec_pid=%s' % (
            self.master_name, self.master_pid, self.reexec_pid))

        self.pid = os.getpid()
        if self.cfg.pidfile is not None:
            pidname = self.cfg.pidfile
            if self.master_pid != 0:
                pidname += ".2"
            self.pidfile = Pidfile(pidname)
            self.pidfile.create(self.pid)

        self.cfg.on_starting(self)
        self.init_signals()

        self._create_listeners()

        if hasattr(self.worker_class, "check_config"):
            self.worker_class.check_config(self.cfg, self.log)

        self.cfg.when_ready(self)

    # done
    def _create_listeners(self):
        if not self.LISTENERS:
            fds = None
            # systemd socket activation方式
            listen_fds = systemd.listen_fds()
            if listen_fds:
                self.systemd = True
                fds = range(systemd.SD_LISTEN_FDS_START,
                            systemd.SD_LISTEN_FDS_START + listen_fds)

            # reexec之后, promote master之前, master_id != 0
            elif self.master_pid:
                fds = []
                for fd in os.environ.pop('GUNICORN_FD').split(','):
                    fds.append(int(fd))

            # create_sockets中会关闭(旧)fds
            # 若 fds=None, 则从cfg中设置的bind创建sock
            self._log('create listeners fds=%s' % fds)
            self.LISTENERS = sock.create_sockets(self.cfg, self.log, fds)

        listeners_str = ",".join([str(l) for l in self.LISTENERS])
        self.log.debug("Arbiter booted")
        self._log("Listening at: %s (%s)" % (listeners_str, self.pid))
        self._log("Using worker: %s" % self.cfg.worker_class_str)
        systemd.sd_notify("READY=1\nSTATUS=Gunicorn arbiter booted", self.log)

    # done
    def init_signals(self):
        """Initialize master signal handling. Most of the signals
        are queued. Child signals only wake up the master.
        """
        self._log('init_signals')
        # close old PIPE
        for p in self.PIPE:
            os.close(p)

        # initialize the pipe
        # 当收到信号后, 会在 wakeup 中向 PIPE 中写入数据
        # 在 sleep 中会通过 select 监听 PIPE 中是否有数据
        # todo: 那么问题是? 为啥要搞个PIPE?
        self.PIPE = pair = os.pipe()
        for p in pair:
            util.set_non_blocking(p)
            util.close_on_exec(p)

        self.log.close_on_exec()

        # initialize all signals
        for s in self.SIGNALS:
            signal.signal(s, self.signal)

        # 子进程终止时发送给父进程的信号
        signal.signal(signal.SIGCHLD, self.handle_chld)

    # done: 若进程收到信号, 此方法会首先被调用. 如Ctrl-C时候, 会传入sig=2(SIGINT)
    def signal(self, sig, frame):
        self._log('signal sig=%s & wakeup' % sig)
        if len(self.SIG_QUEUE) < 5:
            # todo: 为什么是5? 更多的信号忽略? 什么时候会产生很多的信号?
            self.SIG_QUEUE.append(sig)
            self.wakeup(abc='SIG%s>' % sig)

    # done
    def run(self):
        """Main master loop.
        """
        self._log('run')
        self.start()
        util._setproctitle("master [%s]" % self.proc_name)

        abc = 'RUN>'
        try:
            # 看是否需要 增/减 worker
            self.manage_workers()

            while True:
                self.maybe_promote_master()

                # 周期性检测信号
                sig = self.SIG_QUEUE.pop(0) if self.SIG_QUEUE else None
                if sig is None:
                    # 处理 PIPE 中的消息(数据)
                    self.sleep()
                    # 终止unused/idle worker
                    self.murder_workers()
                    # 看是否需要 增/减 worker
                    self.manage_workers()
                    continue

                self._log('run got sig=%s' % sig)
                if sig not in self.SIG_NAMES:
                    self.log.info("Ignoring unknown signal: %s", sig)
                    continue

                signame = self.SIG_NAMES.get(sig)
                handler = getattr(self, "handle_%s" % signame, None)
                if not handler:
                    self.log.error("Unhandled signal: %s", signame)
                    continue
                self._log("Handling signal: %s" % signame)
                handler()

                # todo: 为啥此处要 wakeup?
                self._log('run will wakeup')
                self.wakeup(abc='RUN>')

        except StopIteration:
            self._log('run except1: StopIteration')
            self.halt(abc=abc)
        except KeyboardInterrupt:
            self._log('run except1: KeyboardInterrupt')
            self.halt(abc=abc)
        except HaltServer as e:
            self._log('run except2: %s' % e)
            self.halt(reason=e.reason, exit_status=e.exit_status, abc=abc)
        except SystemExit:
            self._log('run except3: SystemExit')
            raise
        except Exception as e:
            self._log('run except4: %s' % e)
            self.log.info("Unhandled exception in main loop", exc_info=True)
            # 此处直接可以调用 halt 方法的吧?
            self.stop(False, abc=abc)
            if self.pidfile is not None:
                self.pidfile.unlink()
            sys.exit(-1)

    # reap_workers & wakeup
    def handle_chld(self, sig, frame):
        """SIGCHLD handling

        任何一个子进程(init除外)在exit后并非马上就消失，而是留下一个称外僵尸进程的
        数据结构, 等待父进程处理。另外子进程退出的时候会向其父进程发送一个SIGCHLD信号
        """
        self._log('handle_chld & reap_workers & wakeup')
        self.reap_workers(abc='H_CHLD>')
        self.wakeup(abc='H_CHLD>')

    # reload
    def handle_hup(self):
        """HUP handling.

        SIGHUP会在以下3种情况下被发送给相应的进程：

        1. 终端关闭时，该信号被发送到session首进程以及作为job提交的进程（即用 & 符号提交的进程）
        2. session首进程退出时，该信号被发送到该session中的前台进程组中的每一个进程
        3. 若父进程退出导致进程组成为孤儿进程组，且该进程组中有进程处于停止状态（收到SIGSTOP或SIGTSTP信号），
           该信号会被发送到该进程组中的每一个进程。

        系统对SIGHUP信号的默认处理是终止收到该信号的进程。所以若程序中没有捕捉该信号，当收到该信号时，进程就会退出。

        - Reload configuration
        - Start the new worker processes with a new configuration
        - Gracefully shutdown the old worker processes
        """
        self._log('handle_hup & reload')
        self.log.info("Hang up: %s", self.master_name)
        self.reload()

    # raise StopIteration
    def handle_term(self):
        """SIGTERM handling

        此方法中没有直接 stop, 而是 StopIteration, 然后再 run 中
        捕获到 StopIteration 后会 halt, 故是 优雅地 终止. 相比之下,
        handle_int handle_quit 会先 stop, 后 StopIteration.
        """
        self._log('handle_term & StopIteration')
        raise StopIteration

    # stop & raise StopIteration
    def handle_int(self):
        """SIGINT handling
        """
        self._log('handle_int & stop & StopIteration')
        self.stop(False, abc='H_INT>')
        raise StopIteration

    # stop & raise StopIteration
    def handle_quit(self):
        """SIGQUIT handling
        """
        self._log('handle_quit & stop & StopIteration')
        self.stop(False, abc='H_QUIT>')
        raise StopIteration

    # todo SIGTTIN SIGTTOU 是啥?
    # increase one worker
    def handle_ttin(self):
        """SIGTTIN handling. Increases the number of workers by one.
        """
        self._log('handle_ttin & workers+=1')
        self.num_workers += 1
        self.manage_workers()

    # decrease one worker
    def handle_ttou(self):
        """SIGTTOU handling. Decreases the number of workers by one.
        """
        self._log('handle_ttou & workers-=1')
        if self.num_workers <= 1:
            return
        self.num_workers -= 1
        self.manage_workers()

    # kill all workers
    def handle_usr1(self):
        """SIGUSR1 handling.

        Kill all workers by sending them a SIGUSR1
        """
        self._log('handle_usr1 & kill_workers')
        self.log.reopen_files()
        self.kill_workers(signal.SIGUSR1, abc='H_USR1>')

    # reexec
    def handle_usr2(self):
        """SIGUSR2 handling.

        Creates a new master/worker set as a slave of the current
        master without affecting old workers. Use this to do live
        deployment with the ability to backout a change.
        """
        self._log('handle_usr2 & reexec')
        self.reexec()

    def handle_winch(self):
        """SIGWINCH handling"""
        self._log('handle_winch')
        if self.cfg.daemon:
            self.log.info("graceful stop of workers")
            self.num_workers = 0
            self.kill_workers(signal.SIGTERM, abc='H_WINCH>')
        else:
            self.log.debug("SIGWINCH ignored. Not daemonized")

    # done: 向 PIPE 中写入数据
    def wakeup(self, abc=''):
        """\
        Wake up the arbiter by writing to the PIPE
        """
        self._log('%s wakeup' % abc)
        try:
            os.write(self.PIPE[1], b'.')
        except IOError as e:
            # EAGAIN: try again
            # EINTR: interrupted system call
            if e.errno not in [errno.EAGAIN, errno.EINTR]:
                raise

    # done: Sleep until PIPE is readable or we timeout.
    def sleep(self):
        """\
        Sleep until PIPE is readable or we timeout.
        A readable PIPE means a signal occurred.
        """
        # self._log('sleep')
        try:
            ready = select.select([self.PIPE[0]], [], [], 1.0)
            # 如果ready, ready[0] == [self.PIPE[0]], 否则 == []
            if not ready[0]:
                return
            while os.read(self.PIPE[0], 1):
                pass
        except (select.error, OSError) as e:
            # TODO: select.error is a subclass of OSError since Python 3.3.
            self._log('sleep except %s' % e)

            # 当Ctrl-C时候, errno_number = errno.EWOULDBLOCK
            # 而 errno.EWOULDBLOCK == errno.EAGAIN == 35
            # https://stackoverflow.com/questions/49049430/
            # difference-between-eagain-or-ewouldblock
            error_number = getattr(e, 'errno', e.args[0])
            if error_number not in [errno.EAGAIN, errno.EINTR]:
                raise
        except KeyboardInterrupt:
            self._log('sleep except KeyboardInterrupt')
            sys.exit()

    # halt 不仅stop, 而且做了stop后的清理工作
    # done: stop & unlink pidfile & exit
    def halt(self, reason=None, exit_status=0, abc=''):
        """halt arbiter
        """
        self._log('%s halt reason=%s exit_status=%s' %
                  (abc, reason, exit_status))
        abc += 'HALT>'
        self.stop(abc=abc)
        self._log("Shutting down: %s" % self.master_name)
        if reason is not None:
            self.log.info("Reason: %s", reason)
        if self.pidfile is not None:
            self.pidfile.unlink()
        self.cfg.on_exit(self)
        self._log('Shutting down %s' % self.master_name)
        sys.exit(exit_status)

    # stop 是关闭socket & 停止workers
    # done close_sockets & kill_workers
    def stop(self, graceful=True, abc=''):
        """Stop workers

        :attr graceful: boolean, If True (the default) workers will be
        killed gracefully  (ie. trying to wait for the current connection)
        """
        self._log('%s stop graceful=%s' % (abc, graceful))
        unlink = (
            self.reexec_pid == self.master_pid == 0
            and not self.systemd
            and not self.cfg.reuse_port
        )
        sock.close_sockets(self.LISTENERS, unlink)

        self.LISTENERS = []
        sig = signal.SIGTERM
        if not graceful:
            sig = signal.SIGQUIT

        abc += 'STOP>'
        limit = time.time() + self.cfg.graceful_timeout

        # instruct the workers to exit
        self._log('stop will %s workers' % sig.name)
        self.kill_workers(sig, abc=abc)
        # wait until the graceful timeout
        while self.WORKERS and time.time() < limit:
            time.sleep(0.1)

        self._log('stop will SIGKILL workers')
        self.kill_workers(signal.SIGKILL, abc=abc)

    def maybe_promote_master(self):
        # self._log('promote master master_pid=%s' % self.master_pid)
        if self.master_pid == 0:
            return
        # os.getppid: Return the parent’s process id. On Unix
        # the id returned is the one of the init process (1)
        # todo: 不太理解
        self._log('promote master mid=%s ppid=%s' %
                  (self.master_pid, os.getppid()))
        if self.master_pid == os.getppid():
            return

        self._log("Master has been promoted.")
        # reset master infos
        self.master_name = "Master"
        self.master_pid = 0
        self.proc_name = self.cfg.proc_name
        del os.environ['GUNICORN_PID']
        # rename the pidfile
        if self.pidfile is not None:
            self.pidfile.rename(self.cfg.pidfile)
        # reset proc title
        util._setproctitle("master [%s]" % self.proc_name)

    # done 只会在 handle_usr2 中调用
    def reexec(self):
        """Relaunch the master and workers.
        """
        self._log('reexec')
        # reexec之后, reap_works之后 , reexec_pid != 0
        if self.reexec_pid != 0:
            self.log.warning("USR2 signal ignored. Child exists.")
            return
        # reexec之后, promote_master之前, master_pid != 0
        if self.master_pid != 0:
            self.log.warning("USR2 signal ignored. Parent exists.")
            return

        master_pid = os.getpid()
        self.reexec_pid = os.fork()
        if self.reexec_pid != 0:    # 父进程中
            return

        self._log('reexec master_pid=%s' % master_pid)
        self._log('reexec reexec_pid=%s' % self.reexec_pid)

        self.cfg.pre_exec(self)

        environ = self.cfg.env_orig.copy()
        # start 方法中会用到 GUNICORN_PID
        environ['GUNICORN_PID'] = str(master_pid)

        if self.systemd:
            # 下面两个环境变量在 gunicorn.systemd.listen_fds 会用到
            environ['LISTEN_PID'] = str(os.getpid())
            environ['LISTEN_FDS'] = str(len(self.LISTENERS))
        else:
            environ['GUNICORN_FD'] = ','.join(
                str(l.fileno()) for l in self.LISTENERS)

        os.chdir(self.START_CTX['cwd'])

        # exec the process using the original environment
        # These functions all execute a new program, replacing
        # the current process; they do not return. On Unix, the
        # new executable is loaded into the current process, and
        # **will have the same process id as the caller.**
        self._log('reexec will execvpe')
        os.execvpe(self.START_CTX[0], self.START_CTX['args'], environ)

    # done reload 目前只会在 handle_hup 中被调用
    def reload(self):
        abc = 'RELOAD>'
        self._log('reload')
        old_address = self.cfg.address

        # reset old environment
        for k in self.cfg.env:
            if k in self.cfg.env_orig:
                # reset the key to the value it had before
                # we launched gunicorn
                os.environ[k] = self.cfg.env_orig[k]
            else:
                # delete the value set by gunicorn
                try:
                    del os.environ[k]
                except KeyError:
                    pass

        # reload conf
        self.app.reload()
        self.setup(self.app)

        # reopen log files
        self.log.reopen_files()

        # do we need to change listener ?
        if old_address != self.cfg.address:
            # close all listeners
            for l in self.LISTENERS:
                l.close()
            # todo: 此时为什么不想 _create_listeners 中那样考虑 fds?
            # init new listeners
            self.LISTENERS = sock.create_sockets(self.cfg, self.log)
            listeners_str = ",".join([str(l) for l in self.LISTENERS])
            self.log.info("Listening at: %s", listeners_str)

        # do some actions on reload
        self.cfg.on_reload(self)

        # unlink pidfile and create new
        if self.pidfile is not None:
            self.pidfile.unlink()
        if self.cfg.pidfile is not None:
            self.pidfile = Pidfile(self.cfg.pidfile)
            self.pidfile.create(self.pid)

        util._setproctitle("master [%s]" % self.proc_name)
        for _ in range(self.cfg.workers):
            self.spawn_worker(abc=abc)
        self.manage_workers()

    # done: Kill unused/idle workers
    def murder_workers(self):
        """\
        Kill unused/idle workers
        """
        if not self.timeout:
            return

        workers = list(self.WORKERS.items())
        for (pid, worker) in workers:
            try:
                if time.time() - worker.tmp.last_update() <= self.timeout:
                    continue
            except (OSError, ValueError):
                continue

            if not worker.aborted:
                self.log.critical("WORKER TIMEOUT (pid:%s)", pid)
                worker.aborted = True
                self.kill_worker(pid, signal.SIGABRT, abc='MD_WKS>')
            else:
                self.kill_worker(pid, signal.SIGKILL, abc='MD_WKS>')

    # Reap workers to avoid zombie processes
    def reap_workers(self, abc=''):
        """Reap workers to avoid zombie processes
        """
        self._log('%s reap_workers' % abc)
        try:
            while True:
                # -1: meaning wait for any child process, 不一定都是
                # workers, 也可能是 reexec 中 fork 得到的子进程
                wpid, status = os.waitpid(-1, os.WNOHANG)
                if not wpid:
                    break

                self._log('%s reap_workers wpid=%s reexec_pid=%s master_pid=%s'
                          % (abc, wpid, self.reexec_pid, self.master_pid))
                # reexec 中 fork 得到的 子进程
                if self.reexec_pid == wpid:
                    self.reexec_pid = 0
                else:
                    # A worker was terminated. If the termination reason was
                    # that it could not boot, we'll shut it down to avoid
                    # infinite start/stop cycles.
                    exitcode = status >> 8
                    if exitcode == self.WORKER_BOOT_ERROR:
                        reason = "Worker failed to boot."
                        raise HaltServer(reason, self.WORKER_BOOT_ERROR)
                    if exitcode == self.APP_LOAD_ERROR:
                        reason = "App failed to load."
                        raise HaltServer(reason, self.APP_LOAD_ERROR)

                    worker = self.WORKERS.pop(wpid, None)
                    if not worker:
                        continue
                    worker.tmp.close()
                    self.cfg.child_exit(self, worker)
        except OSError as e:
            if e.errno != errno.ECHILD:
                raise

    # done: spawn/kill workers
    def manage_workers(self):
        """\
        Maintain the number of workers by spawning or killing
        as required.
        """
        # self._log('manage_workers')
        if len(self.WORKERS) < self.num_workers:
            self.spawn_workers()

        workers = self.WORKERS.items()
        workers = sorted(workers, key=lambda w: w[1].age)
        while len(workers) > self.num_workers:
            (pid, _) = workers.pop(0)
            self.kill_worker(pid, signal.SIGTERM, abc='MN_WKS>')

        active_worker_count = len(workers)
        if self._last_logged_active_worker_count != active_worker_count:
            self._last_logged_active_worker_count = active_worker_count
            self.log.debug(
                "{0} workers".format(active_worker_count),
                extra={
                    "metric": "gunicorn.workers",
                    "value": active_worker_count,
                    "mtype": "gauge"
                }
            )

    # done
    def spawn_worker(self, abc=''):
        self._log('%s spawn_worker' % abc)
        self.worker_age += 1
        worker = self.worker_class(self.worker_age, self.pid, self.LISTENERS,
                                   self.app, self.timeout / 2.0,
                                   self.cfg, self.log)

        self.cfg.pre_fork(self, worker)  # hook before fork

        self._log('spawn_worker before fork')
        pid = os.fork()
        self._log('spawn_worker after fork')

        if pid != 0:    # 父进程中
            worker.pid = pid
            self.WORKERS[pid] = worker
            return pid

        # 在子进程中
        # Do not inherit the temporary files of other workers
        for sibling in self.WORKERS.values():
            sibling.tmp.close()

        # Process Child
        worker.pid = os.getpid()
        try:
            util._setproctitle("worker [%s]" % self.proc_name)
            self._log("Booting worker with pid: %s" % worker.pid)
            self.cfg.post_fork(self, worker)

            worker._log('spawn_worker before worker init_process')
            # 现在是在子进程中, 下面会hang住
            worker.init_process()
            # 如: 直到Ctrl-C worker退出, 才会执行到这里
            worker._log('spawn_worker after worker init_process')

            sys.exit(0)
        except SystemExit:
            # 比如: worker.init_process中因为修改代码而reload时, 将会运行到这儿
            worker._log('spawn_worker except SystemExit')
            raise
        except AppImportError as e:
            self.log.debug("Exception while loading the application",
                           exc_info=True)
            print("%s" % e, file=sys.stderr)
            sys.stderr.flush()
            sys.exit(self.APP_LOAD_ERROR)
        except Exception as e:
            # woker.init_process中因为修改代码而reload时, 不会运行到这儿
            worker._log('spawn_worker except %s' % e)
            self.log.exception("Exception in worker process")
            if not worker.booted:
                sys.exit(self.WORKER_BOOT_ERROR)
            sys.exit(-1)
        finally:
            # worker.init_process中因为修改代码而reload时, 将会运行到这儿
            worker._log('spawn_worker finally worker exiting')
            self.log.info("Worker exiting (pid: %s)", worker.pid)
            try:
                worker.tmp.close()
                self.cfg.worker_exit(self, worker)
            except:
                self.log.warning("Exception during worker exit:\n%s",
                                  traceback.format_exc())

    # done
    def spawn_workers(self):
        """\
        Spawn new workers as needed.

        This is where a worker process leaves the main loop
        of the master process.
        """
        self._log('spawn_workers')
        for _ in range(self.num_workers - len(self.WORKERS)):
            self.spawn_worker(abc='SP_WKS>')
            time.sleep(0.1 * random.random())

    # done
    def kill_workers(self, sig, abc=''):
        """\
        Kill all workers with the signal `sig`
        :attr sig: `signal.SIG*` value
        """
        self._log('%s kill_workers: %s' % (abc, sig))
        worker_pids = list(self.WORKERS.keys())
        for pid in worker_pids:
            self.kill_worker(pid, sig, abc=abc + 'KL_WKS>')

    # done
    def kill_worker(self, pid, sig, abc=''):
        """Kill a worker

        :attr pid: int, worker pid
        :attr sig: `signal.SIG*` value
         """
        self._log('%s kill_worker pid=%s %s' % (abc, pid, sig))
        try:
            # kill worker的时候worker进程会收到相应的信号并进行处理
            os.kill(pid, sig)
            # 此处不需要 self.cfg.worker_exit, worker正常退出后,
            # 会在 ``spawn_worker`` 中调用 worker_exit
        except OSError as e:
            if e.errno == errno.ESRCH:  # no such process
                try:
                    worker = self.WORKERS.pop(pid)
                    worker.tmp.close()
                    self.cfg.worker_exit(self, worker)
                    return
                except (KeyError, OSError):
                    return
            raise

    def _log(self, msg, *args, **kw):
        colors = {
            0: Fore.RED,
            1: Fore.GREEN,
            2: Fore.BLUE,
            3: Fore.YELLOW,
            4: Fore.CYAN,
        }
        # master使用RESET颜色, worker使用其他颜色
        pid = getattr(self, 'pid', None)
        the_pid = os.getpid()
        if pid is None or pid == the_pid:
            color = Fore.RESET
        else:
            color = colors[the_pid % 5]
        msg = '%s %s {%s} %s' % (
            color, ' ' * 3, threading.current_thread().ident, msg)
        self.log.info(msg, *args, **kw)
