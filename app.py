# encoding: utf-8

"""
一个用于调试的WSGI App
"""

import os
import time
import random
import threading

from flask import Flask
from flask_redis import FlaskRedis

# 全局变量, 用于测试并发请求会不会同时修改一个变量
LIST = []


class Application(object):

    def __call__(self, environ, start_fn):
        x = random.randint(1, 5)
        time.sleep(x)
        LIST.append(x)
        start_fn('200 OK', [('Content-Type', 'text/plain')])
        msg = "PID=%s TID=%s LIST=%s" % (
            os.getpid(), threading.current_thread().ident, LIST)
        return [msg.encode('utf-8')]


class TestDB(object):
    # 并发请求的时候, 每个请求都可以拿到items中的数据(存在脏读)
    items = []

    def commit(self, x):
        self.items.append(x)
        print('Commit %s' % x)


flask_app = Flask(__name__)

test_db = TestDB()
redis = FlaskRedis(flask_app)


@flask_app.route('/test-db/<int:delta>')
def test_test_db(delta):
    test_db.commit(delta)
    time.sleep(delta)
    msg = "PID=%s TID=%s LIST=%s" % (
        os.getpid(), threading.current_thread().ident, test_db.items)
    return msg


@flask_app.route('/redis/<int:delta>')
def test_redis(delta):
    rd_key = 'test:multi:req:list'
    redis.lpush(rd_key, delta)
    time.sleep(delta)
    msg = "PID=%s TID=%s LIST=%s" % (
        os.getpid(), threading.current_thread().ident,
        redis.lrange(rd_key, 0, -1)
    )
    return msg


# app = Application()
app = flask_app
