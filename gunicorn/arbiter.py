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

        self.setup(app)

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

        # todo: reopen files
        if 'GUNICORN_FD' in os.environ:
            self.log.reopen_files()

        self.worker_class = self.cfg.worker_class
        self.address = self.cfg.address
        self.num_workers = self.cfg.workers
        self.timeout = self.cfg.timeout
        self.proc_name = self.cfg.proc_name
        self._log('proc_name: %s' % self.proc_name)  # e.g.: app:app

        self.log.debug('Current configuration:\n{0}'.format(
            '\n'.join(
                '  {0}: {1}'.format(config, value.value)
                for config, value in
                sorted(self.cfg.settings.items(), key=lambda s: s[1])
            )
        ))

        # set enviroment' variables
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
        self.log.info("Starting gunicorn %s", __version__)

        if 'GUNICORN_PID' in os.environ:
            self.master_pid = int(os.environ.get('GUNICORN_PID'))
            # todo: 数字后缀?
            self.proc_name = self.proc_name + ".2"
            self.master_name = "Master.2"

        self.pid = os.getpid()
        if self.cfg.pidfile is not None:
            pidname = self.cfg.pidfile
            # todo: 什么时候 master_pid != 0 ?
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

            elif self.master_pid:
                fds = []
                for fd in os.environ.pop('GUNICORN_FD').split(','):
                    fds.append(int(fd))

            # create_sockets中会关闭(旧)fds
            # 若 fds=None, 则从cfg中设置的bind创建sock
            self.LISTENERS = sock.create_sockets(self.cfg, self.log, fds)

        listeners_str = ",".join([str(l) for l in self.LISTENERS])
        self.log.debug("Arbiter booted")
        self.log.info("Listening at: %s (%s)", listeners_str, self.pid)
        self.log.info("Using worker: %s", self.cfg.worker_class_str)
        systemd.sd_notify("READY=1\nSTATUS=Gunicorn arbiter booted", self.log)

    # done
    def init_signals(self):
        """\
        Initialize master signal handling. Most of the signals
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
        self._log('signal and will wakeup sig=%s' % sig)
        if len(self.SIG_QUEUE) < 5:
            # todo: 为什么是5? 更多的信号忽略? 什么时候会产生很多的信号?
            self.SIG_QUEUE.append(sig)
            self.wakeup()

    def run(self):
        """Main master loop.
        """
        self._log('run')
        self.start()
        util._setproctitle("master [%s]" % self.proc_name)

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
                self.log.info("Handling signal: %s", signame)
                handler()

                # todo: 为啥此处要 wakeup?
                self._log('run will wakeup')
                self.wakeup()

        except (StopIteration, KeyboardInterrupt) as e:
            self._log('run except1: %s' % e)
            self.halt()
        except HaltServer as inst:
            self._log('run except2: %s' % inst)
            self.halt(reason=inst.reason, exit_status=inst.exit_status)
        except SystemExit:
            self._log('run except3: SystemExit')
            raise
        except Exception as e:
            self._log('run except4: %s' % e)
            self.log.info("Unhandled exception in main loop", exc_info=True)
            self.stop(False)
            if self.pidfile is not None:
                self.pidfile.unlink()
            sys.exit(-1)

    # do self.reap_workers
    def handle_chld(self, sig, frame):
        """SIGCHLD handling

        任何一个子进程(init除外)在exit后并非马上就消失，而是留下一个称外僵尸进程的
        数据结构, 等待父进程处理。另外子进程退出的时候会向其父进程发送一个SIGCHLD信号
        """
        self._log('handle_chld & reap_workers & wakeup')
        self.reap_workers()
        self.wakeup()

    # do self.reload
    def handle_hup(self):
        """\
        HUP handling.
        - Reload configuration
        - Start the new worker processes with a new configuration
        - Gracefully shutdown the old worker processes
        """
        self._log('handle_hup & self.reload')
        self.log.info("Hang up: %s", self.master_name)
        self.reload()

    # raise StopIteration
    def handle_term(self):
        """SIGTERM handling
        """
        self._log('handle_term & StopIteration')
        raise StopIteration

    # self.stop & raise StopIteration
    def handle_int(self):
        """SIGINT handling
        """
        self._log('handle_int & self.stop')
        self.stop(False)
        raise StopIteration

    # self.stop & raise StopIteration
    def handle_quit(self):
        """SIGQUIT handling
        """
        self._log('handle_quit & stop & StopIteration')
        self.stop(False)
        raise StopIteration

    # todo SIGTTIN SIGTTOUT 是啥?
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

    def handle_usr1(self):
        """\
        SIGUSR1 handling.
        Kill all workers by sending them a SIGUSR1
        """
        self._log('handle_usr1 & kill_workers')
        self.log.reopen_files()
        self.kill_workers(signal.SIGUSR1)

    def handle_usr2(self):
        """\
        SIGUSR2 handling.
        Creates a new master/worker set as a slave of the current
        master without affecting old workers. Use this to do live
        deployment with the ability to backout a change.
        """
        self._log('handle_usr2 & self.reexec')
        self.reexec()

    def handle_winch(self):
        """SIGWINCH handling"""
        self._log('handle_winch')
        if self.cfg.daemon:
            self.log.info("graceful stop of workers")
            self.num_workers = 0
            self.kill_workers(signal.SIGTERM)
        else:
            self.log.debug("SIGWINCH ignored. Not daemonized")

    def maybe_promote_master(self):
        # self._log('maybe_promote_master')
        if self.master_pid == 0:
            return

        if self.master_pid != os.getppid():
            self.log.info("Master has been promoted.")
            # reset master infos
            self.master_name = "Master"
            self.master_pid = 0
            self.proc_name = self.cfg.proc_name
            del os.environ['GUNICORN_PID']
            # rename the pidfile
            if self.pidfile is not None:
                self.pidfile.rename(self.cfg.pidfile)
            # reset proctitle
            util._setproctitle("master [%s]" % self.proc_name)

    # done: 向 PIPE 中写入数据
    def wakeup(self):
        """\
        Wake up the arbiter by writing to the PIPE
        """
        self._log('wakeup')
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

    # todo: halt 和 stop 的区别? 什么时候用哪个?
    # done: stop & unlink pidfile & exit
    def halt(self, reason=None, exit_status=0):
        """halt arbiter
        """
        self._log('halt reason=%s exit_status=%s' % (reason, exit_status))
        self.stop()
        self.log.info("Shutting down: %s", self.master_name)
        if reason is not None:
            self.log.info("Reason: %s", reason)
        if self.pidfile is not None:
            self.pidfile.unlink()
        self.cfg.on_exit(self)
        sys.exit(exit_status)

    # done close_sockets & self.kill_workers
    def stop(self, graceful=True):
        """Stop workers

        :attr graceful: boolean, If True (the default) workers will be
        killed gracefully  (ie. trying to wait for the current connection)
        """
        self._log('stop graceful=%s' % graceful)
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
        limit = time.time() + self.cfg.graceful_timeout
        # instruct the workers to exit
        self.kill_workers(sig)
        # wait until the graceful timeout
        while self.WORKERS and time.time() < limit:
            time.sleep(0.1)

        self.kill_workers(signal.SIGKILL)

    def reexec(self):
        """\
        Relaunch the master and workers.
        """
        self._log('reexec')
        if self.reexec_pid != 0:
            self.log.warning("USR2 signal ignored. Child exists.")
            return

        if self.master_pid != 0:
            self.log.warning("USR2 signal ignored. Parent exists.")
            return

        master_pid = os.getpid()
        self.reexec_pid = os.fork()
        if self.reexec_pid != 0:
            return

        self.cfg.pre_exec(self)

        environ = self.cfg.env_orig.copy()
        environ['GUNICORN_PID'] = str(master_pid)

        if self.systemd:
            environ['LISTEN_PID'] = str(os.getpid())
            environ['LISTEN_FDS'] = str(len(self.LISTENERS))
        else:
            environ['GUNICORN_FD'] = ','.join(
                str(l.fileno()) for l in self.LISTENERS)

        os.chdir(self.START_CTX['cwd'])

        # exec the process using the original environment
        os.execvpe(self.START_CTX[0], self.START_CTX['args'], environ)

    def reload(self):
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
            # init new listeners
            self.LISTENERS = sock.create_sockets(self.cfg, self.log)
            listeners_str = ",".join([str(l) for l in self.LISTENERS])
            self.log.info("Listening at: %s", listeners_str)

        # do some actions on reload
        self.cfg.on_reload(self)

        # unlink pidfile
        if self.pidfile is not None:
            self.pidfile.unlink()

        # create new pidfile
        if self.cfg.pidfile is not None:
            self.pidfile = Pidfile(self.cfg.pidfile)
            self.pidfile.create(self.pid)

        # set new proc_name
        util._setproctitle("master [%s]" % self.proc_name)

        # spawn new workers
        for _ in range(self.cfg.workers):
            self.spawn_worker()

        # manage workers
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
                self.kill_worker(pid, signal.SIGABRT)
            else:
                self.kill_worker(pid, signal.SIGKILL)

    def reap_workers(self):
        """\
        Reap workers to avoid zombie processes
        """
        self._log('reap_workers')
        try:
            while True:
                wpid, status = os.waitpid(-1, os.WNOHANG)
                if not wpid:
                    break
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
            self.kill_worker(pid, signal.SIGTERM)

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
    def spawn_worker(self):
        self._log('spawn_worker')
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
            self.log.info("Booting worker with pid: %s", worker.pid)
            self.cfg.post_fork(self, worker)

            self._log('spawn_worker before worker init_process')

            # todo: 现在是在子进程中, 此处会hang住
            worker.init_process()

            # todo: 如: 直到Ctrl-C worker退出, 才会执行到这里
            self._log('spawn_worker after worker init_process')

            sys.exit(0)
        except SystemExit:
            # 比如: worker.init_process中因为修改代码而reload时, 将会运行到这儿
            self._log('spawn_worker except SystemExit')
            raise
        except AppImportError as e:
            self.log.debug("Exception while loading the application",
                           exc_info=True)
            print("%s" % e, file=sys.stderr)
            sys.stderr.flush()
            sys.exit(self.APP_LOAD_ERROR)
        except Exception as e:
            # woker.init_process中因为修改代码而reload时, 不会运行到这儿
            self._log('spawn_worker except %s' % e)
            self.log.exception("Exception in worker process")
            if not worker.booted:
                sys.exit(self.WORKER_BOOT_ERROR)
            sys.exit(-1)
        finally:
            # worker.init_process中因为修改代码而reload时, 将会运行到这儿
            self._log('spawn_worker finally')
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
            self.spawn_worker()
            time.sleep(0.1 * random.random())

    # done
    def kill_workers(self, sig):
        """\
        Kill all workers with the signal `sig`
        :attr sig: `signal.SIG*` value
        """
        self._log('kill_workers: %s' % sig)
        worker_pids = list(self.WORKERS.keys())
        for pid in worker_pids:
            self.kill_worker(pid, sig)

    # done
    def kill_worker(self, pid, sig):
        """Kill a worker

        :attr pid: int, worker pid
        :attr sig: `signal.SIG*` value
         """
        self._log('kill_worker pid=%s %s' % (pid, sig))
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

    def _log(self, msg):
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
        self.log.info('%s %s {%s} %s' % (
            color, ' ' * 3, threading.current_thread().ident, msg)
        )
