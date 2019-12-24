# encoding: utf-8

"""
一个用于调试的WSGI App
"""

import os
import time
import random
import threading

from flask import Flask, request, g
from flask_redis import FlaskRedis
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Column
from sqlalchemy import func, text as _text

TMP_LIST = []


class Application(object):

    def __call__(self, environ, start_fn):
        x = random.randint(1, 5)
        time.sleep(x)
        TMP_LIST.append(x)
        start_fn('200 OK', [('Content-Type', 'text/plain')])
        msg = "process=%s thread=%s TMP_LIST=%s" % (
            os.getpid(), threading.current_thread().ident, TMP_LIST)
        return [msg.encode('utf-8')]


class TestDB(object):
    # 并发请求的时候, 每个请求都可以拿到items中的数据(存在脏读)
    items = []

    def commit(self, x):
        self.items.append(x)
        # print('Commit %s' % x)


flask_app = Flask(__name__)
flask_app.config['REDIS_URL'] = "redis://localhost:6379/0"
flask_app.config['SQLALCHEMY_DATABASE_URI'] = \
    "mysql+pymysql://root:root@127.0.0.1:3306/idict"
flask_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = True

test_db = TestDB()
redis = FlaskRedis(flask_app)
sqla_db = SQLAlchemy(flask_app, session_options={'autoflush': False})


class TestModel(sqla_db.Model):

    __tablename__ = 'test'

    id_ = Column('id', sqla_db.BigInteger, autoincrement=True, primary_key=True)
    user_id = Column(
        'user_id', sqla_db.BigInteger, nullable=False, server_default='0')
    name = Column(
        'name', sqla_db.VARCHAR(128), nullable=False, server_default='')
    _type = Column(
        'type', sqla_db.SmallInteger, nullable=False, server_default='0')
    create_time = Column('create_time', sqla_db.TIMESTAMP,
                         nullable=False, server_default=func.now())
    update_time = Column(
        'update_time', sqla_db.TIMESTAMP, nullable=False,
        server_default=_text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'))


@flask_app.route('/ping')
@flask_app.route('/ping/<int:delta>')
def ping(delta=0):
    time.sleep(delta)
    return 'pong'


@flask_app.route('/user/num')
def get_user_num():
    return 'user_num=%s' % TestModel.query.count()


@flask_app.route('/test-db/<int:delta>')
def test_test_db(delta):
    test_db.commit(delta)
    time.sleep(delta)
    msg = "process=%s thread=%s items=%s" % (
        os.getpid(), threading.current_thread().ident, test_db.items)
    return msg


@flask_app.route('/redis/<int:delta>')
def test_redis(delta):
    rd_key = 'gunicorn:test:multi:req:list'
    redis.lpush(rd_key, delta)
    time.sleep(delta)
    msg = "process=%s thread=%s items=%s" % (
        os.getpid(), threading.current_thread().ident,
        redis.lrange(rd_key, 0, -1)
    )
    return msg


@flask_app.route('/sqla/<int:delta>')
def test_sqla(delta):
    obj = TestModel(user_id=delta, name=str(delta), _type=delta)
    sqla_db.session.add(obj)
    time.sleep(delta)
    sqla_db.session.commit()

    msg = "process=%s thread=%s COUNT=%s" % (
        os.getpid(), threading.current_thread().ident,
        TestModel.query.count()
    )
    return msg


# app = Application()
app = flask_app
