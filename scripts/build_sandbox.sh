#!/usr/bin/env sh
set -eu
TAG="${1:-codeguard-sandbox-python:3.12}"
docker build -f dockerfiles/sandbox/python.Dockerfile -t "$TAG" .
echo "built $TAG"
