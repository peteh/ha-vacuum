FROM python:3.10.4-alpine
RUN apk update \
  && apk add \
    build-base \
    linux-headers
RUN mkdir /usr/src/app
WORKDIR /usr/src/app
COPY ./requirements.txt .
RUN pip install -r requirements.txt
ENV PYTHONUNBUFFERED 1
COPY . .
