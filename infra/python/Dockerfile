# 自己脚本用的共享 Python 镜像（promtail-sd 和 Woodpecker CI 共用）。
# 一个容器用一张镜像；不要单独起一个空转的「环境」服务。
# git：policy（check_scope.py）要对 main...HEAD 取 diff / log，打进镜像以免每次 CI 再 apk。
# docker-cli：CD 用宿主机 sock 做 `docker compose up -d`（不带 woodpecker 自身）。
FROM docker.m.daocloud.io/library/python:3.12-alpine@sha256:d09d15e60962ca365d1cd544a48773bac9d33f2fb1b00f2aa0deec78ade7dc31

# apk 默认 dl-cdn.alpinelinux.org，国内常慢；包走阿里云。基础镜像仍是 DaoCloud。
RUN sed -i 's#https://dl-cdn.alpinelinux.org/alpine#https://mirrors.aliyun.com/alpine#g' /etc/apk/repositories \
    && apk add --no-cache git openssh-client docker-cli docker-cli-compose

WORKDIR /opt/infra
COPY requirements.txt .
RUN pip install --no-cache-dir \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com \
    -r requirements.txt
