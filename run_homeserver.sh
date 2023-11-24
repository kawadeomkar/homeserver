#!/usr/bin/env bash

set -ex

ansible-playbook -e @vault.enc -K playbook.yaml

