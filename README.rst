Gunicorn 源码分析
-----------------

阅读Gunicorn源码, 添加了很多注释和todo, 当然也有很多不清楚的地方. 希望在弄懂Gunicorn整体
架构的基础上, 通过扣细节学到学到更多的东西.


1. 环境搭建

::

    # 将 .venv 文件夹安装在当前目录
    $ export PIPENV_VENV_IN_PROJECT=$PWD
    # 安装依赖包
    $ pipenv install
    # 激活环境
    $ pipenv shell
