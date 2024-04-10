#!/usr/bin/env bash

set -ex

ansible-playbook -i inventory -e @vault.enc -K playbook.yaml

